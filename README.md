# Bunnings store locations (Australia)

Regularly updated store list for Bunnings Australia.

A daily GitHub Action downloads the store sitemap
(`https://www.bunnings.com.au/stores.xml`), fetches each store page and writes
the store details embedded in it to `stores.json`, sorted by store code.

The site is behind Cloudflare, so [`scrape.py`](scrape.py) fetches everything
through a real Chrome (via nodriver, under Xvfb).

To test the parser against a saved store page:

    python extract_stores.py --html "Midland - Bunnings Australia.html"
