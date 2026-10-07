import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

SITEMAP_FILE = "bunnings.com.au-stores.xml.xml"
# Store detail pages look like https://www.bunnings.com.au/stores/wa/midland;
# skip any state index or other pages the sitemap may also list.
STORE_URL_RE = re.compile(r'^https://www\.bunnings\.com\.au/stores/[a-z]+/[^/?#]+/?$')
DEFAULT_WORKERS = 8
FETCH_ATTEMPTS = 5
# Statuses worth retrying: throttling and transient server-side faults. A 404
# will not become a 200 on retry.
RETRYABLE_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}
# Allow a few stale sitemap entries to fail, but fail loudly if the success
# rate collapses (e.g. the site starts challenging the scraper).
DEFAULT_MIN_SUCCESS_RATE = 0.9
DAY_ORDER = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
verbose = False

def extract_urls_from_sitemap(filepath):
    """Extract store URLs from the sitemap XML file."""
    try:
        root = ET.parse(filepath).getroot()
        # Match <loc> regardless of which sitemap namespace URI is used
        urls = [
            elem.text.strip()
            for elem in root.iter()
            if elem.tag.rpartition('}')[2] == 'loc' and elem.text
        ]
        return [url for url in urls if STORE_URL_RE.match(url)]
    except Exception as e:
        print(f"Error parsing sitemap: {e}", file=sys.stderr)
        return []

def backoff_delay(attempt, retry_after=None):
    """Seconds to wait before the next attempt, with jitter to desynchronise workers."""
    if retry_after:
        try:
            return min(float(retry_after), 30)
        except ValueError:
            pass
    return min(2 ** attempt, 16) + random.uniform(0, 1)

def fetch_html(url):
    """Fetch a page, retrying on throttling and transient network errors."""
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urlopen(url, timeout=15) as response:
                return response.read().decode('utf-8')
        except HTTPError as e:
            if e.code not in RETRYABLE_STATUSES or attempt == FETCH_ATTEMPTS - 1:
                raise
            delay = backoff_delay(attempt, e.headers.get('Retry-After'))
            if verbose:
                print(f"Retrying {url} in {delay:.1f}s after HTTP {e.code}", file=sys.stderr)
            time.sleep(delay)
        except (URLError, TimeoutError, OSError) as e:
            if attempt == FETCH_ATTEMPTS - 1:
                raise
            delay = backoff_delay(attempt)
            if verbose:
                print(f"Retrying {url} in {delay:.1f}s after error: {e}", file=sys.stderr)
            time.sleep(delay)

def to_24h(time_data):
    """Convert Bunnings' {formattedHour: '9:00 pm', hour: 9, minute: 0} to '21:00'.

    The numeric hour is on a 12-hour clock, so the am/pm marker in
    formattedHour is needed to disambiguate.
    """
    if not time_data:
        return None
    formatted = (time_data.get('formattedHour') or '').replace(' ', ' ').strip().lower()
    match = re.match(r'^(\d{1,2}):(\d{2})\s*([ap])\.?m\.?$', formatted)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
    hour = hour % 12 + (12 if meridiem == 'p' else 0)
    return f"{hour:02d}:{minute:02d}"

def parse_trading_hours(opening_hours):
    """Flatten weekDayOpeningList into Monday-Sunday open/close entries."""
    days = []
    for day in (opening_hours or {}).get('weekDayOpeningList') or []:
        closed = bool(day.get('closed'))
        days.append({
            'weekDay': day.get('weekDay'),
            'closed': closed,
            'open': None if closed else to_24h(day.get('openingTime')),
            'close': None if closed else to_24h(day.get('closingTime')),
        })
    return sorted(days, key=lambda d: DAY_ORDER.index(d['weekDay']) if d['weekDay'] in DAY_ORDER else 7)

def parse_store_page(html, url):
    """Pull the page's store record out of the __NEXT_DATA__ blob."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    data = json.loads(match.group(1))
    state = data.get('props', {}).get('pageProps', {}).get('initialState', {})
    # Note: initialState.global.userData.appliedStore is the *visitor's* chosen
    # store, not the store this page is about, so only read initialState.store.
    store = state.get('store', {}).get('data') or {}
    if not store.get('name'):
        return None

    # Guard against picking up the wrong store: the page route's storeCode
    # should agree with the store record.
    route_code = (state.get('global', {}).get('sitecoreData', {}).get('sitecore', {})
                  .get('route', {}).get('fields', {}).get('storeCode', {}).get('value'))
    if route_code and route_code != store.get('name'):
        print(f"Store code mismatch on {url}: route {route_code}, data {store.get('name')}",
              file=sys.stderr)
        return None

    address = store.get('address') or {}
    geo = store.get('geoPoint') or {}
    return {
        'storeCode': store.get('name'),
        'displayName': store.get('displayName'),
        'phone': address.get('phone'),
        'fax': address.get('fax'),
        'email': address.get('email'),
        'line1': address.get('line1'),
        'line2': address.get('line2'),
        'town': address.get('town'),
        'state': (address.get('region') or {}).get('isocode') or store.get('storeRegion'),
        'postcode': address.get('postalCode'),
        'latitude': geo.get('latitude'),
        'longitude': geo.get('longitude'),
        'timeZone': store.get('timeZone'),
        'tradingHours': parse_trading_hours(store.get('openingHours')),
        'storeServices': sorted(store.get('storeServices') or []),
        'hasClickAndCollect': store.get('hasClickAndCollect'),
        'hasDelivery': store.get('hasDelivery'),
        'hasDriveAndCollect': store.get('hasDriveAndCollect'),
        'isActiveLocation': store.get('isActiveLocation'),
        'pricingRegion': store.get('pricingRegion'),
        'storeZone': store.get('storeZone'),
        'url': url,
    }

def get_store_details(url):
    """Fetch a store page and extract its details."""
    try:
        if verbose:
            print(f"Fetching: {url}", file=sys.stderr)
        return parse_store_page(fetch_html(url), url)
    except (URLError, HTTPError) as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON from {url}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error processing {url}: {e}", file=sys.stderr)
        return None

def main():
    """Main function to scrape all stores and output as JSON."""
    global verbose

    parser = argparse.ArgumentParser(description='Extract Bunnings store details from sitemap.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    parser.add_argument('-w', '--workers', type=int, default=DEFAULT_WORKERS, help=f'Number of parallel workers (default: {DEFAULT_WORKERS})')
    parser.add_argument('--min-success-rate', type=float, default=DEFAULT_MIN_SUCCESS_RATE,
                        help=f'Exit non-zero if the fraction of stores extracted falls below this (default: {DEFAULT_MIN_SUCCESS_RATE})')
    parser.add_argument('--html', metavar='FILE',
                        help='Parse a saved store page instead of scraping (for testing)')
    args = parser.parse_args()
    verbose = args.verbose

    if args.html:
        store = parse_store_page(Path(args.html).read_text(encoding='utf-8'), args.html)
        if not store:
            print(f"Error: No store data found in '{args.html}'.", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(store, indent=2))
        return

    if not Path(SITEMAP_FILE).exists():
        print(f"Error: Sitemap file '{SITEMAP_FILE}' not found.", file=sys.stderr)
        sys.exit(1)

    urls = extract_urls_from_sitemap(SITEMAP_FILE)

    if verbose:
        print(f"Found {len(urls)} stores in sitemap", file=sys.stderr)

    if not urls:
        print(f"Error: No store URLs found in '{SITEMAP_FILE}'.", file=sys.stderr)
        sys.exit(1)

    all_stores = []
    errors = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_url = {
            executor.submit(get_store_details, url): (i, url)
            for i, url in enumerate(urls, 1)
        }

        for future in as_completed(future_to_url):
            i, url = future_to_url[future]
            try:
                store_data = future.result()
                if store_data:
                    all_stores.append(store_data)
                    if verbose:
                        print(f"  [{i}/{len(urls)}] {store_data.get('displayName', 'Unknown')}",
                              file=sys.stderr)
                else:
                    errors.append((i, url))
                    if verbose:
                        print(f"  [{i}/{len(urls)}] Failed to extract", file=sys.stderr)
            except Exception as e:
                errors.append((i, url))
                print(f"Error processing {url}: {e}", file=sys.stderr)

    print(f"Extracted {len(all_stores)} stores", file=sys.stderr)
    if errors:
        print(f"Failed to extract {len(errors)} stores:", file=sys.stderr)
        for idx, url in errors:
            print(f"  [{idx}] {url}", file=sys.stderr)

    success_rate = len(all_stores) / len(urls)
    if success_rate < args.min_success_rate:
        print(
            f"Error: Only extracted {len(all_stores)}/{len(urls)} stores "
            f"({success_rate:.1%}), below the {args.min_success_rate:.1%} threshold.",
            file=sys.stderr,
        )
        sys.exit(1)

    all_stores_sorted = sorted(all_stores, key=lambda x: x.get('storeCode') or '')
    print(json.dumps(all_stores_sorted, indent=2))

if __name__ == "__main__":
    main()
