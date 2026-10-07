#!/usr/bin/env python3
"""
Try one technique for getting past Cloudflare on www.bunnings.com.au and
report whether it could fetch (a) the stores.xml sitemap and (b) a store page
with its __NEXT_DATA__ store record.

Usage: probe.py METHOD [VARIANT]

Writes a RESULT line to stdout, the GitHub step summary, and the fetched
bodies to probe-out/ for inspection.
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from shutil import which

SITEMAP = "https://www.bunnings.com.au/stores.xml"
STORE = "https://www.bunnings.com.au/stores/wa/midland"
STORE2 = "https://www.bunnings.com.au/stores/nsw/alexandria"
OUT = Path("probe-out")
OUT.mkdir(exist_ok=True)

CHALLENGE_MARKERS = (
    "Just a moment",
    "challenge-platform",
    "cf_chl_opt",
    "Enable JavaScript and cookies to continue",
    "Verifying you are human",
    "Performing security verification",
)
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

results = {}
t0 = time.time()


def log(*a):
    print(f"[{time.time() - t0:6.1f}s]", *a, flush=True)


def is_challenge(body: str) -> bool:
    return any(m in body for m in CHALLENGE_MARKERS)


def sitemap_ok(body) -> bool:
    return bool(body) and "<loc>" in body and "/stores/" in body and not is_challenge(body)


def store_ok(body) -> bool:
    if not body:
        return False
    m = NEXT_DATA_RE.search(body)
    if not m:
        return False
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return False
    store = (data.get("props", {}).get("pageProps", {}).get("initialState", {})
             .get("store", {}).get("data") or {})
    return bool(store.get("name"))


def record(name, ok, body=None, note=""):
    results[name] = {"ok": bool(ok), "note": note, "bytes": len(body) if body else 0}
    if body:
        ext = "xml" if "sitemap" in name else "html"
        (OUT / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.{ext}").write_text(
            body if isinstance(body, str) else str(body), encoding="utf-8")
    title = ""
    if body:
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = m.group(1).strip()[:60] if m else ""
    log(f"{'OK  ' if ok else 'FAIL'} {name}: {len(body) if body else 0} bytes "
        f"title={title!r} {note}")


def finish(method):
    elapsed = time.time() - t0
    line = " ".join(f"{k}={'OK' if v['ok'] else 'FAIL'}" for k, v in results.items())
    print(f"RESULT {method}: {line} ({elapsed:.0f}s)", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### {method}\n\n| check | ok | bytes | note |\n|---|---|---|---|\n")
            for k, v in results.items():
                f.write(f"| {k} | {'✅' if v['ok'] else '❌'} | {v['bytes']} | {v['note']} |\n")
            f.write(f"\n{elapsed:.0f}s\n\n")
    (OUT / "results.json").write_text(json.dumps({"method": method, "results": results}, indent=2))
    return 0 if any(v["ok"] for v in results.values()) else 1


# --------------------------------------------------------------------------
# Plain HTTP clients
# --------------------------------------------------------------------------

def m_curl_cffi(_variant):
    from curl_cffi import requests
    for imp in ("chrome", "chrome131", "safari", "safari_ios", "firefox", "edge"):
        for url, name, check in ((SITEMAP, "sitemap", sitemap_ok), (STORE, "store", store_ok)):
            try:
                r = requests.get(url, impersonate=imp, timeout=30)
                record(f"{name}-{imp}", check(r.text), r.text,
                       f"HTTP {r.status_code} cf-mitigated={r.headers.get('cf-mitigated')}")
            except Exception as e:
                record(f"{name}-{imp}", False, note=f"error {e!r}"[:200])


def m_cloudscraper(_variant):
    import cloudscraper
    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    for url, name, check in ((SITEMAP, "sitemap", sitemap_ok), (STORE, "store", store_ok)):
        try:
            r = s.get(url, timeout=30)
            record(name, check(r.text), r.text, f"HTTP {r.status_code}")
        except Exception as e:
            record(name, False, note=f"error {e!r}"[:200])


def m_wayback(_variant):
    """Not a bypass: see whether archive.org has fresh copies to fall back on."""
    for url, name, check in ((SITEMAP, "sitemap", sitemap_ok), (STORE, "store", store_ok)):
        try:
            api = "https://archive.org/wayback/available?url=" + url
            with urllib.request.urlopen(api, timeout=30) as r:
                snap = json.load(r).get("archived_snapshots", {}).get("closest") or {}
            ts = snap.get("timestamp")
            if not ts:
                record(name, False, note="no snapshot")
                continue
            raw = f"https://web.archive.org/web/{ts}id_/{url}"
            with urllib.request.urlopen(raw, timeout=60) as r:
                body = r.read().decode("utf-8", "replace")
            record(name, check(body), body, f"snapshot {ts}")
        except Exception as e:
            record(name, False, note=f"error {e!r}"[:200])


def m_flaresolverr(variant):
    endpoint = "http://localhost:8191/v1"
    for _ in range(60):
        try:
            urllib.request.urlopen("http://localhost:8191/", timeout=2)
            break
        except Exception:
            time.sleep(2)
    for url, name, check in ((STORE, "store", store_ok), (SITEMAP, "sitemap", sitemap_ok)):
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps({"cmd": "request.get", "url": url, "maxTimeout": 120000}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
            sol = data.get("solution") or {}
            body = sol.get("response") or ""
            record(name, check(body), body,
                   f"status={data.get('status')} HTTP {sol.get('status')} msg={data.get('message')!r}"[:200])
        except Exception as e:
            record(name, False, note=f"error {e!r}"[:200])


# --------------------------------------------------------------------------
# nodriver (what tribunal-tracker uses)
# --------------------------------------------------------------------------

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
    "--no-sandbox",
    "--lang=en-AU",
]


def find_chrome():
    for c in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        p = which(c)
        if p:
            return p
    raise FileNotFoundError("no chrome")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def nd_wait(tab, url, seconds=90, click=True):
    """Wait for the challenge on the current tab to clear; return html or None."""
    target = url.split("?")[0].rstrip("/")
    deadline = time.time() + seconds
    next_click = time.time() + 6
    last = None
    while time.time() < deadline:
        try:
            state = await tab.evaluate("document.readyState + ' ' + location.origin + location.pathname")
            ready, _, href = str(state).partition(" ")
            html = await tab.get_content() if ready == "complete" and href.rstrip("/") == target else None
        except Exception as e:
            log(f"  page check failed: {e}")
            await tab.sleep(1)
            continue
        if html is not None:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            title = m.group(1).strip() if m else ""
            if title != last:
                log(f"  title={title!r} bytes={len(html)} challenge={is_challenge(html)}")
                last = title
            if not is_challenge(html):
                return html
            if click and time.time() >= next_click:
                next_click = time.time() + 5
                try:
                    await tab.verify_cf()
                    log("  verify_cf clicked")
                except Exception as e:
                    log(f"  verify_cf: {e!r}"[:200])
        await tab.sleep(1)
    try:
        await tab.save_screenshot(str(OUT / f"timeout-{int(time.time())}.png"))
    except Exception:
        pass
    return None


API_HOOK = r"""
(() => {
  window.__apiCalls = [];
  const keep = u => /api/i.test(String(u));
  const norm = h => {
    const o = {};
    if (!h) return o;
    if (h instanceof Headers) { h.forEach((v, k) => o[k] = v); return o; }
    if (Array.isArray(h)) { h.forEach(([k, v]) => o[k] = v); return o; }
    return Object.assign(o, h);
  };
  const of = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || String(input);
      if (keep(url)) {
        const headers = Object.assign(norm(input && input.headers), norm(init && init.headers));
        window.__apiCalls.push({url, method: (init && init.method) || 'GET', headers});
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };
  const oo = XMLHttpRequest.prototype.open, os = XMLHttpRequest.prototype.setRequestHeader,
        osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__rec = {url: String(u), method: m, headers: {}}; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) { if (this.__rec) this.__rec.headers[k] = v; return os.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () { if (this.__rec && keep(this.__rec.url)) window.__apiCalls.push(this.__rec); return osend.apply(this, arguments); };
})();
"""


async def collect_api_calls(tab):
    try:
        calls = json.loads(await tab.evaluate(
            "JSON.stringify(window.__apiCalls || [])", return_by_value=True))
        urls = json.loads(await tab.evaluate(
            "JSON.stringify(performance.getEntriesByType('resource').map(e => e.name)"
            ".filter(u => /api/i.test(u)))", return_by_value=True))
        log(f"hooked api calls: {len(calls)}; resource entries with 'api': {urls[:40]}")
        return calls
    except Exception as e:
        log(f"could not read api calls: {e!r}")
        return []


async def page_fetch(tab, url):
    js = (
        "(async () => { try {"
        f" const r = await fetch({json.dumps(url)}, {{credentials: 'include'}});"
        " return JSON.stringify({status: r.status, body: await r.text()});"
        "} catch (e) { return JSON.stringify({status: -1, body: String(e)}); } })()"
    )
    res = await tab.evaluate(js, await_promise=True, return_by_value=True)
    d = json.loads(res)
    return d["status"], d["body"]


async def nd_main(variant):
    import nodriver as uc

    chrome = find_chrome()
    port = free_port()
    udd = tempfile.mkdtemp(prefix="nd-")
    args = [chrome, *CHROME_ARGS, f"--user-data-dir={udd}",
            "--remote-debugging-host=127.0.0.1", f"--remote-debugging-port={port}"]
    if variant == "headless":
        args.append("--headless=new")
    args.append("about:blank")
    log("launching", chrome, variant)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                log("devtools", json.load(r).get("Browser"))
                break
        except Exception:
            time.sleep(0.5)
    browser = await uc.start(host="127.0.0.1", port=port, browser_executable_path=chrome)

    try:
        # nodriver's CDP network event parser lags behind current Chrome, so
        # record the site's API calls by wrapping fetch/XHR in the page.
        tab = browser.main_tab
        await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(source=API_HOOK))

        start = time.time()
        tab = await browser.get(STORE)
        html = await nd_wait(tab, STORE)
        record("store-navigate", store_ok(html), html, f"{time.time() - start:.0f}s to clear")
        if html is None:
            return
        await tab.sleep(5)  # let client-side API calls happen
        api_calls = await collect_api_calls(tab)

        st, sm_body = await page_fetch(tab, SITEMAP)
        record("sitemap-pagefetch", sitemap_ok(sm_body), sm_body, f"HTTP {st}")

        st, body = await page_fetch(tab, STORE2)
        record("store2-pagefetch", store_ok(body), body, f"HTTP {st}")

        # Throughput: fetch a batch of store pages concurrently from the page.
        urls = re.findall(r"<loc>\s*(https://www\.bunnings\.com\.au/stores/[a-z]+/[^<\s]+?)\s*</loc>", sm_body or "")
        if not urls:
            urls = [STORE, STORE2] * 10
        sample = urls[:20]
        start = time.time()
        js = (
            "(async () => { const urls = " + json.dumps(sample) + ";"
            " const res = await Promise.all(urls.map(async u => { try {"
            "   const r = await fetch(u, {credentials: 'include'}); const t = await r.text();"
            "   return [r.status, t.includes('__NEXT_DATA__'), t.includes('Just a moment')];"
            " } catch (e) { return [-1, false, false]; } }));"
            " return JSON.stringify(res); })()"
        )
        res = json.loads(await tab.evaluate(js, await_promise=True, return_by_value=True))
        good = sum(1 for s, nd, ch in res if s == 200 and nd)
        record("batch20-pagefetch", good == len(sample), None,
               f"{good}/{len(sample)} ok in {time.time() - start:.1f}s statuses={sorted(set(r[0] for r in res))}")

        st_html = None
        tab2 = await browser.get(STORE2)
        st_html = await nd_wait(tab2, STORE2, seconds=45)
        record("store2-navigate", store_ok(st_html), st_html)
        if st_html:
            await tab2.sleep(5)
            api_calls += await collect_api_calls(tab2)

        # Can the clearance cookie be replayed outside the browser?
        cookies = await browser.cookies.get_all()
        jar = {c.name: c.value for c in cookies if "bunnings" in (c.domain or "")}
        ua = await tab.evaluate("navigator.userAgent")
        log("cookies:", sorted(jar), "ua:", ua)
        try:
            from curl_cffi import requests
            for imp in ("chrome", "chrome131"):
                r = requests.get(STORE, impersonate=imp, cookies=jar,
                                 headers={"User-Agent": ua}, timeout=30)
                record(f"cookie-replay-{imp}", store_ok(r.text), r.text, f"HTTP {r.status_code}")
        except Exception as e:
            record("cookie-replay", False, note=f"error {e!r}"[:200])
        try:
            req = urllib.request.Request(STORE, headers={
                "User-Agent": ua,
                "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
            record("cookie-replay-urllib", store_ok(body), body)
        except Exception as e:
            record("cookie-replay-urllib", False, note=f"error {e!r}"[:200])

        # What does the site's own JS call, and can we call it directly?
        seen = {}
        for c in api_calls:
            key = c["url"].split("?")[0]
            seen.setdefault(key, c)
        log(f"API calls seen: {len(api_calls)}")
        for key, c in list(seen.items())[:40]:
            h = {k: (v if k.lower() not in ("authorization", "cookie") else f"<{len(v)} chars>")
                 for k, v in c["headers"].items()}
            log(f"  {c['method']} {c['url'][:200]} {h}")
        (OUT / "api-calls.json").write_text(json.dumps(
            [{"url": c["url"], "method": c["method"], "header_names": sorted(c["headers"])}
             for c in api_calls], indent=2))
        authed = [c for c in api_calls if "api.prod.bunnings" in c["url"]
                  and any(k.lower() == "authorization" for k in c["headers"])]
        if authed:
            c = authed[0]
            hdrs = {k: v for k, v in c["headers"].items() if not k.startswith(":")}
            for url in (
                c["url"],
                "https://api.prod.bunnings.com.au/v1/stores/country/AU?fields=FULL&pageSize=1000",
            ):
                try:
                    from curl_cffi import requests
                    r = requests.get(url, headers=hdrs, impersonate="chrome", timeout=30)
                    record(f"api-replay-{url.split('/v1/')[-1][:40]}", r.status_code == 200, r.text,
                           f"HTTP {r.status_code}")
                except Exception as e:
                    record("api-replay", False, note=f"error {e!r}"[:200])
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        proc.terminate()


def m_nodriver(variant):
    import nodriver as uc
    uc.loop().run_until_complete(nd_main(variant))


# --------------------------------------------------------------------------
# SeleniumBase UC mode
# --------------------------------------------------------------------------

def m_seleniumbase(variant):
    from seleniumbase import SB

    with SB(uc=True, xvfb=True, locale="en-AU") as sb:
        start = time.time()
        sb.uc_open_with_reconnect(STORE, reconnect_time=6)
        html = sb.get_page_source()
        for i in range(6):
            if not is_challenge(html):
                break
            log(f"  challenge still showing, uc_gui_click_captcha (try {i + 1})")
            try:
                sb.uc_gui_click_captcha()
            except Exception as e:
                log(f"  click failed: {e!r}"[:200])
            sb.sleep(4)
            html = sb.get_page_source()
        record("store-navigate", store_ok(html), html, f"{time.time() - start:.0f}s")
        if not store_ok(html):
            sb.save_screenshot(str(OUT / "sb-fail.png"))
            return
        body = sb.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            f"fetch({json.dumps(SITEMAP)}, {{credentials: 'include'}})"
            ".then(r => r.text()).then(done).catch(e => done(String(e)));")
        record("sitemap-pagefetch", sitemap_ok(body), body)
        sb.uc_open_with_reconnect(STORE2, reconnect_time=4)
        html = sb.get_page_source()
        record("store2-navigate", store_ok(html), html)


# --------------------------------------------------------------------------
# Camoufox (patched Firefox)
# --------------------------------------------------------------------------

def pw_wait(page, url, seconds=90):
    deadline = time.time() + seconds
    last = None
    next_click = time.time() + 6
    while time.time() < deadline:
        try:
            html = page.content()
        except Exception:
            time.sleep(1)
            continue
        title = page.title() if html else ""
        if title != last:
            log(f"  title={title!r} bytes={len(html)} challenge={is_challenge(html)}")
            last = title
        if not is_challenge(html) and page.url.split("?")[0].rstrip("/") == url.rstrip("/"):
            return html
        if time.time() >= next_click:
            next_click = time.time() + 5
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    try:
                        box = frame.frame_element().bounding_box()
                        if box:
                            page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                            log("  clicked turnstile frame")
                    except Exception as e:
                        log(f"  click failed: {e!r}"[:200])
        time.sleep(1)
    page.screenshot(path=str(OUT / f"timeout-{int(time.time())}.png"))
    return None


def pw_run(page):
    start = time.time()
    page.goto(STORE, wait_until="domcontentloaded", timeout=60000)
    html = pw_wait(page, STORE)
    record("store-navigate", store_ok(html), html, f"{time.time() - start:.0f}s")
    if not html:
        return
    res = page.evaluate(
        "async (u) => { const r = await fetch(u, {credentials: 'include'});"
        " return [r.status, await r.text()]; }", SITEMAP)
    record("sitemap-pagefetch", sitemap_ok(res[1]), res[1], f"HTTP {res[0]}")
    res = page.evaluate(
        "async (u) => { const r = await fetch(u, {credentials: 'include'});"
        " return [r.status, await r.text()]; }", STORE2)
    record("store2-pagefetch", store_ok(res[1]), res[1], f"HTTP {res[0]}")


def m_camoufox(variant):
    from camoufox.sync_api import Camoufox
    with Camoufox(headless="virtual" if variant != "headless" else True,
                  humanize=True, disable_coop=True, i_know_what_im_doing=True,
                  locale="en-AU") as browser:
        page = browser.new_page()
        pw_run(page)


def m_patchright(variant):
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=tempfile.mkdtemp(prefix="pr-"),
            channel="chrome",
            headless=(variant == "headless"),
            no_viewport=True,
            locale="en-AU",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        pw_run(page)
        ctx.close()


METHODS = {
    "curl_cffi": m_curl_cffi,
    "cloudscraper": m_cloudscraper,
    "wayback": m_wayback,
    "flaresolverr": m_flaresolverr,
    "nodriver": m_nodriver,
    "seleniumbase": m_seleniumbase,
    "camoufox": m_camoufox,
    "patchright": m_patchright,
}


def main():
    method = sys.argv[1]
    variant = sys.argv[2] if len(sys.argv) > 2 else ""
    name = f"{method}{'-' + variant if variant else ''}"
    try:
        METHODS[method](variant)
    except Exception as e:
        import traceback
        traceback.print_exc()
        record("exception", False, note=repr(e)[:200])
    return finish(name)


if __name__ == "__main__":
    sys.exit(main())
