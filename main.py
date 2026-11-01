#!/usr/bin/env python3
"""
Traceroute with GeoIP/ASN lookup on every hop.

Runs the system traceroute (tracert on Windows, traceroute on Linux/macOS),
and for each hop IP looks it up in the MySQL `geoip` database
(tables: pl_ipv4 first, then all_ipv4).

Requires:  pip install pymysql
Usage:     python traceroute_geoip.py 8.8.8.8
           python traceroute_geoip.py example.com --max-hops 20
"""

import argparse
import ipaddress
import os
import platform
import re
import socket
import subprocess
import sys

import pymysql

# --- DB config (can be overridden with environment variables) ---------------
DB_HOST = os.getenv("GEOIP_DB_HOST", "") #databse host
DB_USER = os.getenv("GEOIP_DB_USER", "") #database user
DB_PASS = os.getenv("GEOIP_DB_PASS", "") #database password
DB_NAME = os.getenv("GEOIP_DB_NAME", "geoip")

# Searched in this order; first match wins (pl_ipv4 is the smaller, local table)
TABLES = ("pl_ipv4", "all_ipv4")

HOP_LINE = re.compile(r"^\s*(\d+)\s")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def connect():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=True,
    )


def lookup(conn, ip):
    """Return the most specific matching row for an IP, or None."""
    ip_int = int(ipaddress.IPv4Address(ip))
    for table in TABLES:
        sql = (
            f"SELECT cidr, asn, company, prefix_country, as_country "
            f"FROM `{table}` "
            f"WHERE %s BETWEEN ip_start AND ip_end "
            f"ORDER BY (ip_end - ip_start) ASC LIMIT 1"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (ip_int,))
            row = cur.fetchone()
        if row:
            row["table"] = table
            return row
    return None


def describe(conn, ip):
    addr = ipaddress.IPv4Address(ip)
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return "private / local network"
    if addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return "CGNAT (shared address space)"

    row = lookup(conn, ip)
    if not row:
        return "no match in database"

    parts = [row["cidr"]]
    if row["asn"]:
        parts.append(f"AS{row['asn']}")
    if row["company"]:
        parts.append(row["company"])
    country = row["prefix_country"] or "??"
    if row["as_country"] and row["as_country"] != country:
        country += f" (AS: {row['as_country']})"
    parts.append(country)
    return " | ".join(parts)


def build_command(target, max_hops):
    if platform.system() == "Windows":
        return ["tracert", "-d", "-h", str(max_hops), target]
    return ["traceroute", "-n", "-m", str(max_hops), target]


def main():
    parser = argparse.ArgumentParser(description="Traceroute with GeoIP DB lookups")
    parser.add_argument("target", help="Hostname or IPv4 address")
    parser.add_argument("--max-hops", type=int, default=30)
    args = parser.parse_args()

    try:
        target_ip = socket.gethostbyname(args.target)
    except socket.gaierror:
        sys.exit(f"Cannot resolve {args.target}")

    try:
        conn = connect()
    except pymysql.MySQLError as e:
        sys.exit(f"Database connection failed: {e}")

    print(f"Traceroute to {args.target} ({target_ip}), max {args.max_hops} hops\n")

    try:
        proc = subprocess.Popen(
            build_command(args.target, args.max_hops),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        sys.exit("traceroute/tracert not found. On Debian/Ubuntu: sudo apt install traceroute")

    try:
        for line in proc.stdout:
            m = HOP_LINE.match(line)
            if not m:
                continue  # header / blank lines
            hop = m.group(1)
            # Drop the hop number so it isn't mistaken for part of an IP
            ips = list(dict.fromkeys(IPV4_RE.findall(line[m.end():])))

            if not ips:
                print(f"{hop:>3}  *  (no reply)")
                continue

            for ip in ips:
                try:
                    info = describe(conn, ip)
                except pymysql.MySQLError as e:
                    conn.ping(reconnect=True)
                    info = f"DB error: {e}"
                print(f"{hop:>3}  {ip:<15}  {info}")
    except KeyboardInterrupt:
        proc.terminate()
        print("\nInterrupted.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
