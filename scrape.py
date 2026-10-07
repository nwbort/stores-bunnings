#!/usr/bin/env python3
"""
Browser-based scraper for Bunnings store pages.

www.bunnings.com.au sits behind a Cloudflare managed challenge, so plain HTTP
clients (curl, urllib, even curl_cffi with a browser TLS fingerprint) only
ever get the "Just a moment..." interstitial from GitHub's runners. We drive a
real, headful Chrome via nodriver (under Xvfb on CI), wait for the challenge
to clear, then fetch the stores.xml sitemap and every store page with the
page's own fetch(), so each request carries the browser's Cloudflare
clearance.

Writes the sitemap to SITEMAP_FILE and the store list (JSON) to stdout, like
extract_stores.py, whose parsing it reuses.

Usage: scrape.py [-v] [-w WORKERS]
"""

import argparse
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from shutil import which
from urllib.parse import urlparse

import nodriver as uc

from extract_stores import (
    DEFAULT_MIN_SUCCESS_RATE,
    SITEMAP_FILE,
    extract_urls_from_sitemap,
    parse_store_page,
)

HOME_URL = "https://www.bunnings.com.au/"
SITEMAP_URL = "https://www.bunnings.com.au/stores.xml"
DEFAULT_WORKERS = 6
FETCH_ATTEMPTS = 4
# Launching a fresh browser profile is the most effective retry when a
# challenge refuses to clear, so try a few before giving up.
BROWSER_ATTEMPTS = 3
CHALLENGE_TIMEOUT = 60
POLL_SECONDS = 0.5
# On GitHub's runners the challenge has cleared ~5s after a click on the
# Turnstile checkbox, which is ready a few seconds after the page loads.
FIRST_CLICK_AFTER = 6
TURNSTILE_CLICK_INTERVAL = 5

# Signs of the interstitial itself. Real Bunnings pages also load Cloudflare's
# /cdn-cgi/challenge-platform/ bot-detection script, so that string is not one.
CHALLENGE_MARKERS = (
    "<title>Just a moment...</title>",
    "_cf_chl_opt",
    "Enable JavaScript and cookies to continue",
)

# nodriver's default args, which help the browser look like a normal user
# session rather than an automated one.
CHROME_ARGS = [
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--lang=en-AU",
    "--no-sandbox",  # CI runs as root
]

# Fetch a batch of URLs from inside the page, a few at a time, and hand back
# only the __NEXT_DATA__ script (all parse_store_page needs) so we don't ship
# ~0.5 MB of HTML per store over the DevTools socket.
FETCH_BATCH_JS = r"""
(async (urls, workers) => {
  const out = [];
  let next = 0;
  async function worker() {
    while (next < urls.length) {
      const url = urls[next++];
      try {
        const r = await fetch(url, {credentials: 'include'});
        const text = await r.text();
        const m = text.match(/<script[^>]*id="__NEXT_DATA__"[^>]*>[\s\S]*?<\/script>/);
        out.push({url, status: r.status, body: m ? m[0] : text.slice(0, 2000)});
      } catch (e) {
        out.push({url, status: -1, body: String(e)});
      }
    }
  }
  await Promise.all(Array.from({length: workers}, worker));
  return JSON.stringify(out);
})
"""

verbose = False


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def debug(*args):
    if verbose:
        log(*args)


def looks_like_challenge(html: str) -> bool:
    return any(marker in html for marker in CHALLENGE_MARKERS)


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = which(candidate)
        if path:
            return path
    raise FileNotFoundError("Could not find a Chrome/Chromium binary")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_chrome(chrome_path: str, port: int, user_data_dir: str):
    args = [
        chrome_path,
        *CHROME_ARGS,
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    # Chrome ignores the proxy environment variables, so pass any HTTPS proxy
    # on explicitly (not needed on GitHub's runners).
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")
    debug(f"Launching Chrome: {chrome_path} (port {port})")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_devtools(port: int, timeout: float = 30.0) -> bool:
    """nodriver's own launcher only waits ~2.5s for the DevTools port, which
    loses a race against Chrome's cold start on CI runners, so wait here."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                debug(f"DevTools ready: {json.load(r).get('Browser')}")
                return True
        except Exception:
            time.sleep(0.5)
    return False


async def click_turnstile(tab):
    """Best-effort click of the Turnstile checkbox. The managed challenge
    usually clears on its own, but sometimes wants a click. The checkbox lives
    in a closed shadow root, so find it by matching nodriver's template image
    against a screenshot (needs opencv). verify_cf() writes its scratch images
    to the working directory, so run it somewhere they can't get committed."""
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.gettempdir())
        await tab.verify_cf()
        log("  clicked Turnstile checkbox")
    except Exception as e:
        debug(f"  Turnstile click failed: {e!r}")
    finally:
        os.chdir(cwd)


async def clear_challenge(tab, url: str) -> bool:
    """Load url and wait for the Cloudflare challenge to clear."""
    log(f"Navigating to {url}")
    # Mark the current document so we don't mistake it for the new one before
    # the navigation commits.
    try:
        await tab.evaluate("window.__stale = true")
    except Exception:
        pass
    await tab.get(url)
    deadline = time.time() + CHALLENGE_TIMEOUT
    next_click = time.time() + FIRST_CLICK_AFTER
    last_title = None
    while time.time() < deadline:
        try:
            state = await tab.evaluate(
                "[document.readyState, location.hostname, !!window.__stale].join(' ')")
            ready, host, stale = str(state).split(" ")
            loaded = ready == "complete" and host == urlparse(url).hostname and stale == "false"
            html = await tab.get_content() if loaded else None
        except Exception as e:
            debug(f"  page check failed: {e}")
            html = None
        if html is not None:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = m.group(1).strip() if m else ""
            if title != last_title:
                log(f"  title={title!r} ({len(html)} bytes)")
                last_title = title
            if not looks_like_challenge(html):
                return True
            if time.time() >= next_click:
                await click_turnstile(tab)
                next_click = time.time() + TURNSTILE_CLICK_INTERVAL
        await tab.sleep(POLL_SECONDS)
    log(f"Challenge did not clear within {CHALLENGE_TIMEOUT}s on {url}")
    return False


async def fetch_batch(tab, urls, workers):
    result = await tab.evaluate(
        f"{FETCH_BATCH_JS}({json.dumps(urls)}, {workers})",
        await_promise=True,
        return_by_value=True,
    )
    return json.loads(result)


async def fetch_sitemap(tab):
    js = (
        "(async () => { const r = await fetch(" + json.dumps(SITEMAP_URL) + ","
        " {credentials: 'include'}); return JSON.stringify([r.status, await r.text()]); })()"
    )
    status, body = json.loads(await tab.evaluate(js, await_promise=True, return_by_value=True))
    if status != 200 or "<loc>" not in body:
        raise RuntimeError(f"sitemap fetch failed: HTTP {status}, {len(body)} bytes")
    return body


async def scrape_stores(tab, urls, workers):
    """Fetch and parse every store page, retrying the ones that fail."""
    stores = {}
    gone = []
    pending = list(urls)
    for attempt in range(FETCH_ATTEMPTS):
        if not pending:
            break
        if attempt:
            delay = min(2 ** attempt, 16) + random.uniform(0, 1)
            debug(f"Retrying {len(pending)} stores in {delay:.1f}s")
            await tab.sleep(delay)
        failed = []
        challenged = 0
        # Batches keep each DevTools reply to a few MB and let us notice a
        # re-challenge early.
        for i in range(0, len(pending), workers * 5):
            batch = pending[i:i + workers * 5]
            for res in await fetch_batch(tab, batch, workers):
                url, status, body = res["url"], res["status"], res["body"]
                store = parse_store_page(body, url) if status == 200 else None
                if store:
                    stores[url] = store
                    debug(f"  [{len(stores)}/{len(urls)}] {store.get('displayName')}")
                elif status in (404, 410):
                    # A stale sitemap entry; retrying won't help.
                    debug(f"  failed {url}: HTTP {status}")
                    gone.append(url)
                else:
                    if looks_like_challenge(body):
                        challenged += 1
                    debug(f"  failed {url}: HTTP {status}")
                    failed.append(url)
        pending = failed
        if challenged:
            # The clearance expired or was revoked; earn a new one.
            log(f"{challenged} requests were challenged; re-clearing")
            await clear_challenge(tab, HOME_URL)
    return stores, gone + pending


async def run(workers):
    chrome_path = find_chrome()
    for attempt in range(1, BROWSER_ATTEMPTS + 1):
        port = free_port()
        proc = launch_chrome(chrome_path, port, tempfile.mkdtemp(prefix="bunnings-"))
        browser = None
        try:
            if not wait_for_devtools(port):
                log("Chrome DevTools endpoint never became ready")
                continue
            browser = await uc.start(host="127.0.0.1", port=port,
                                     browser_executable_path=chrome_path)
            tab = browser.main_tab
            if not await clear_challenge(tab, HOME_URL):
                log(f"Browser attempt {attempt}/{BROWSER_ATTEMPTS} failed")
                continue
            sitemap = await fetch_sitemap(tab)
            with open(SITEMAP_FILE, "w", encoding="utf-8") as f:
                f.write(sitemap)
            urls = extract_urls_from_sitemap(SITEMAP_FILE)
            log(f"Found {len(urls)} stores in sitemap")
            if not urls:
                raise RuntimeError(f"No store URLs found in {SITEMAP_FILE}")
            start = time.time()
            stores, failed = await scrape_stores(tab, urls, workers)
            log(f"Fetched {len(stores)} stores in {time.time() - start:.0f}s")
            return urls, stores, failed
        finally:
            if browser is not None:
                try:
                    browser.stop()
                except Exception:
                    pass
            proc.terminate()
    raise RuntimeError("Could not get past the Cloudflare challenge")


def main():
    global verbose

    parser = argparse.ArgumentParser(description="Scrape Bunnings store details via a real browser.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent in-page fetches (default: {DEFAULT_WORKERS})")
    parser.add_argument("--min-success-rate", type=float, default=DEFAULT_MIN_SUCCESS_RATE,
                        help="Exit non-zero if the fraction of stores extracted falls below this "
                             f"(default: {DEFAULT_MIN_SUCCESS_RATE})")
    args = parser.parse_args()
    verbose = args.verbose

    urls, stores, failed = uc.loop().run_until_complete(run(args.workers))

    log(f"Extracted {len(stores)} stores")
    if failed:
        log(f"Failed to extract {len(failed)} stores:")
        for url in failed:
            log(f"  {url}")

    success_rate = len(stores) / len(urls)
    if success_rate < args.min_success_rate:
        log(f"Error: Only extracted {len(stores)}/{len(urls)} stores ({success_rate:.1%}), "
            f"below the {args.min_success_rate:.1%} threshold.")
        sys.exit(1)

    print(json.dumps(sorted(stores.values(), key=lambda s: s.get("storeCode") or ""), indent=2))


if __name__ == "__main__":
    main()
