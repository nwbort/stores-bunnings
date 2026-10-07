# Bunnings store locations (Australia)

Regularly updated store list for Bunnings Australia.

A daily GitHub Action downloads the store sitemap
(`https://www.bunnings.com.au/stores.xml`), fetches each store page and writes
the store details embedded in it to `stores.json`, sorted by store code.

To test the parser against a saved store page:

    python extract_stores.py --html "Midland - Bunnings Australia.html"
