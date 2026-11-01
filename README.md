# 🌍 traceroute-geoip

**Traceroute, but it tells you *who owns* every router along the way, and how fast each one answers.**

A plain `traceroute` gives you a wall of IP addresses and leaves you to guess what they are. This tool runs the traceroute for you, then looks up every hop in your own local MySQL database and tells you the network, ASN, company and country behind it, plus latency and packet loss per hop. It speaks IPv4 and IPv6, uses colour so problems jump out, can look up reverse-DNS names, exports to `--json` or `--csv` for scripts and spreadsheets, and has a live, refreshing `--mtr` mode for watching a path over time. No third-party API, no rate limits, no sending your lookups to someone else's server.

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
- 📑 **`--csv` mode**: one row per hop address, streamed as the trace runs. Opens straight in Excel, LibreOffice or pandas.
- 🏷 **`--resolve`**: reverse-DNS hostname for every hop (`ae-1.r21.frnkge08.de.bb.gin.ntt.net` says a lot more than an IP).
- 📡 **`--mtr` live mode**: a refreshing, MTR-style table with loss %, last / avg / best / worst latency and standard deviation per hop, plus the same network info.
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
- `ping` (already present on Windows, Linux and macOS), only needed for `--mtr`

```bash
pip install pymysql
# On MySQL 8 with caching_sha2_password you may also need:
pip install cryptography
```

### 2. Set up the database

**Quickest way:** this repo ships `geoip.zip`, a phpMyAdmin dump containing `geoip.sql` with the `all_ipv4` and `pl_ipv4` tables. Unzip it, create the database, and import:

```bash
unzip geoip.zip
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS geoip CHARACTER SET utf8mb4"
mysql -u root -p geoip < geoip.sql
```

The dump already includes the `(ip_start, ip_end)` index, so lookups are fast straight away. It has no IPv6 tables; IPv6 hops show *"no IPv6 table in database"* until you add some (see below).

**Or build your own.** The tool expects a database called `geoip`. IPv4 and IPv6 each get one or more tables, with the same columns but different types for the address range.

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
python main.py google.com
python main.py 1.1.1.1 --max-hops 20
python main.py ipv6.google.com -6
python main.py example.com --resolve              # add reverse-DNS names
python main.py example.com --json > trace.json
python main.py example.com --csv > trace.csv
python main.py example.com --mtr                   # live view, Ctrl+C to quit
python main.py example.com --mtr --count 20 --csv > mtr.csv
```

Press `Ctrl+C` to stop early; you still get the summary for the hops collected so far.

### Command-line options

| Option | Description |
|---|---|
| `target` | Hostname or IP address |
| `-4` / `-6` | Force IPv4 or IPv6 (default: auto, preferring IPv4 if a name has both) |
| `--max-hops N` | Maximum number of hops (default 30) |
| `--resolve` | Look up the reverse-DNS hostname of every hop (2 s timeout per hop) |
| `--json` | Print a single JSON document instead of text |
| `--csv` | Print CSV (one row per hop address) instead of text. Can't be combined with `--json` |
| `--mtr` | Live, refreshing MTR-style view (see below) |
| `--count N` | With `--mtr`: stop after N rounds (default: run until `Ctrl+C`) |
| `--interval SEC` | With `--mtr`: seconds between rounds (default 1) |
| `--ping-timeout SEC` | With `--mtr`: how long to wait for each ping (default 1) |
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

The `kind` field is one of `match`, `private`, `loopback`, `link-local`, `cgnat` or `unknown`. With `--resolve`, each address also gets a `hostname` field (`null` when there is no reverse-DNS record). Handy one-liners:

```bash
# Just the list of ASNs on the path
python main.py example.com --json | jq -r '.summary.networks_path[] | "AS\(.asn) \(.company)"'

# Hops slower than 100 ms
python main.py example.com --json | jq '.hops[] | select(.rtt_ms.avg > 100) | .hop'
```

### CSV output

`--csv` writes a header and then one row per hop address, as each hop arrives. A hop that didn't answer gets a row with an empty `ip`; a hop that answered from several addresses (load balancing) gets one row per address, each repeating the hop's latency figures.

```
hop,ip,hostname,ip_version,kind,cidr,asn,company,prefix_country,as_country,probes_sent,timeouts,loss_pct,rtt_min_ms,rtt_avg_ms,rtt_max_ms
1,192.168.1.1,,4,private,,,,,,3,0,0.0,1.0,1.2,1.5
2,,,,,,,,,,3,3,100.0,,,
3,83.238.12.1,,4,match,83.238.0.0/16,5617,Orange Polska Spolka Akcyjna,PL,PL,3,0,0.0,11.1,11.8,12.9
```

Values containing commas or quotes (company names often do) are quoted properly, so the file loads cleanly in spreadsheets and `pandas.read_csv`.

### Reverse DNS (`--resolve`)

Add `--resolve` and every responding hop gets its PTR hostname appended to the line, in the text, JSON, CSV and `--mtr` outputs. Lookups for a hop run in parallel, give up after 2 seconds, and are cached, so one slow DNS server can't stall the trace. Hops with no PTR record simply show no name.

```
  4  72.14.213.30       24.3 ms  (23.8-25.1)       72.14.192.0/18 | AS15169 | GOOGLE - Google LLC | US  lhr48s30-in-f14.1e100.net
```

### Live mode (`--mtr`)

A one-shot traceroute is a snapshot. `--mtr` turns it into a live view that updates every second, like the classic `mtr` tool, so you can watch loss and jitter build up over time:

```
Live trace to example.com (93.184.216.34)   round 42   Ctrl+C to quit
     Host              Loss%  Snt    Last     Avg    Best    Wrst   StDev  Network
  1  192.168.1.1        0.0%   42     1.0     1.3     0.9     2.8     0.4  private / local network
  2  10.20.0.1          0.0%   42     3.1     3.3     2.9     5.0     0.5  private / local network
  3  83.238.12.1        2.4%   42    11.8    12.4    11.0    19.6     1.6  83.238.0.0/16 | AS5617 | Orange Polska Spolka Akcyjna | PL
  4  93.184.216.34      0.0%   42    24.3    24.6    23.8    31.2     1.1  93.184.216.0/24 | AS15133 | EDGECAST - Edgecast Inc. | US
```

*(Example output. Latency is colour-coded and loss turns red in a real terminal.)*

How it works: the route is discovered once with the normal traceroute, then every hop's address is pinged once per round, in parallel. For each hop you get loss %, packets sent, and last / average / best / worst latency and standard deviation, next to the usual network, ASN, company and country.

- Press `Ctrl+C` to stop. The last screen stays on your terminal.
- `--count N` stops after N rounds, which is handy for scripts.
- With `--json` or `--csv`, nothing is drawn live; you get one final document when the run ends (by `--count` or `Ctrl+C`). Progress messages go to stderr, so stdout stays clean.
- If stdout isn't a terminal (you piped it), only the final table is printed.
- Hops that never answered the initial traceroute show as `???` and aren't pinged.

**A caveat:** unlike plain traceroute, `--mtr` pings each router's own address directly. Many routers treat that differently from traffic passing *through* them (rate-limiting it or ignoring it), so loss on a middle hop that vanishes on the hops after it is usually harmless. The destination's numbers are the ones to trust.

---

## 🛠 How it works

1. Resolves the target hostname to an IPv4 or IPv6 address.
2. Launches `tracert -d` or `traceroute -n` (no reverse DNS by default, which keeps it fast; `--resolve` adds it back in a controlled, time-limited way).
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
6. In `--mtr` mode, it then pings each discovered hop once per round and keeps running statistics (Welford's algorithm for the standard deviation, so memory stays constant however long you leave it running).

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
| `--mtr`: "No hop answered the traceroute" | Nothing replied during route discovery. Try a different target, or check that your firewall allows traceroute. |
| `--mtr` shows 100% loss on a hop but later hops are fine | That router ignores direct pings. It's harmless; traffic still passes through it. |
| `--mtr` screen flickers or fills with escape codes | Use a modern terminal (Windows Terminal / PowerShell 7), or pipe to a file / use `--count` for a single final table. |
| `--resolve` is slow | Your DNS server is slow to answer PTR queries. Each hop waits at most 2 seconds, and results are cached. |
| Lookups feel slow | Add an index on `(ip_start, ip_end)`. |

---

## 🗺 Ideas / roadmap

- Optional caching of repeated lookups
- Probing each TTL directly in `--mtr` mode (so middle hops reflect real transit, not just ping replies)
- Saving an `--mtr` run's history for graphs
- Packaging (`pip install`) with a proper console script

PRs and ideas are welcome. 🎉

---

## 🔒 A note on privacy and security

- Network lookups happen against **your own database**, so the IPs you trace are never sent to a third-party API. The only traffic that leaves your machine is the traceroute/ping packets themselves and, if you use `--resolve`, ordinary reverse-DNS queries to whatever DNS server your system is configured to use.
- Use a **read-only** database user and restrict who can reach port 3306.
- Credentials live only in `config.ini`, which is git-ignored. Never commit it, and never put real values in `config.example.ini`.

---

## 📄 License

MIT. See [`LICENSE`](LICENSE). If you redistribute the data in `geoip.zip`, make sure the licence of the dataset it was built from allows that.

---

*Made because "what is 72.14.213.30 and why is it slow?" shouldn't need five browser tabs.*