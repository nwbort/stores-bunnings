#!/bin/bash
set -e

# Cloudflare protects www.bunnings.com.au with a managed challenge, so plain
# curl only ever gets the "Just a moment..." interstitial. Drive a real Chrome
# (headful, under Xvfb) via nodriver to clear the challenge, then fetch the
# stores.xml sitemap and every store page from inside the browser. xvfb-run
# gives Chrome a display so it runs headful, which is far less likely to be
# flagged than headless (headless never got through in testing).
#
# Writes bunnings.com.au-stores.xml.xml (the sitemap) and stores.json.
xvfb-run -a -s "-screen 0 1920x1080x24" python scrape.py > stores.json.tmp
mv stores.json.tmp stores.json
