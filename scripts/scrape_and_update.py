#!/usr/bin/env python3
"""
Weekly scrape + diff for carbarsale.ie stock tracker.

Run from the repo root (GitHub Actions does this automatically):
    python3 scripts/scrape_and_update.py

What it does:
  1. Fetches https://carbarsale.ie/cars and parses the current stock list.
  2. Diffs against data/baseline.json (last known stock) to produce
     changelog entries: New listing / Sold / Removed / Price change / Status change.
  3. Appends a snapshot to data/history.json (total, available, deposit-taken,
     AUM, avg price).
  4. Appends new entries to data/changelog.json.
  5. Overwrites data/baseline.json with the fresh scrape.

All state lives in data/*.json and is committed back to the repo by the
GitHub Actions workflow — no external database needed.
"""
import json
import os
import re
import sys
from datetime import date, datetime, timezone

import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SOURCE_URL = "https://carbarsale.ie/cars"

TODAY = date.today().isoformat()


def load_json(name, default):
    path = os.path.join(DATA_DIR, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(name, obj):
    path = os.path.join(DATA_DIR, name)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; StockTrackerBot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_cars(html):
    """
    Best-effort scraper for carbarsale.ie/cars listing cards.

    NOTE: This targets the page structure observed in August 2026. Sites
    change their markup over time — if this stops finding cars, open the
    page's HTML source, find the listing-card container, and update the
    regex/selectors below. Consider swapping in BeautifulSoup
    (`pip install beautifulsoup4`) for a more robust parse if the site's
    markup gets more complex.
    """
    cars = []
    # carbarsale.ie car detail links look like /cars/<id>/<slug>
    link_pattern = re.compile(r'href="(https?://carbarsale\.ie/cars/(\d+)/[^"]+)"')
    seen_ids = set()
    for match in link_pattern.finditer(html):
        url, car_id = match.group(1), int(match.group(2))
        if car_id in seen_ids:
            continue
        seen_ids.add(car_id)
        # Grab a window of HTML around the link to pull title/price/etc from
        # the same card. This is deliberately loose — tighten it once you've
        # inspected the live markup.
        start = max(0, match.start() - 800)
        window = html[start:match.end() + 800]

        title_m = re.search(r'([A-Z][\w\-]+(?:\s+[\w\-]+){1,5}\s+20\d{2}[^<"\n]{0,40})', window)
        price_m = re.search(r'€\s?([\d,]{3,7})', window)
        mileage_m = re.search(r'([\d,]{3,7})\s?(km|mi)\b', window, re.I)
        year_m = re.search(r'\b(20\d{2})\b', window)
        engine_m = re.search(r'(\d\.\d)L?\s?(Diesel|Petrol|Hybrid|Electric)', window, re.I)
        trans_m = re.search(r'\b(Auto|Automatic|Manual)\b', window, re.I)

        cars.append({
            "id": car_id,
            "title": (title_m.group(1).strip() if title_m else f"Car {car_id}"),
            "year": int(year_m.group(1)) if year_m else None,
            "price": int(price_m.group(1).replace(",", "")) if price_m else None,
            "mileage": int(mileage_m.group(1).replace(",", "")) if mileage_m else None,
            "mileage_unit": mileage_m.group(2).lower() if mileage_m else "km",
            "engine": engine_m.group(0) if engine_m else "",
            "transmission": trans_m.group(1) if trans_m else "",
            "url": url,
            "make": (title_m.group(1).strip().split()[0] if title_m else ""),
        })
    return cars


def compute_stats(cars):
    available = [c for c in cars if "Deposit Taken" not in c["title"]]
    deposit = [c for c in cars if "Deposit Taken" in c["title"]]
    prices = [c["price"] for c in cars if c.get("price")]
    avail_prices = [c["price"] for c in available if c.get("price")]
    return {
        "total_cars": len(cars),
        "available": len(available),
        "deposit_taken": len(deposit),
        "aum_available_eur": sum(avail_prices),
        "aum_all_eur": sum(prices),
        "avg_price_eur": round(sum(prices) / len(prices)) if prices else 0,
    }


def diff_cars(old_cars, new_cars):
    """Return a list of changelog entries comparing old -> new stock."""
    old_by_id = {c["id"]: c for c in old_cars}
    new_by_id = {c["id"]: c for c in new_cars}
    entries = []

    for cid, car in new_by_id.items():
        if cid not in old_by_id:
            entries.append({
                "date": TODAY, "type": "New listing", "carId": cid,
                "title": car["title"],
                "details": f"New listing added: {car['title']} - EUR {car.get('price', '?')}",
            })
        else:
            old = old_by_id[cid]
            if old.get("price") != car.get("price") and car.get("price"):
                entries.append({
                    "date": TODAY, "type": "Price change", "carId": cid,
                    "title": car["title"],
                    "details": f"Price changed: EUR {old.get('price')} -> EUR {car.get('price')}",
                })
            old_deposit = "Deposit Taken" in old["title"]
            new_deposit = "Deposit Taken" in car["title"]
            if old_deposit != new_deposit:
                status = "Deposit taken" if new_deposit else "Deposit released"
                entries.append({
                    "date": TODAY, "type": "Status change", "carId": cid,
                    "title": car["title"], "details": status,
                })

    for cid, car in old_by_id.items():
        if cid not in new_by_id:
            # Vanished from the site: treat as Sold unless it had no price
            # (site removals without a sale are rare but possible).
            entries.append({
                "date": TODAY, "type": "Sold", "carId": cid,
                "title": car["title"], "price": car.get("price"),
                "details": f"No longer listed (sold or removed): {car['title']} - EUR {car.get('price', '?')}",
            })

    if not entries:
        entries.append({
            "date": TODAY, "type": "No change", "carId": "-",
            "title": "-", "details": "No changes since last check.",
        })
    return entries


def main():
    print(f"Fetching {SOURCE_URL} ...")
    try:
        html = fetch_html(SOURCE_URL)
        new_cars = parse_cars(html)
    except Exception as e:
        print(f"ERROR: scrape failed: {e}", file=sys.stderr)
        sys.exit(1)

    if len(new_cars) == 0:
        print("ERROR: parsed 0 cars - site markup may have changed. Aborting without overwriting data.", file=sys.stderr)
        sys.exit(1)

    baseline = load_json("baseline.json", {"cars": []})
    old_cars = baseline.get("cars", [])

    changelog_entries = diff_cars(old_cars, new_cars)
    stats = compute_stats(new_cars)

    # Update baseline.json
    save_json("baseline.json", {
        "scraped_at": TODAY,
        "source": SOURCE_URL,
        "cars": new_cars,
        "stats": stats,
    })

    # Append history snapshot
    history = load_json("history.json", [])
    history.append({
        "date": TODAY,
        "total": stats["total_cars"],
        "available": stats["available"],
        "depositTaken": stats["deposit_taken"],
        "aumAvailable": stats["aum_available_eur"],
        "aumAll": stats["aum_all_eur"],
        "avgPrice": stats["avg_price_eur"],
    })
    save_json("history.json", history)

    # Append changelog entries
    changelog = load_json("changelog.json", [])
    changelog.extend(changelog_entries)
    save_json("changelog.json", changelog)

    print(f"OK: {len(new_cars)} cars, {len(changelog_entries)} changelog entries added.")


if __name__ == "__main__":
    main()
