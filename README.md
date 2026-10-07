# Bunnings store locations (Australia)

Regularly updated store list for Bunnings Australia.

A daily GitHub Action downloads the store sitemap
(`https://www.bunnings.com.au/stores.xml`), fetches each store page and writes
the store details embedded in it to `stores.json`, sorted by store code.

## How it works

www.bunnings.com.au sits behind a Cloudflare managed challenge, so plain HTTP
clients only ever see the "Just a moment..." page from GitHub's runners.
[`scrape.py`](scrape.py) drives a real Chrome via
[nodriver](https://github.com/ultrafunkamsterdam/nodriver) (headful, under
Xvfb), waits for the challenge to clear (clicking the Turnstile checkbox if it
appears), then fetches the sitemap and every store page with the page's own
`fetch()`, so each request carries the browser's Cloudflare clearance. The
parsing lives in [`extract_stores.py`](extract_stores.py).

What was tried on GitHub's runners:

| Technique | Result |
|---|---|
| curl, urllib | Challenged (403) |
| curl_cffi impersonating Chrome, Safari, Firefox, Edge | Challenged (403) |
| cloudscraper | Challenged (403) |
| Wayback Machine | No snapshots |
| nodriver, headless Chrome | Stuck on challenge |
| Patchright (headful, Chrome) | Stuck on challenge |
| SeleniumBase UC mode + `uc_gui_click_captcha` | Stuck on challenge |
| **nodriver, headful Chrome under Xvfb** | **Cleared in 4 of 4 runs, ~13s each; 40 stores in ~5s** |
| Camoufox | Cleared in 1 of 2 runs, after a Turnstile click |
| FlareSolverr (Docker) | Clears in ~15s; ~4s per page after that |

Once a browser has the `cf_clearance` cookie, replaying it (with the same
User-Agent) from curl_cffi or urllib also worked, but fetching from inside
the browser doesn't depend on Cloudflare continuing to allow that.

## Running locally

    pip install -r requirements.txt
    ./scrape.sh                      # or: python scrape.py -v -o stores.json

To test the parser against a saved store page:

    python extract_stores.py --html "Midland - Bunnings Australia.html"
