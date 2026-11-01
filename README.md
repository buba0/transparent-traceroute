# 🌍 traceroute-geoip

**Traceroute, but it tells you *who owns* every router along the way.**

A plain `traceroute` gives you a wall of IP addresses and leaves you to guess what they are. This tool runs the traceroute for you, then looks up every hop in your own local MySQL database and tells you the network, ASN, company and country behind it. No third-party API, no rate limits, no sending your lookups to someone else's server.

```
Traceroute to google.com (142.250.74.14), max 30 hops

  1  192.168.1.1      private / local network
  2  10.20.0.1        private / local network
  3  83.238.12.1      83.238.0.0/16 | AS5617 | Orange Polska Spolka Akcyjna | PL
  4  72.14.213.30     72.14.192.0/18 | AS15169 | GOOGLE - Google LLC | US
  5  142.250.74.14    142.250.0.0/15 | AS15169 | GOOGLE - Google LLC | US
```

*(Example output. Yours will depend on your route and your data.)*

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

- Wraps the system traceroute, so it works on **Windows** (`tracert`) and **Linux/macOS** (`traceroute`) without admin or root.
- **Streams results hop by hop** as they arrive instead of waiting for the whole trace.
- Looks up each hop with an **integer range query**, so it stays fast even with hundreds of thousands of rows.
- Always picks the **most specific matching range** when ranges overlap.
- Supports a **two-tier lookup**: a small, local table (e.g. your own country) is checked first, then a big global one.
- Understands **private, loopback and CGNAT** addresses and labels them instead of reporting "unknown".
- Shows when a prefix's country and its AS's country **differ**, which is common with anycast and international carriers.

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

The tool expects a database called `geoip` with one or more tables shaped like this:

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

| Column | Example | Meaning |
|---|---|---|
| `cidr` | `1.0.0.0/24` | The network prefix |
| `asn` | `13335` | Autonomous System Number |
| `company` | `CLOUDFLARENET - Cloudflare, Inc.` | Who runs that ASN |
| `prefix_country` | `AU` | Country the prefix is registered/located in |
| `as_country` | `US` | Country of the AS |
| `ip_start` / `ip_end` | `16777216` / `16777471` | The range as integers (`1.0.0.0` = 16777216) |

You can create a second table with the same layout called `pl_ipv4`. It's checked first, so you can put a smaller, more detailed dataset there (the table list is configurable, see below). Fill the tables from whichever IP-to-ASN / GeoIP dataset you prefer, and check its licence before you redistribute any data.

> 💡 Tip: `INET_ATON('1.0.0.0')` in MySQL gives you the integer form of an address, so you can compute `ip_start` and `ip_end` straight from your CIDRs.

### 3. Configure

Connection details are read from environment variables:

| Variable | Default | Description |
|---|---|---|
| `GEOIP_DB_HOST` | `127.0.0.1` | Database host |
| `GEOIP_DB_USER` | *(required)* | Database user |
| `GEOIP_DB_PASS` | *(required)* | Database password |
| `GEOIP_DB_NAME` | `geoip` | Database name |

```powershell
# Windows (PowerShell)
$env:GEOIP_DB_HOST = "10.0.0.5"
$env:GEOIP_DB_USER = "geoip_reader"
$env:GEOIP_DB_PASS = "your-password"
```

```bash
# Linux / macOS
export GEOIP_DB_HOST=10.0.0.5
export GEOIP_DB_USER=geoip_reader
export GEOIP_DB_PASS='your-password'
```

The tool only ever reads data, so give it a **read-only user**:

```sql
CREATE USER 'geoip_reader'@'%' IDENTIFIED BY 'your-password';
GRANT SELECT ON geoip.* TO 'geoip_reader'@'%';
```

### 4. Run it

```bash
python traceroute_geoip.py google.com
python traceroute_geoip.py 1.1.1.1 --max-hops 20
```

Press `Ctrl+C` to stop early.

---

## 🛠 How it works

1. Resolves the target hostname to an IPv4 address.
2. Launches `tracert -d` or `traceroute -n` (no reverse DNS, which keeps it fast).
3. Reads the output line by line and pulls the IPs out of each hop.
4. Converts each IP to an integer and runs:

   ```sql
   SELECT cidr, asn, company, prefix_country, as_country
   FROM all_ipv4
   WHERE <ip_as_int> BETWEEN ip_start AND ip_end
   ORDER BY (ip_end - ip_start) ASC
   LIMIT 1;
   ```

   The `ORDER BY` picks the narrowest range, which is the most specific match, the same way real routing works.
5. Prints the result next to the hop.

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| `Can't connect to MySQL server ... actively refused` | MySQL isn't listening on that address/port. Check `bind-address` in `my.ini` / `my.cnf`, and that port 3306 is open in the firewall. |
| `Access denied for user` | The MySQL user may be limited to `localhost`. Create it for the host you connect from (`'user'@'%'` or your subnet). |
| `cryptography package is required` | `pip install cryptography` |
| `traceroute/tracert not found` | Install it: `sudo apt install traceroute` (Debian/Ubuntu). |
| Everything shows `no match in database` | Check that your tables are filled and that `ip_start`/`ip_end` are stored as integers, not strings. |
| Lookups feel slow | Add an index on `(ip_start, ip_end)`. |

---

## 🗺 Ideas / roadmap

- IPv6 support
- Colour output and a `--json` mode
- Per-hop latency summary
- Optional caching of repeated lookups
- Configurable table list via CLI flag

PRs and ideas are welcome. 🎉

---

## 🔒 A note on privacy and security

- Lookups happen against **your own database**, so the IPs you trace never leave your network (apart from the traceroute packets themselves, of course).
- Use a **read-only** database user and restrict who can reach port 3306.
- Never commit real credentials to the repo. Use environment variables.

---

## 📄 License

Released under the [MIT License](LICENSE).

---

*Made because "what is 72.14.213.30 and why is it slow?" shouldn't need five browser tabs.*
