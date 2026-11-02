#!/usr/bin/env python3
"""
traceroute-geoip: traceroute that tells you who owns every hop.

Runs the system traceroute (tracert on Windows, traceroute on Linux/macOS),
looks every hop up in your own MySQL GeoIP/ASN database, and prints
per-hop latency, packet loss, network, ASN, company and country.

Supports IPv4 and IPv6, colour output, per-hop reverse DNS (--resolve),
JSON and CSV output, and a live, refreshing MTR-style mode (--mtr).

Requires:  pip install pymysql   (plus `cryptography` on MySQL 8 setups)

Setup:     copy config.example.ini to config.ini and fill in your database
           details. config.ini is git-ignored, so credentials never get
           committed.

Usage:
    python main.py google.com
    python main.py 1.1.1.1 --max-hops 20
    python main.py ipv6.google.com -6
    python main.py example.com --resolve
    python main.py example.com --json > trace.json
    python main.py example.com --csv > trace.csv
    python main.py example.com --mtr
    python main.py example.com --mtr --count 20 --csv > mtr.csv
"""

import argparse
import configparser
import csv
import ipaddress
import json
import math
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    if args.json or args.csv or args.no_color or "NO_COLOR" in os.environ:
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
# Reverse DNS
# --------------------------------------------------------------------------
class Resolver:
    """Reverse DNS with a hard timeout (gethostbyaddr can hang for seconds)."""

    def __init__(self, timeout=2.0):
        self.timeout = timeout
        self.cache = {}

    @staticmethod
    def _work(ip, box):
        try:
            name = socket.gethostbyaddr(ip)[0]
            box.append(None if name == ip else name)
        except (OSError, UnicodeError):
            box.append(None)

    def resolve_many(self, ips):
        """Look up several addresses in parallel; returns {ip: hostname or None}."""
        pending = {}
        for ip in ips:
            if ip in self.cache or ip in pending:
                continue
            box = []
            t = threading.Thread(target=self._work, args=(ip, box), daemon=True)
            t.start()
            pending[ip] = (t, box)

        deadline = time.monotonic() + self.timeout
        for ip, (t, box) in pending.items():
            t.join(max(0.0, deadline - time.monotonic()))
            self.cache[ip] = box[0] if box else None
        return {ip: self.cache.get(ip) for ip in ips}


# --------------------------------------------------------------------------
# Traceroute plumbing
# --------------------------------------------------------------------------
def resolve_target(target, family):
    fam = {4: socket.AF_INET, 6: socket.AF_INET6, None: socket.AF_UNSPEC}[family]
    infos = socket.getaddrinfo(target, None, fam, socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)  # prefer IPv4 when both exist
    return ipaddress.ip_address(infos[0][4][0].split("%")[0])


def build_command(ip, max_hops):
    system = platform.system()
    if system == "Windows":
        return ["tracert", "-d", f"-{ip.version}", "-h", str(max_hops), str(ip)]
    if system == "Darwin":
        # macOS (BSD) traceroute has no -4/-6 flags; IPv6 uses a separate binary.
        tool = "traceroute6" if ip.version == 6 else "traceroute"
        return [tool, "-n", "-m", str(max_hops), str(ip)]
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


def make_hop(parsed, db, resolver=None):
    """Turn a parsed traceroute line into a hop dict with GeoIP (and DNS) data."""
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

    if resolver and hop["ips"]:
        names = resolver.resolve_many([i["ip"] for i in hop["ips"]])
        for info in hop["ips"]:
            info["hostname"] = names.get(info["ip"])
    return hop


def run_traceroute(target_ip, max_hops, db, resolver, on_hop):
    """Run the system traceroute, calling on_hop(hop) as each hop arrives."""
    cmd = build_command(target_ip, max_hops)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        die(f"{cmd[0]} not found. On Debian/Ubuntu: sudo apt install traceroute")

    hops = []
    other = []  # lines that aren't hops: headers, or error messages from the tool
    interrupted = False
    try:
        for line in proc.stdout:
            parsed = parse_hop(line)
            if not parsed:
                if line.strip():
                    other.append(line.rstrip())
                continue
            hop = make_hop(parsed, db, resolver)
            hops.append(hop)
            on_hop(hop)
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()

    if not hops and not interrupted:
        # Don't fail silently: show what the traceroute command actually said.
        print(f"No hops were read from: {' '.join(cmd)}", file=sys.stderr)
        for line in other[-5:]:
            print(f"  {line}", file=sys.stderr)
    return hops, interrupted


# --------------------------------------------------------------------------
# Text output
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


def geo_and_host(info, pal):
    """Network description, followed by the reverse-DNS name when we have one."""
    text = geo_text(info, pal)
    host = info.get("hostname")
    if host:
        text += f"  {pal(host, 'blue')}"
    return text


def print_hop(h, pal, ip_width):
    num = pal(f"{h['hop']:>3}", "dim")

    if not h["ips"]:
        print(f"{num}  {pal('*', 'dim')}  {pal('(no reply)', 'dim')}", flush=True)
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
        geo = geo_and_host(info, pal)
        if i == 0:
            line = f"{num}  {ip_txt}  {lat}  {rng} {geo}"
            if loss:
                line += f"  {loss}"
        else:
            line = f"{pal('', None):>3}  {pal('↳', 'dim')} {ip_txt}  {' ' * 10}  {' ' * 17} {geo}"
        print(line, flush=True)


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
# CSV output
# --------------------------------------------------------------------------
GEO_COLS = ["kind", "cidr", "asn", "company", "prefix_country", "as_country"]

TRACE_CSV_HEADER = (
    ["hop", "ip", "hostname", "ip_version", *GEO_COLS,
     "probes_sent", "timeouts", "loss_pct", "rtt_min_ms", "rtt_avg_ms", "rtt_max_ms"]
)
MTR_CSV_HEADER = (
    ["hop", "ip", "hostname", "ip_version", *GEO_COLS,
     "sent", "received", "loss_pct", "last_ms", "avg_ms", "best_ms", "worst_ms", "stdev_ms"]
)


def blank(value):
    return "" if value is None else value


def geo_fields(info):
    return [blank(info.get(col)) for col in GEO_COLS]


def trace_csv_rows(h):
    """One row per responding address (a hop with no reply gets one empty row)."""
    rtt = h["rtt_ms"] or {}
    common = [h["probes_sent"], h["timeouts"], h["loss_pct"],
              blank(rtt.get("min")), blank(rtt.get("avg")), blank(rtt.get("max"))]
    if not h["ips"]:
        return [[h["hop"], "", "", "", *[""] * len(GEO_COLS), *common]]
    return [[h["hop"], i["ip"], blank(i.get("hostname")), i["version"],
             *geo_fields(i), *common] for i in h["ips"]]


def mtr_csv_row(h, s):
    st = s.as_dict()
    stats = [st["sent"], st["received"], st["loss_pct"], blank(st["last_ms"]),
             blank(st["avg_ms"]), blank(st["best_ms"]), blank(st["worst_ms"]),
             blank(st["stdev_ms"])]
    if not h["ips"]:
        return [h["hop"], "", "", "", *[""] * len(GEO_COLS), *stats]
    i = h["ips"][0]
    return [h["hop"], i["ip"], blank(i.get("hostname")), i["version"],
            *geo_fields(i), *stats]


# --------------------------------------------------------------------------
# Live MTR-style mode
# --------------------------------------------------------------------------
class HopStats:
    """Running statistics for one hop (Welford's algorithm for the std-dev)."""

    def __init__(self):
        self.sent = 0
        self.received = 0
        self.last = None
        self.best = None
        self.worst = None
        self.mean = 0.0
        self.m2 = 0.0

    def add(self, rtt):
        """Record one probe; rtt is None when it was lost."""
        self.sent += 1
        if rtt is None:
            return
        self.received += 1
        self.last = rtt
        self.best = rtt if self.best is None else min(self.best, rtt)
        self.worst = rtt if self.worst is None else max(self.worst, rtt)
        delta = rtt - self.mean
        self.mean += delta / self.received
        self.m2 += delta * (rtt - self.mean)

    @property
    def loss_pct(self):
        return (self.sent - self.received) / self.sent * 100 if self.sent else 0.0

    @property
    def avg(self):
        return self.mean if self.received else None

    @property
    def stdev(self):
        if not self.received:
            return None
        return math.sqrt(self.m2 / (self.received - 1)) if self.received > 1 else 0.0

    def as_dict(self):
        def r(v, n=2):
            return None if v is None else round(v, n)
        return {
            "sent": self.sent,
            "received": self.received,
            "loss_pct": round(self.loss_pct, 1),
            "last_ms": r(self.last),
            "avg_ms": r(self.avg),
            "best_ms": r(self.best),
            "worst_ms": r(self.worst),
            "stdev_ms": r(self.stdev),
        }


# The reply line has exactly one "=12ms" / "<1ms" / "= 12.3 ms" value. Statistics
# lines (Windows: "Minimum = 0ms, Maximum = 0ms, ...") have several and are skipped.
# Matching on the unit instead of the word "time" keeps this working on localised
# Windows ("czas=12ms", "Zeit=12ms", ...).
PING_RTT_RE = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms\b")


def ping_command(ip, timeout):
    system = platform.system()
    ms = int(timeout * 1000)
    if system == "Windows":
        return ["ping", f"-{ip.version}", "-n", "1", "-w", str(ms), str(ip)]
    if system == "Darwin":
        if ip.version == 6:
            return ["ping6", "-c", "1", str(ip)]
        return ["ping", "-c", "1", "-W", str(ms), str(ip)]
    return ["ping", f"-{ip.version}", "-c", "1", "-W", str(max(1, math.ceil(timeout))), str(ip)]


def parse_ping_rtt(output):
    for line in output.splitlines():
        found = PING_RTT_RE.findall(line)
        if len(found) == 1:
            return float(found[0].replace(",", "."))
    return None


def ping_once(ip, timeout=1.0):
    """Send one echo request to ip; return the RTT in ms, or None if it got no answer."""
    try:
        result = subprocess.run(
            ping_command(ip, timeout), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout + 3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return parse_ping_rtt(result.stdout)


def run_mtr(hops, count, interval, ping_timeout, on_round, ping=ping_once):
    """
    Ping every discovered hop once per round and keep running statistics.

    Stops after `count` rounds (0 = keep going until Ctrl+C).
    Returns (stats_per_hop, rounds_done, interrupted).
    """
    targets = [ipaddress.ip_address(h["ips"][0]["ip"]) if h["ips"] else None for h in hops]
    stats = [HopStats() for _ in hops]
    live = [i for i, t in enumerate(targets) if t is not None]
    rounds = 0
    if not live:
        return stats, rounds, False

    try:
        with ThreadPoolExecutor(max_workers=len(live)) as pool:
            while True:
                started = time.monotonic()
                results = list(pool.map(lambda i: ping(targets[i], ping_timeout), live))
                for i, rtt in zip(live, results):
                    stats[i].add(rtt)
                rounds += 1
                on_round(stats, rounds)
                if count and rounds >= count:
                    break
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return stats, rounds, True
    return stats, rounds, False


def render_mtr(hops, stats, rounds, pal, ip_width, target, target_ip):
    def num(v, style=None):
        return pal(("-" if v is None else f"{v:.1f}").rjust(7), style)

    lines = [
        f"{pal('Live trace', 'bold')} to {pal(target, 'bold')} ({target_ip})"
        f"   round {rounds}   Ctrl+C to quit",
        pal(f"{'':>3}  {'Host':<{ip_width}}  {'Loss%':>6} {'Snt':>4} {'Last':>7} {'Avg':>7} "
            f"{'Best':>7} {'Wrst':>7} {'StDev':>7}  Network", "dim"),
    ]
    for h, s in zip(hops, stats):
        n = pal(f"{h['hop']:>3}", "dim")
        if not h["ips"]:
            lines.append(f"{n}  {pal('???', 'dim')}")
            continue

        info = h["ips"][0]
        loss_txt = f"{s.loss_pct:.1f}%".rjust(6)
        loss = pal(loss_txt, "red" if s.loss_pct else "green")
        avg_style = pal.latency_style(s.avg) if s.avg is not None else None
        lines.append(
            f"{n}  {info['ip'].ljust(ip_width)}  {loss} {s.sent:>4} "
            f"{num(s.last)} {num(s.avg, avg_style)} {num(s.best)} {num(s.worst)} "
            f"{num(s.stdev)}  {geo_and_host(info, pal)}"
        )
    lines.append(pal("Pings go straight to each hop's address. Many routers answer them slowly "
                     "or not at all, so loss on a middle hop that disappears further on "
                     "is usually harmless.", "dim"))
    return lines


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def do_trace(args, target_ip, db, resolver, pal):
    ip_width = 15 if target_ip.version == 4 else 39
    writer = None

    if args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(TRACE_CSV_HEADER)
    elif not args.json:
        print(f"Traceroute to {pal(args.target, 'bold')} ({target_ip}), "
              f"IPv{target_ip.version}, max {args.max_hops} hops\n")

    def on_hop(h):
        if writer:
            writer.writerows(trace_csv_rows(h))
            sys.stdout.flush()
        elif not args.json:
            print_hop(h, pal, ip_width)

    hops, interrupted = run_traceroute(target_ip, args.max_hops, db, resolver, on_hop)
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
    elif not args.csv:
        if interrupted:
            print("\nInterrupted.")
        print_summary(summary, pal)


def do_mtr(args, target_ip, db, resolver, pal):
    quiet = args.json or args.csv           # machine-readable: only the final result
    live = sys.stdout.isatty() and not quiet  # redraw the screen each round
    ip_width = 15 if target_ip.version == 4 else 39

    # 1) discover the route once
    if not quiet:
        print(f"Discovering route to {args.target} ({target_ip}) ...", file=sys.stderr)

    def on_discovered(h):
        if not quiet:
            where = h["ips"][0]["ip"] if h["ips"] else "*"
            print(f"  hop {h['hop']:>2}: {where}", file=sys.stderr)

    hops, interrupted = run_traceroute(target_ip, args.max_hops, db, resolver, on_discovered)
    if interrupted:
        die("Interrupted during route discovery.")
    if not any(h["ips"] for h in hops):
        die("No hop answered the traceroute, so there is nothing to measure.")

    # 2) ping every hop, round after round
    first_frame = [True]

    def on_round(stats, rounds):
        if not live:
            return
        lines = render_mtr(hops, stats, rounds, pal, ip_width, args.target, target_ip)
        clear = "\033[2J" if first_frame[0] else ""
        first_frame[0] = False
        sys.stdout.write(clear + "\033[H" + "\033[K\n".join(lines) + "\033[K\n\033[J")
        sys.stdout.flush()

    if live:
        enable_windows_ansi()
    stats, rounds, interrupted = run_mtr(
        hops, args.count, args.interval, args.ping_timeout, on_round)

    # 3) final output
    if args.json:
        out_hops = [{**h, "mtr": s.as_dict()} for h, s in zip(hops, stats)]
        print(json.dumps({
            "mode": "mtr",
            "target": args.target,
            "resolved_ip": str(target_ip),
            "ip_version": target_ip.version,
            "rounds": rounds,
            "interrupted": interrupted,
            "hops": out_hops,
        }, indent=2, ensure_ascii=False))
    elif args.csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(MTR_CSV_HEADER)
        for h, s in zip(hops, stats):
            writer.writerow(mtr_csv_row(h, s))
    elif live:
        print("\nStopped." if interrupted else "\nDone.")
    else:
        print("\n".join(render_mtr(hops, stats, rounds, pal, ip_width, args.target, target_ip)))


def main():
    p = argparse.ArgumentParser(description="Traceroute with GeoIP/ASN lookups per hop")
    p.add_argument("target", help="Hostname or IP address")
    fam = p.add_mutually_exclusive_group()
    fam.add_argument("-4", dest="family", action="store_const", const=4, help="force IPv4")
    fam.add_argument("-6", dest="family", action="store_const", const=6, help="force IPv6")
    p.add_argument("--max-hops", type=int, default=30)
    p.add_argument("--resolve", action="store_true",
                   help="look up the reverse-DNS hostname of every hop")

    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true",
                     help="print one JSON document instead of text")
    out.add_argument("--csv", action="store_true",
                     help="print CSV (one row per hop address) instead of text")
    p.add_argument("--no-color", action="store_true", help="disable colour output")

    mtr = p.add_argument_group("live mode")
    mtr.add_argument("--mtr", action="store_true",
                     help="live, refreshing MTR-style view with loss/avg/best/worst/stdev per hop")
    mtr.add_argument("--count", type=int, default=0, metavar="N",
                     help="stop --mtr after N rounds (default: run until Ctrl+C)")
    mtr.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                     help="seconds between --mtr rounds (default 1)")
    mtr.add_argument("--ping-timeout", type=float, default=1.0, metavar="SEC",
                     help="seconds to wait for each --mtr ping (default 1)")

    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="path to the config file (default: config.ini next to the script)")
    p.add_argument("--tables-v4", default=None,
                   help="comma-separated IPv4 tables, searched in order (overrides config)")
    p.add_argument("--tables-v6", default=None,
                   help="comma-separated IPv6 tables, searched in order (overrides config)")
    args = p.parse_args()

    if args.count < 0:
        p.error("--count must be 0 or more")
    if args.interval <= 0 or args.ping_timeout <= 0:
        p.error("--interval and --ping-timeout must be greater than 0")

    cfg = load_config(args.config)
    tables_v4 = args.tables_v4 or cfg["tables_v4"]
    tables_v6 = args.tables_v6 or cfg["tables_v6"]

    use_colour = colour_wanted(args)
    if use_colour:
        enable_windows_ansi()
    pal = Palette(use_colour)

    try:
        target_ip = resolve_target(args.target, args.family)
    except socket.gaierror:
        die(f"Cannot resolve {args.target}")

    try:
        db = GeoDB(cfg, tables_v4, tables_v6)
    except pymysql.MySQLError as e:
        die(f"Database connection failed: {e}")

    resolver = Resolver() if args.resolve else None

    try:
        if args.mtr:
            do_mtr(args, target_ip, db, resolver, pal)
        else:
            do_trace(args, target_ip, db, resolver, pal)
    finally:
        db.close()


if __name__ == "__main__":
    main()
