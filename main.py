#!/usr/bin/env python3
"""
traceroute-geoip: traceroute that tells you who owns every hop.

Runs the system traceroute (tracert on Windows, traceroute on Linux/macOS),
looks every hop up in your own MySQL GeoIP/ASN database, and prints
per-hop latency, packet loss, network, ASN, company and country.

Supports IPv4 and IPv6, colour output, and a machine-readable --json mode.

Requires:  pip install pymysql   (plus `cryptography` on MySQL 8 setups)

Setup:     copy config.example.ini to config.ini and fill in your database
           details. config.ini is git-ignored, so credentials never get
           committed.

Usage:
    python traceroute_geoip.py google.com
    python traceroute_geoip.py 1.1.1.1 --max-hops 20
    python traceroute_geoip.py ipv6.google.com -6
    python traceroute_geoip.py example.com --json > trace.json
"""

import argparse
import configparser
import ipaddress
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
from pathlib import Path

import pymysql

# --------------------------------------------------------------------------
# Configuration (config.ini next to this script, or --config PATH)
# --------------------------------------------------------------------------
DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.ini")

CGNAT = ipaddress.IPv4Network("100.64.0.0/10")
ER_NO_SUCH_TABLE = 1146


def load_config(path):
    """Read database settings from an INI file and return them as a dict."""
    path = Path(path)
    if not path.is_file():
        die(f"Config file not found: {path}\n"
            f"Copy config.example.ini to config.ini and fill in your database details.")

    # interpolation=None so a '%' in a password is taken literally
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
        db = parser["database"]
        tables = parser["tables"] if parser.has_section("tables") else {}
        cfg = {
            "host": db.get("host", "127.0.0.1").strip(),
            "port": db.getint("port", 3306),
            "user": db.get("user", "").strip(),
            "password": db.get("password", ""),
            "name": db.get("name", "geoip").strip(),
            # Tables are searched in order; the first match wins.
            "tables_v4": tables.get("ipv4", "pl_ipv4,all_ipv4"),
            "tables_v6": tables.get("ipv6", "pl_ipv6,all_ipv6"),
        }
    except (configparser.Error, ValueError, KeyError) as e:
        die(f"Problem reading {path}: {e}\n"
            f"Compare it with config.example.ini (it needs a [database] section).")

    if not cfg["user"]:
        die(f"No database user set in {path} (see [database] -> user).")
    return cfg


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
class Palette:
    CODES = {
        "dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33",
        "blue": "34", "magenta": "35", "cyan": "36",
    }

    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, text, style):
        if not self.enabled or not style:
            return str(text)
        return f"\033[{self.CODES[style]}m{text}\033[0m"

    @staticmethod
    def latency_style(ms):
        if ms < 30:
            return "green"
        if ms < 100:
            return "yellow"
        return "red"


def colour_wanted(args):
    if args.json or args.no_color or "NO_COLOR" in os.environ:
        return False
    return sys.stdout.isatty()


def enable_windows_ansi():
    if platform.system() == "Windows":
        os.system("")  # flips on VT100 processing in the Windows console


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
class GeoDB:
    def __init__(self, cfg, tables_v4, tables_v6):
        self.cfg = cfg
        self.tables_v4 = [t.strip() for t in tables_v4.split(",") if t.strip()]
        self.tables_v6 = [t.strip() for t in tables_v6.split(",") if t.strip()]
        self.missing_tables = set()
        self.conn = None
        self._connect()

    def _connect(self):
        self.conn = pymysql.connect(
            host=self.cfg["host"],
            port=self.cfg["port"],
            user=self.cfg["user"],
            password=self.cfg["password"],
            database=self.cfg["name"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            autocommit=True,
        )

    def close(self):
        if self.conn:
            self.conn.close()

    def _query(self, sql, params):
        for attempt in (1, 2):
            try:
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchone()
            except pymysql.err.OperationalError:
                if attempt == 2:
                    raise
                self.conn.ping(reconnect=True)

    def lookup(self, ip):
        """Return the most specific matching row for an ip, or None."""
        if ip.version == 4:
            tables = self.tables_v4
            key = int(ip)
            order = "(ip_end - ip_start) ASC"
        else:
            tables = self.tables_v6
            key = ip.packed  # BINARY(16), same as INET6_ATON()
            # CIDR ranges nest, so the narrowest range containing the address
            # is the one with the greatest start.
            order = "ip_start DESC, ip_end ASC"

        for table in tables:
            if table in self.missing_tables:
                continue
            sql = (
                f"SELECT cidr, asn, company, prefix_country, as_country "
                f"FROM `{table}` WHERE %s BETWEEN ip_start AND ip_end "
                f"ORDER BY {order} LIMIT 1"
            )
            try:
                row = self._query(sql, (key,))
            except pymysql.err.ProgrammingError as e:
                if e.args and e.args[0] == ER_NO_SUCH_TABLE:
                    self.missing_tables.add(table)
                    continue
                raise
            if row:
                row["table"] = table
                return row
        return None

    def describe(self, ip):
        """Classify an address and return a dict describing it."""
        base = {"ip": str(ip), "version": ip.version}

        if ip.is_loopback:
            return {**base, "kind": "loopback", "note": "loopback"}
        if ip.is_link_local:
            return {**base, "kind": "link-local", "note": "link-local"}
        if ip.is_private:
            return {**base, "kind": "private", "note": "private / local network"}
        if ip.version == 4 and ip in CGNAT:
            return {**base, "kind": "cgnat", "note": "CGNAT (shared address space)"}

        row = self.lookup(ip)
        if row:
            return {
                **base,
                "kind": "match",
                "cidr": row["cidr"],
                "asn": row["asn"],
                "company": row["company"],
                "prefix_country": row["prefix_country"],
                "as_country": row["as_country"],
                "table": row["table"],
            }

        tables = self.tables_v4 if ip.version == 4 else self.tables_v6
        if tables and all(t in self.missing_tables for t in tables):
            note = f"no IPv{ip.version} table in database"
        else:
            note = "no match in database"
        return {**base, "kind": "unknown", "note": note}


# --------------------------------------------------------------------------
# Traceroute plumbing
# --------------------------------------------------------------------------
def resolve(target, family):
    fam = {4: socket.AF_INET, 6: socket.AF_INET6, None: socket.AF_UNSPEC}[family]
    infos = socket.getaddrinfo(target, None, fam, socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)  # prefer IPv4 when both exist
    return ipaddress.ip_address(infos[0][4][0].split("%")[0])


def build_command(ip, max_hops):
    system = platform.system()
    if system == "Windows":
        return ["tracert", "-d", f"-{ip.version}", "-h", str(max_hops), str(ip)]
    if system == "Darwin" and ip.version == 6:
        return ["traceroute6", "-n", "-m", str(max_hops), str(ip)]
    return ["traceroute", "-n", f"-{ip.version}", "-m", str(max_hops), str(ip)]


HOP_RE = re.compile(r"^\s*(\d+)\s+(.*)$")
RTT_RE = re.compile(r"(?<![\w.])<?(\d+(?:[.,]\d+)?)\s*ms")
STAR_RE = re.compile(r"(?<!\S)\*(?!\S)")


def parse_hop(line):
    """Parse one tracert/traceroute line into hop number, IPs and RTTs."""
    m = HOP_RE.match(line)
    if not m:
        return None
    hop = int(m.group(1))
    rest = m.group(2)

    rtts = [float(x.replace(",", ".")) for x in RTT_RE.findall(rest)]
    stars = len(STAR_RE.findall(rest))

    ips = []
    for token in re.split(r"[\s\[\]()]+", rest):
        token = token.split("%")[0]  # drop IPv6 scope ids
        try:
            ip = ipaddress.ip_address(token)
        except ValueError:
            continue
        if ip not in ips:
            ips.append(ip)

    return {"hop": hop, "ips": ips, "rtts": rtts, "timeouts": stars}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def geo_text(info, pal):
    kind = info["kind"]
    if kind in ("private", "loopback", "link-local", "cgnat"):
        return pal(info["note"], "dim")
    if kind == "unknown":
        return pal(info["note"], "yellow")

    parts = [pal(info["cidr"], "dim")]
    if info["asn"]:
        parts.append(pal(f"AS{info['asn']}", "magenta"))
    if info["company"]:
        parts.append(info["company"])
    country = info["prefix_country"] or "??"
    if info["as_country"] and info["as_country"] != country:
        country_txt = f"{country} (AS: {info['as_country']})"
    else:
        country_txt = country
    parts.append(pal(country_txt, "cyan"))
    return " | ".join(parts)


def print_hop(h, pal, ip_width):
    num = pal(f"{h['hop']:>3}", "dim")

    if not h["ips"]:
        print(f"{num}  {pal('*', 'dim')}  {pal('(no reply)', 'dim')}")
        return

    stats = h["rtt_ms"]
    if stats:
        lat = pal(f"{stats['avg']:>7.1f} ms", pal.latency_style(stats["avg"]))
        rng = pal(f"({stats['min']:.1f}-{stats['max']:.1f})".ljust(17), "dim")
    else:
        lat = pal(f"{'-':>10}", "dim")
        rng = " " * 17
    loss = pal(f"{h['loss_pct']:.0f}% loss", "red") if h["loss_pct"] else ""

    for i, info in enumerate(h["ips"]):
        ip_txt = info["ip"].ljust(ip_width)
        geo = geo_text(info, pal)
        if i == 0:
            line = f"{num}  {ip_txt}  {lat}  {rng} {geo}"
            if loss:
                line += f"  {loss}"
        else:
            line = f"{pal('', None):>3}  {pal('↳', 'dim')} {ip_txt}  {' ' * 10}  {' ' * 17} {geo}"
        print(line)


def build_summary(hops, target_ip):
    responding = [h for h in hops if h["ips"]]
    reached = bool(hops) and any(i["ip"] == str(target_ip) for i in hops[-1]["ips"])

    timed = [h for h in hops if h["rtt_ms"]]
    slowest = max(timed, key=lambda h: h["rtt_ms"]["avg"], default=None)

    biggest_jump = None
    prev = None
    for h in timed:
        if prev is not None:
            delta = h["rtt_ms"]["avg"] - prev["rtt_ms"]["avg"]
            if biggest_jump is None or delta > biggest_jump["delta_ms"]:
                biggest_jump = {"from_hop": prev["hop"], "to_hop": h["hop"],
                                "delta_ms": round(delta, 1)}
        prev = h

    countries, networks = [], []
    for h in hops:
        for info in h["ips"]:
            if info["kind"] != "match":
                continue
            c = info["prefix_country"]
            if c and (not countries or countries[-1] != c):
                countries.append(c)
            if info["asn"]:
                net = {"asn": info["asn"], "company": info["company"]}
                if not networks or networks[-1]["asn"] != net["asn"]:
                    networks.append(net)

    lossy = [h["hop"] for h in hops if h["loss_pct"] and h["ips"]]

    return {
        "destination_reached": reached,
        "total_hops": len(hops),
        "responding_hops": len(responding),
        "slowest_hop": ({"hop": slowest["hop"], "avg_ms": slowest["rtt_ms"]["avg"]}
                        if slowest else None),
        "biggest_latency_jump": biggest_jump,
        "hops_with_loss": lossy,
        "countries_path": countries,
        "networks_path": networks,
    }


def print_summary(s, pal):
    print()
    print(pal("Summary", "bold"))
    reached = pal("yes", "green") if s["destination_reached"] else pal("no", "red")
    print(f"  Destination reached : {reached} ({s['total_hops']} hops, "
          f"{s['responding_hops']} responded)")

    if s["slowest_hop"]:
        ms = s["slowest_hop"]["avg_ms"]
        print(f"  Slowest hop         : #{s['slowest_hop']['hop']} "
              f"({pal(f'{ms:.1f} ms', pal.latency_style(ms))})")
    j = s["biggest_latency_jump"]
    if j and j["delta_ms"] > 0:
        print(f"  Biggest latency jump: #{j['from_hop']} -> #{j['to_hop']} "
              f"(+{j['delta_ms']:.1f} ms)")
    if s["hops_with_loss"]:
        hops = ", ".join(f"#{n}" for n in s["hops_with_loss"])
        print(f"  Hops with loss      : {pal(hops, 'red')}")
    if s["countries_path"]:
        print(f"  Countries           : {' -> '.join(s['countries_path'])}")
    if s["networks_path"]:
        nets = " -> ".join(
            f"AS{n['asn']}" + (f" {n['company']}" if n["company"] else "")
            for n in s["networks_path"]
        )
        print(f"  Networks            : {nets}")
    print(pal("  Note: latency is ICMP/UDP probe time to each router. A slow middle hop "
              "with a fast final hop is usually just rate-limiting.", "dim"))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Traceroute with GeoIP/ASN lookups per hop")
    p.add_argument("target", help="Hostname or IP address")
    fam = p.add_mutually_exclusive_group()
    fam.add_argument("-4", dest="family", action="store_const", const=4, help="force IPv4")
    fam.add_argument("-6", dest="family", action="store_const", const=6, help="force IPv6")
    p.add_argument("--max-hops", type=int, default=30)
    p.add_argument("--json", action="store_true", help="print one JSON document instead of text")
    p.add_argument("--no-color", action="store_true", help="disable colour output")
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="path to the config file (default: config.ini next to the script)")
    p.add_argument("--tables-v4", default=None,
                   help="comma-separated IPv4 tables, searched in order (overrides config)")
    p.add_argument("--tables-v6", default=None,
                   help="comma-separated IPv6 tables, searched in order (overrides config)")
    args = p.parse_args()

    cfg = load_config(args.config)
    tables_v4 = args.tables_v4 or cfg["tables_v4"]
    tables_v6 = args.tables_v6 or cfg["tables_v6"]

    use_colour = colour_wanted(args)
    if use_colour:
        enable_windows_ansi()
    pal = Palette(use_colour)

    try:
        target_ip = resolve(args.target, args.family)
    except socket.gaierror:
        die(f"Cannot resolve {args.target}")

    try:
        db = GeoDB(cfg, tables_v4, tables_v6)
    except pymysql.MySQLError as e:
        die(f"Database connection failed: {e}")

    cmd = build_command(target_ip, args.max_hops)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        die(f"{cmd[0]} not found. On Debian/Ubuntu: sudo apt install traceroute")

    if not args.json:
        print(f"Traceroute to {pal(args.target, 'bold')} ({target_ip}), "
              f"IPv{target_ip.version}, max {args.max_hops} hops\n")

    ip_width = 15 if target_ip.version == 4 else 39
    hops = []
    interrupted = False

    try:
        for line in proc.stdout:
            parsed = parse_hop(line)
            if not parsed:
                continue

            rtts = parsed["rtts"]
            sent = len(rtts) + parsed["timeouts"]
            hop = {
                "hop": parsed["hop"],
                "ips": [],
                "probes_sent": sent,
                "timeouts": parsed["timeouts"],
                "loss_pct": round(parsed["timeouts"] / sent * 100, 1) if sent else 0.0,
                "rtt_ms": ({"min": min(rtts), "avg": round(statistics.mean(rtts), 2),
                            "max": max(rtts)} if rtts else None),
            }
            for ip in parsed["ips"]:
                try:
                    hop["ips"].append(db.describe(ip))
                except pymysql.MySQLError as e:
                    hop["ips"].append({"ip": str(ip), "version": ip.version,
                                       "kind": "unknown", "note": f"DB error: {e}"})
            hops.append(hop)
            if not args.json:
                print_hop(hop, pal, ip_width)
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()
    finally:
        db.close()

    summary = build_summary(hops, target_ip)

    if args.json:
        print(json.dumps({
            "target": args.target,
            "resolved_ip": str(target_ip),
            "ip_version": target_ip.version,
            "interrupted": interrupted,
            "hops": hops,
            "summary": summary,
        }, indent=2, ensure_ascii=False))
    else:
        if interrupted:
            print("\nInterrupted.")
        print_summary(summary, pal)


if __name__ == "__main__":
    main()