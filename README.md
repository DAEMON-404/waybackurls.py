```
                       _                _              _
 __      ____ _ _   _| |__   __ _  ___| | ___   _ _ __| |___
 \ \ /\ / / _` | | | | '_ \ / _` |/ __| |/ / | | | '__| / __|
  \ V  V / (_| | |_| | |_) | (_| | (__|   <| |_| | |  | \__ \
   \_/\_/ \__,_|\__, |_.__/ \__,_|\___|_|\_\\__,_|_|  |_|___/
                |___/          wayback machine url harvester
```

<div align="center">

**Pull every URL the Wayback Machine ever saw for a host — fast, threaded, and rate-limit aware.**

![python](https://img.shields.io/badge/python-3.8%2B-blue)
![uv](https://img.shields.io/badge/run%20with-uv-de5fe9)
![license](https://img.shields.io/badge/license-MIT-green)

</div>

---

## What it does

`waybackurls.py` queries the [Wayback Machine CDX API](https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md) and dumps every archived URL it has for one or more hosts. Handy for recon, OSINT, and bug-bounty scoping.

## Why v2

The original script broke against a live API: the Internet Archive now returns **HTTP 429 (Too Many Requests)** to clients that don't identify themselves or that hammer the endpoint. It isn't dead — it's throttling.

v2 fixes exactly that:

- **Sends a real `User-Agent`** so requests aren't rejected on sight.
- **Exponential backoff + retry on 429/5xx**, honoring the server's `Retry-After` header (via `urllib3`'s `Retry` adapter).
- **Reuses one `requests.Session`** across threads (connection pooling).
- **Request timeouts** so a hung connection can't stall the run.
- **Runs with [uv](https://docs.astral.sh/uv/)** — zero manual setup.

## Features

- 🔍 Retrieve archived URLs for multiple hosts concurrently
- 🌐 Include or exclude subdomains (`-s`)
- 📁 Custom output directory (`-o`)
- 🚀 Adjustable thread count (`-t`)
- 🛡️ Configurable retry budget for throttling (`-r`)
- 🕰️ Human-readable timestamps in the output
- 🎬 Animated banner + live status bar (spinner, host / URL counters)
- 📡 Verbose mode (`-v`) streams every URL live as archive.org returns it
- 🔗 Clickable Wayback **snapshot links** alongside each original URL
- 🎨 Color auto-detects the terminal; `--no-color` / `NO_COLOR` to disable

## Install & Run

### Option A — uv (recommended, no setup)

[uv](https://docs.astral.sh/uv/getting-started/installation/) reads the inline dependency metadata at the top of the script and builds a throwaway environment for you:

```bash
uv run waybackurls.py example.com
```

That's it — no `pip install`, no virtualenv to manage.

### Option B — install as a global command

```bash
uv tool install .
waybackurls example.com
```

### Option C — classic pip

```bash
pip install -r requirements.txt
python waybackurls.py example.com
```

## Usage

```
waybackurls [-h] [-s] [-o OUTPUT] [-t THREADS] [-r RETRIES] [-l LIMIT] [--no-collapse] [-v] [--no-color] [-q] hosts [hosts ...]
```

| Flag | Description | Default |
|------|-------------|---------|
| `hosts` | One or more hosts to retrieve URLs for (required) | — |
| `-s`, `--subdomains` | Include subdomains in the search | off |
| `-o`, `--output` | Output directory | `results` |
| `-t`, `--threads` | Number of concurrent threads | `5` |
| `-r`, `--retries` | Max retries on throttling (429/5xx) | `5` |
| `-l`, `--limit` | Cap results per host (`0` = all). Small value = fast connectivity test | `0` |
| `--no-collapse` | Don't dedupe captures server-side (helps if a huge host hangs) | off |
| `-v`, `--verbose` | Print every URL live as it streams in from archive.org | off |
| `--no-color` | Disable colored / animated output | off |
| `-q`, `--quiet` | Hide the ASCII banner | off |

### Examples

```bash
# Fast connectivity test (returns quickly, proves it works)
uv run waybackurls.py --limit 50 example.com

# Single host
uv run waybackurls.py example.com

# Multiple hosts
uv run waybackurls.py example.com example.org

# Include subdomains
uv run waybackurls.py -s example.com

# Watch every URL stream in live
uv run waybackurls.py -v example.com

# Custom output dir + more threads
uv run waybackurls.py -o loot/example -t 10 example.com

# Huge host that hangs? Drop server-side dedupe
uv run waybackurls.py --no-collapse example.com
```

## Troubleshooting

### It hangs, then returns nothing / "rate limited by archive.org"

The Internet Archive **aggressively rate-limits the CDX API by network**, and it up front returns `HTTP 429` with a `x-rl: 0` (quota exhausted) header.

**The single biggest cause is running through a VPN, cloud host, or datacenter IP.** archive.org gives those ranges near-zero quota. If you see `x-nid: M247 …` (or any hosting provider) in the response headers, that's the tell.

Fixes, in order of effectiveness:

1. **Turn the VPN off.** Run from a residential/home IP. This resolves it the vast majority of the time.
2. **Start small:** `--limit 50` returns quickly and confirms connectivity before you pull a whole domain.
3. **Back off harder:** `-r 10 -t 1` — more retries, one thread, so the exponential backoff (which honors the server's `Retry-After`) has room to work.
4. **Big domain hanging** (not throttled, just slow)? Add `--no-collapse` — the `collapse=urlkey` dedupe is expensive server-side on domains with huge histories.

archive.org being up (`archive.org` loads in a browser) does **not** mean the CDX API will serve *your* IP — the rate limiter is per-network and independent of the main site.

## Output

Results are written as text files in the output directory, one per host:

```
results/example.com-waybackurls.txt
```

Each line is an archive timestamp (ISO 8601, UTC), the original URL, and a
clickable Wayback **snapshot link** that opens the archived copy:

```
2015-03-12T08:41:10Z - http://example.com/old/login.php - https://web.archive.org/web/20150312084110/http://example.com/old/login.php
2018-11-02T22:07:55Z - http://example.com/assets/app.js - https://web.archive.org/web/20181102220755/http://example.com/assets/app.js
```

The middle field stays pipe-friendly (feed it to other recon tools); the third
field is the one you click to see what the page actually looked like.

## Original Creator 🙌

Based on the work of [mhmdiaa](https://github.com/mhmdiaa) — original gist [here](https://gist.github.com/mhmdiaa/adf6bff70142e5091792841d4b372050).

## Contributing 🤝

Issues and pull requests welcome.

## License ⚖️

[MIT](LICENSE)
