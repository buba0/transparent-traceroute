# 🌍 traceroute-geoip

**Traceroute, but it tells you *who owns* every router along the way, and how fast each one answers.**

A plain `traceroute` gives you a wall of IP addresses and leaves you to guess what they are. This tool runs the traceroute for you, then looks up every hop in your own local MySQL database and tells you the network, ASN, company and country behind it, plus latency and packet loss per hop. It speaks IPv4 and IPv6, uses colour so problems jump out, and has a `--json` mode for scripts. No third-party API, no rate limits, no sending your lookups to someone else's server.

```
Traceroute to google.com (142.250.74.14), IPv4, max 30 hops

  1  192.168.1.1         1.0 ms  (1.0-1.0)         private / local network
  2  10.20.0.1           3.2 ms  (2.9-3.6)         private / local network
  3  83.238.12.1        11.8 ms  (11.1-12.9)       83.238.0.0/16 | AS5617 | Orange Polska Spolka Akcyjna | PL
  4  72.14.213.30       24.3 ms  (23.8-25.1)       72.14.192.0/18 | AS15169 | GOOGLE - Google LLC | US
  5  142.250.74.14      24.9 ms  (24.1-26.0)       142.250.0.0/15 | AS15169 | GOOGLE - Google LLC | US

Summary
  Destination reached : yes (5 hops, 5 responded)
  Slowest hop         : #5 (24.9 ms)
  Biggest latency jump: #2 -> #3 (+8.6 ms)
  Countries           : PL -> US
  Networks            : AS5617 Orange Polska Spolka Akcyjna -> AS15169 GOOGLE - Google LLC
```

*(Example output. Yours will depend on your route and your data. In a real terminal latency is colour-coded: green under 30 ms, yellow under 100 ms, red above.)*

---

## 🤔 Who is this for?

- **Network admins and homelabbers** who want to see at a glance which ISP or carrier is dropping packets.
- **Sysadmins debugging "the internet is slow"**: is it your ISP, their upstream, or the destination's hosting provider?
- **Security folks and the curious** who want to know which countries and companies their traffic passes through.
- **Anyone who already has an IP-to-ASN database** and wants something fun to do with it.
- **Students learning networking.** It's a small, readable script that shows how traceroute, CIDR ranges and ASNs fit together.

If you just want a quick one-off lookup, a web tool is easier. If you want to run it offline, run it a lot, or run it on private infrastructure, this is for you.

---

## ✨ Features

- 🌐 **IPv4 and IPv6**: auto-detects from the target, or force it with `-4` / `-6`.
- 🎨 **Colour output**: latency is green / yellow / red, ASNs and countries stand out, private hops fade into the background. Auto-disabled when piped, or with `--no-color` / the `NO_COLOR` environment variable.
- 🧾 **`--json` mode**: one clean JSON document with every hop and a summary, ready for `jq`, scripts or dashboards.
- ⏱ **Per-hop latency and loss**: min / avg / max RTT and packet loss for every hop.
- 📋 **Summary at the end**: whether the destination was reached, the slowest hop, the biggest latency jump between hops, which hops lost packets, and the path of countries and networks your traffic crossed.
- 🖥 Wraps the system traceroute, so it works on **Windows** (`tracert`) and **Linux/macOS** (`traceroute`) without admin or root.
- ⚡ **Streams results hop by hop** as they arrive instead of waiting for the whole trace.
- 🔎 Fast **integer range lookups**, even with hundreds of thousands of rows.
- 🎯 Always picks the **most specific matching range** when ranges overlap.
- 🥇 **Two-tier lookup**: a small, local table (e.g. your own country) is checked first, then a big global one. The table list is configurable.
- 🏠 Understands **private, loopback, link-local and CGNAT** addresses and labels them instead of reporting "unknown".
- 🔀 Shows when a prefix's country and its AS's country **differ**, which is common with anycast and international carriers.

---

## 🚀 Quick start

### 1. Requirements

- Python 3.8+
- A MySQL / MariaDB server with your GeoIP data (see below)
- `traceroute` installed on Linux (`sudo apt install traceroute`); Windows has `tracert` built in

```bash
pip install pymysql
# On MySQL 8 with caching_sha2_password you may also need:
pip install cryptography
```

### 2. Set up the database

The tool expects a database called `geoip`. IPv4 and IPv6 each get one or more tables, with the same columns but different types for the address range.

**IPv4 tables** (`all_ipv4`, optionally `pl_ipv4`, ...): addresses stored as integers.

```sql
CREATE TABLE all_ipv4 (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cidr           VARCHAR(18)  NOT NULL,
  asn            INT UNSIGNED NULL,
  company        VARCHAR(255) NULL,
  prefix_country CHAR(2)      NULL,
  as_country     CHAR(2)      NULL,
  ip_start       INT UNSIGNED NOT NULL,   -- first address as an integer
  ip_end         INT UNSIGNED NOT NULL,   -- last address as an integer
  INDEX idx_range (ip_start, ip_end)
) CHARACTER SET utf8mb4;
```

**IPv6 tables** (`all_ipv6`, optionally `pl_ipv6`, ...): MySQL has no 128-bit integer, so addresses are stored as 16 raw bytes (`BINARY(16)`), the same format `INET6_ATON()` produces.

```sql
CREATE TABLE all_ipv6 (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cidr           VARCHAR(43)  NOT NULL,
  asn            INT UNSIGNED NULL,
  company        VARCHAR(255) NULL,
  prefix_country CHAR(2)      NULL,
  as_country     CHAR(2)      NULL,
  ip_start       BINARY(16)   NOT NULL,   -- first address, INET6_ATON() format
  ip_end         BINARY(16)   NOT NULL,   -- last address, INET6_ATON() format
  INDEX idx_range (ip_start, ip_end)
) CHARACTER SET utf8mb4;
```

| Column | Example | Meaning |
|---|---|---|
| `cidr` | `1.0.0.0/24` | The network prefix |
| `asn` | `13335` | Autonomous System Number |
| `company` | `CLOUDFLARENET - Cloudflare, Inc.` | Who runs that ASN |
| `prefix_country` | `AU` | Country the prefix is registered/located in |
| `as_country` | `US` | Country of the AS |
| `ip_start` / `ip_end` | `16777216` / `16777471` | The range (integer for IPv4, 16 bytes for IPv6) |

IPv6 is optional. If your database has no IPv6 tables, IPv4 traces work as before and IPv6 hops are shown as *"no IPv6 table in database"*.

Fill the tables from whichever IP-to-ASN / GeoIP dataset you prefer, and check its licence before you redistribute any data. The easiest way to compute the range columns is in Python:

```python
import ipaddress

net = ipaddress.ip_network("2a00:1450::/32")
start = net.network_address.packed      # 16 bytes -> ip_start (IPv6)
end   = net.broadcast_address.packed    # 16 bytes -> ip_end   (IPv6)

net4 = ipaddress.ip_network("1.0.0.0/24")
start4, end4 = int(net4.network_address), int(net4.broadcast_address)   # integers (IPv4)
```

> 💡 In plain SQL, `INET_ATON('1.0.0.0')` gives the IPv4 integer and `INET6_ATON('2a00:1450::')` gives the IPv6 bytes.

### 3. Configure

Your database details live in a separate `config.ini` file next to the script. It is listed in `.gitignore`, so your credentials are never committed. The repo only contains `config.example.ini`, a template with placeholder values.

```bash
# Linux / macOS
cp config.example.ini config.ini
```

```powershell
# Windows (PowerShell)
Copy-Item config.example.ini config.ini
```

Then open `config.ini` and fill it in:

```ini
[database]
host = 10.0.0.5
port = 3306
user = geoip_reader
password = your-password
name = geoip

[tables]
ipv4 = pl_ipv4,all_ipv4
ipv6 = pl_ipv6,all_ipv6
```

| Setting | Default | Description |
|---|---|---|
| `host` | `127.0.0.1` | Database host |
| `port` | `3306` | Database port |
| `user` | *(required)* | Database user |
| `password` | empty | Database password. A `%` or `#` in it is fine, no escaping needed |
| `name` | `geoip` | Database name |
| `ipv4` / `ipv6` (`[tables]`) | `pl_ipv4,all_ipv4` / `pl_ipv6,all_ipv6` | Tables searched in order, first match wins |

Want the config somewhere else? Use `--config /path/to/file.ini`.

> ⚠️ **Already committed a `config.ini` by mistake?** Adding it to `.gitignore` doesn't remove it from git history. Run `git rm --cached config.ini`, commit, and **change the database password**, because the old one is still visible in earlier commits.

The tool only ever reads data, so give it a **read-only user**:

```sql
CREATE USER 'geoip_reader'@'%' IDENTIFIED BY 'your-password';
GRANT SELECT ON geoip.* TO 'geoip_reader'@'%';
```

### 4. Run it

```bash
python traceroute_geoip.py google.com
python traceroute_geoip.py 1.1.1.1 --max-hops 20
python traceroute_geoip.py ipv6.google.com -6
python traceroute_geoip.py example.com --json > trace.json
```

Press `Ctrl+C` to stop early; you still get the summary for the hops collected so far.

### Command-line options

| Option | Description |
|---|---|
| `target` | Hostname or IP address |
| `-4` / `-6` | Force IPv4 or IPv6 (default: auto, preferring IPv4 if a name has both) |
| `--max-hops N` | Maximum number of hops (default 30) |
| `--json` | Print a single JSON document instead of text |
| `--no-color` | Disable colour (also honours the `NO_COLOR` environment variable) |
| `--config PATH` | Use a config file other than `config.ini` next to the script |
| `--tables-v4 a,b` | IPv4 tables to search, in order (overrides `[tables]` in the config) |
| `--tables-v6 a,b` | IPv6 tables to search, in order (overrides `[tables]` in the config) |

---

## 📊 Reading the output

Each hop line shows:

```
  4  72.14.213.30       24.3 ms  (23.8-25.1)       72.14.192.0/18 | AS15169 | GOOGLE - Google LLC | US
 hop     address        avg      (min-max)         prefix | ASN | company | country
```

- If a hop lost some of its probes you'll see a red `33% loss` at the end of the line.
- `*  (no reply)` means the router didn't answer at all. That's very common and often harmless; many routers ignore or rate-limit traceroute probes.
- If the prefix's country differs from the AS's country it shows as `AU (AS: US)`.
- If a hop answers from several addresses (load balancing), the extra addresses are listed beneath it with a `↳`. The latency shown is aggregated over all probes for that hop.

**A word on latency and loss:** the numbers are the round-trip time of the probe to *that router*. A slow or lossy hop in the middle with a healthy final hop is usually just the router deprioritising traceroute replies, not a real problem. Real trouble is latency or loss that **starts at one hop and continues on every hop after it**.

### JSON output

`--json` prints one document, with colour off and nothing else on stdout:

```json
{
  "target": "google.com",
  "resolved_ip": "142.250.74.14",
  "ip_version": 4,
  "interrupted": false,
  "hops": [
    {
      "hop": 4,
      "ips": [
        {
          "ip": "72.14.213.30",
          "version": 4,
          "kind": "match",
          "cidr": "72.14.192.0/18",
          "asn": 15169,
          "company": "GOOGLE - Google LLC",
          "prefix_country": "US",
          "as_country": "US",
          "table": "all_ipv4"
        }
      ],
      "probes_sent": 3,
      "timeouts": 0,
      "loss_pct": 0.0,
      "rtt_ms": { "min": 23.8, "avg": 24.3, "max": 25.1 }
    }
  ],
  "summary": {
    "destination_reached": true,
    "total_hops": 5,
    "responding_hops": 5,
    "slowest_hop": { "hop": 5, "avg_ms": 24.9 },
    "biggest_latency_jump": { "from_hop": 2, "to_hop": 3, "delta_ms": 8.6 },
    "hops_with_loss": [],
    "countries_path": ["PL", "US"],
    "networks_path": [
      { "asn": 5617, "company": "Orange Polska Spolka Akcyjna" },
      { "asn": 15169, "company": "GOOGLE - Google LLC" }
    ]
  }
}
```

The `kind` field is one of `match`, `private`, `loopback`, `link-local`, `cgnat` or `unknown`. Handy one-liners:

```bash
# Just the list of ASNs on the path
python traceroute_geoip.py example.com --json | jq -r '.summary.networks_path[] | "AS\(.asn) \(.company)"'

# Hops slower than 100 ms
python traceroute_geoip.py example.com --json | jq '.hops[] | select(.rtt_ms.avg > 100) | .hop'
```

---

## 🛠 How it works

1. Resolves the target hostname to an IPv4 or IPv6 address.
2. Launches `tracert -d` or `traceroute -n` (no reverse DNS, which keeps it fast).
3. Reads the output line by line and pulls the IPs, round-trip times and `*` timeouts out of each hop.
4. Looks up each IP. For IPv4 it converts the address to an integer and runs:

   ```sql
   SELECT cidr, asn, company, prefix_country, as_country
   FROM all_ipv4
   WHERE <ip_as_int> BETWEEN ip_start AND ip_end
   ORDER BY (ip_end - ip_start) ASC
   LIMIT 1;
   ```

   The `ORDER BY` picks the narrowest range, which is the most specific match, the same way real routing works. For IPv6 the address is compared as 16 raw bytes, and because CIDR ranges nest, the most specific range is simply the one with the greatest `ip_start`.
5. Prints the result next to the hop, then a summary once the trace finishes.

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| `Config file not found` | Copy `config.example.ini` to `config.ini` (next to the script) and fill it in, or pass `--config`. |
| `No database user set` / `Problem reading config` | `config.ini` needs a `[database]` section with at least `user`. Compare it with `config.example.ini`. |
| `Can't connect to MySQL server ... actively refused` | MySQL isn't listening on that address/port. Check `bind-address` in `my.ini` / `my.cnf`, and that port 3306 is open in the firewall. |
| `Access denied for user` | The MySQL user may be limited to `localhost`. Create it for the host you connect from (`'user'@'%'` or your subnet). |
| `cryptography package is required` | `pip install cryptography` |
| `traceroute/tracert not found` | Install it: `sudo apt install traceroute` (Debian/Ubuntu). |
| Everything shows `no match in database` | Check that your tables are filled and that `ip_start`/`ip_end` use the right types (integers for IPv4, `BINARY(16)` for IPv6). |
| IPv6 hops show `no IPv6 table in database` | Create an `all_ipv6` table (see above), or point `--tables-v6` at your table names. |
| IPv6 traces fail or show only `*` | Your machine or network may not have working IPv6. Try `-4`. |
| Colours look like garbage (`←[32m`) | Use a modern terminal (Windows Terminal / PowerShell 7) or pass `--no-color`. |
| Lookups feel slow | Add an index on `(ip_start, ip_end)`. |

---

## 🗺 Ideas / roadmap

- Optional caching of repeated lookups
- Per-hop reverse DNS (`--resolve`)
- CSV output
- Live, refreshing "MTR-style" mode

PRs and ideas are welcome. 🎉

---

## 🔒 A note on privacy and security

- Lookups happen against **your own database**, so the IPs you trace never leave your network (apart from the traceroute packets themselves, of course).
- Use a **read-only** database user and restrict who can reach port 3306.
- Credentials live only in `config.ini`, which is git-ignored. Never commit it, and never put real values in `config.example.ini`.

---

## 📄 License

Pick a licence (MIT is a popular choice for small tools like this) and add a `LICENSE` file.

---

*Made because "what is 72.14.213.30 and why is it slow?" shouldn't need five browser tabs.*