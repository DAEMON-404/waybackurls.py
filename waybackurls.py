#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "requests>=2.31",
# ]
# ///
# Author: Leif R Bruce ~/00xNetrunner
# Description: Waybackurls - Retrieve URLs from the Wayback Machine for multiple hosts.
#
# Run with uv (no install needed):
#     uv run waybackurls.py example.com
# Or install as a command:
#     uv tool install .

import argparse
import concurrent.futures
import itertools
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Iterator, List, Tuple

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

BANNER = r"""
                       _                _              _
 __      ____ _ _   _| |__   __ _  ___| | ___   _ _ __| |___
 \ \ /\ / / _` | | | | '_ \ / _` |/ __| |/ / | | | '__| / __|
  \ V  V / (_| | |_| | |_) | (_| | (__|   <| |_| | |  | \__ \
   \_/\_/ \__,_|\__, |_.__/ \__,_|\___|_|\_\\__,_|_|  |_|___/
                |___/          wayback machine url harvester
"""

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
WAYBACK_SNAPSHOT = "https://web.archive.org/web/{timestamp}/{url}"
USER_AGENT = "waybackurls.py/2.0 (+https://github.com/00xNetrunner/waybackurls.py)"
# (connect timeout, read timeout). Reads can be slow on big collapse queries.
TIMEOUT = (10, 120)

THROTTLE_HINT = (
    "    archive.org rate-limited this network (HTTP 429, quota x-rl:0).\n"
    "    This is normal on VPN / datacenter / cloud IPs — the CDX API gives\n"
    "    those ranges near-zero quota. Fixes, in order:\n"
    "      1. Turn the VPN OFF and use a residential/home IP.\n"
    "      2. Add --limit 50 to test connectivity with a small, fast query.\n"
    "      3. Raise --retries and lower --threads to 1 to back off harder.\n"
)


# --------------------------------------------------------------------------- #
# Terminal styling
# --------------------------------------------------------------------------- #

class C:
    """ANSI escape codes. Blanked out when the terminal can't handle color."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[38;5;203m"
    GREEN = "\033[38;5;84m"
    YELLOW = "\033[38;5;221m"
    CYAN = "\033[38;5;51m"
    BLUE = "\033[38;5;75m"
    MAGENTA = "\033[38;5;213m"
    GREY = "\033[38;5;245m"
    # Gradient sweep used for the banner reveal (violet -> cyan).
    GRADIENT = (99, 134, 135, 79, 80, 45, 51, 87)

    @classmethod
    def disable(cls) -> None:
        for name in dir(cls):
            if name.isupper() and isinstance(getattr(cls, name), str):
                setattr(cls, name, "")
        cls.GRADIENT = ()


def supports_color() -> bool:
    """True only for a real TTY that isn't asking us to stay plain."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stderr.isatty()


def animate_banner() -> None:
    """Reveal the banner line-by-line with a color sweep.

    Pure cosmetics: each line is printed once with a color pulled from a
    cycling gradient, with a tiny sleep so the eye catches the wipe. Falls
    back to a plain dump when color/animation isn't available.
    """
    if not C.GRADIENT:
        print(BANNER, file=sys.stderr)
        return

    lines = BANNER.strip("\n").splitlines()
    palette = itertools.cycle(C.GRADIENT)
    for line in lines:
        color = next(palette)
        sys.stderr.write(f"\033[38;5;{color}m{line}{C.RESET}\n")
        sys.stderr.flush()
        time.sleep(0.04)
    sys.stderr.write("\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# Live status bar (spinner + counters, safe under threads)
# --------------------------------------------------------------------------- #

class StatusBar:
    """A single animated status line pinned to the bottom of the terminal.

    Worker threads mutate shared counters; a background thread repaints the
    spinner. All stdout writes go through one lock so log lines print cleanly
    *above* the bar instead of shredding it.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, total_hosts: int, enabled: bool) -> None:
        self.total_hosts = total_hosts
        self.enabled = enabled
        self.hosts_done = 0
        self.urls_found = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None  # type: ignore

    def __enter__(self) -> "StatusBar":
        if self.enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        if self.enabled:
            with self._lock:
                sys.stderr.write("\033[2K\r")
                sys.stderr.flush()

    def _render(self, frame: str) -> str:
        bar = (
            f"{C.CYAN}{frame}{C.RESET} "
            f"{C.BOLD}{self.hosts_done}/{self.total_hosts}{C.RESET} hosts  "
            f"{C.GREEN}{self.urls_found}{C.RESET} urls"
        )
        return f"\033[2K\r{bar}"

    def _spin(self) -> None:
        frames = itertools.cycle(self.FRAMES)
        while not self._stop.is_set():
            with self._lock:
                sys.stderr.write(self._render(next(frames)))
                sys.stderr.flush()
            time.sleep(0.08)

    def log(self, message: str) -> None:
        """Print a line above the status bar without corrupting it."""
        with self._lock:
            if self.enabled:
                sys.stderr.write("\033[2K\r")
            sys.stderr.write(message + "\n")
            sys.stderr.flush()

    def add_urls(self, n: int) -> None:
        with self._lock:
            self.urls_found += n

    def host_done(self) -> None:
        with self._lock:
            self.hosts_done += 1


# --------------------------------------------------------------------------- #
# Core fetch
# --------------------------------------------------------------------------- #

def build_session(retries: int = 5) -> requests.Session:
    """Session that identifies itself and backs off on 429/5xx.

    Wayback throttles anonymous, hammering clients with HTTP 429. A real
    User-Agent plus exponential backoff (honoring Retry-After) is what makes
    the CDX API usable again.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def stream_waybackurls(
    session: requests.Session,
    host: str,
    with_subs: bool,
    limit: int = 0,
    collapse: bool = True,
) -> Iterator[Tuple[str, str]]:
    """Yield (timestamp, original_url) pairs as archive.org streams them.

    Uses CDX plain-text output (not JSON) so results arrive line-by-line over
    the socket -- that's what lets the caller print each URL the moment it's
    seen instead of waiting for the whole array to buffer.
    """
    pattern = f"*.{host}/*" if with_subs else f"{host}/*"
    params = {
        "url": pattern,
        "fl": "timestamp,original",  # plain text: "<timestamp> <original>" per line
    }
    if collapse:
        # Dedupes repeated captures of the same URL. Cheap wins on small
        # hosts, but expensive server-side on huge ones -- disable with
        # --no-collapse if a large host hangs.
        params["collapse"] = "urlkey"
    if limit > 0:
        params["limit"] = str(limit)

    try:
        r = session.get(CDX_ENDPOINT, params=params, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            parts = raw.split(" ", 1)
            if len(parts) == 2:
                yield parts[0], parts[1]
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status == 429:
            print(f"[!] {host}: rate limited by archive.org.", file=sys.stderr)
            print(THROTTLE_HINT, file=sys.stderr)
        else:
            print(f"[!] {host}: HTTP error: {e}", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"[!] {host}: request failed ({e}).", file=sys.stderr)
        print("    If this timed out, retry with --limit 100 or --no-collapse.", file=sys.stderr)


def snapshot_url(timestamp: str, original_url: str) -> str:
    """The clickable Wayback Machine snapshot for a captured URL.

    web.archive.org serves the archived copy at /web/<timestamp>/<original>;
    that URL, not the bare original, is what actually resolves to the saved page.
    """
    return WAYBACK_SNAPSHOT.format(timestamp=timestamp, url=original_url)


def save_results(urls: List[Tuple[str, str]], filename: str, bar: StatusBar) -> None:
    directory = os.path.dirname(filename)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(filename, "w") as f:
        for timestamp, original_url in urls:
            try:
                date = (
                    datetime.strptime(timestamp, "%Y%m%d%H%M%S")
                    .replace(tzinfo=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                )
            except ValueError:
                date = timestamp  # keep raw if malformed
            f.write(f"{date} - {original_url} - {snapshot_url(timestamp, original_url)}\n")
    bar.log(f"{C.GREEN}[+]{C.RESET} Saved {C.BOLD}{len(urls)}{C.RESET} URLs → {C.CYAN}{filename}{C.RESET}")


def process_host(
    session: requests.Session,
    host: str,
    with_subs: bool,
    output_dir: str,
    limit: int,
    collapse: bool,
    verbose: bool,
    bar: StatusBar,
) -> None:
    start = time.time()
    bar.log(f"{C.BLUE}[>]{C.RESET} Querying {C.BOLD}{host}{C.RESET} …")

    urls: List[Tuple[str, str]] = []
    for timestamp, original_url in stream_waybackurls(
        session, host, with_subs, limit=limit, collapse=collapse
    ):
        urls.append((timestamp, original_url))
        bar.add_urls(1)
        if verbose:
            link = snapshot_url(timestamp, original_url)
            bar.log(
                f"  {C.GREY}{host}{C.RESET} {C.DIM}·{C.RESET} {original_url}\n"
                f"      {C.DIM}↳{C.RESET} {C.BLUE}{link}{C.RESET}"
            )

    if urls:
        save_results(urls, os.path.join(output_dir, f"{host}-waybackurls.txt"), bar)
    else:
        bar.log(f"{C.YELLOW}[-]{C.RESET} No URLs found for {C.BOLD}{host}{C.RESET}")

    bar.host_done()
    bar.log(
        f"{C.MAGENTA}[*]{C.RESET} {host} done — "
        f"{C.BOLD}{len(urls)}{C.RESET} urls in {C.BOLD}{time.time() - start:.2f}s{C.RESET}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve URLs from the Wayback Machine for one or more hosts.",
    )
    parser.add_argument("hosts", nargs="+", help="One or more hosts to retrieve URLs for")
    parser.add_argument(
        "-s", "--subdomains", action="store_true", help="Include subdomains in the search"
    )
    parser.add_argument(
        "-o", "--output", default="results", help="Output directory (default: results)"
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=5, help="Concurrent threads (default: 5)"
    )
    parser.add_argument(
        "-r", "--retries", type=int, default=5, help="Max retries on throttling (default: 5)"
    )
    parser.add_argument(
        "-l", "--limit", type=int, default=0,
        help="Cap results per host (0 = all). Use a small value to test connectivity.",
    )
    parser.add_argument(
        "--no-collapse", action="store_true",
        help="Don't dedupe captures server-side (helps if a huge host hangs)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print every URL live as it streams in from archive.org",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="Disable colored / animated output"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Hide the banner")
    args = parser.parse_args()

    if args.no_color or not supports_color():
        C.disable()

    if not args.quiet:
        animate_banner()

    session = build_session(retries=args.retries)
    start = time.time()

    # Spinner only makes sense on a live TTY; verbose scrolls too fast to pin it.
    bar_enabled = bool(C.GRADIENT) and not args.verbose
    with StatusBar(total_hosts=len(args.hosts), enabled=bar_enabled) as bar:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = [
                executor.submit(
                    process_host, session, host, args.subdomains, args.output,
                    args.limit, not args.no_collapse, args.verbose, bar,
                )
                for host in args.hosts
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        total = bar.urls_found

    print(
        f"\n{C.GREEN}[✓]{C.RESET} {C.BOLD}{total}{C.RESET} URLs across "
        f"{C.BOLD}{len(args.hosts)}{C.RESET} host(s) in "
        f"{C.BOLD}{time.time() - start:.2f}s{C.RESET}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
