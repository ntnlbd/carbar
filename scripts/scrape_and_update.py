#!/usr/bin/env python3
"""
Daily scrape + diff for carbarsale.ie stock tracker.

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


# Each listing on /cars is a self-contained card that starts with an anchor:
#   <a href="https://carbarsale.ie/cars/<id>/<slug>" class="car-card-link">
# We split the page into one block per card (from each anchor up to the next)
# and pull every field from *within that card's own block* — never a fixed
# character window, which previously leaked a neighbouring card's price into
# each car (and left the first card with no price at all).
CARD_LINK = re.compile(
    r'href="(https?://carbarsale\.ie/cars/(\d+)/[^"]+)"[^>]*class="car-card-link"'
)


def _is_deposit(title, url):
    """A sold-pending car is flagged with a '🔹DEPOSIT TAKEN🔹' title and a
    /deposit-taken URL slug rather than a normal make/model title."""
    return (
        "deposit taken" in (title or "").lower()
        or (url or "").rstrip("/").endswith("/deposit-taken")
    )


def _looks_like_marker(title):
    """True when a title is the site's reserved placeholder rather than a real
    make/model name (e.g. '🔹DEPOSIT TAKEN🔹')."""
    return (not title) or "deposit taken" in title.lower()


def carry_forward_identity(new_cars, old_cars):
    """When a car goes deposit-taken, the site hides its make/model (the title
    and even the detail page become '🔹DEPOSIT TAKEN🔹'). Preserve the real
    identity we captured before the deposit so lists keep showing the car's
    name/model/make, with 'Deposit Taken' carried only in the status field."""
    old_by_id = {c["id"]: c for c in old_cars}
    for car in new_cars:
        if not _looks_like_marker(car.get("title")):
            continue
        prev = old_by_id.get(car["id"])
        if prev and not _looks_like_marker(prev.get("title")):
            car["title"] = prev["title"]
            if prev.get("make") and prev["make"] != "Unknown":
                car["make"] = prev["make"]
            if car.get("year") is None:
                car["year"] = prev.get("year")
    return new_cars


def parse_cars(html):
    """
    Scraper for carbarsale.ie/cars listing cards.

    Parses each `.car-card-link` block independently, so it works for any
    number of cars and stays correct as stock is added or removed.

    NOTE: This targets the page structure observed in August 2026. If it
    stops finding cars, open the page's HTML source, find the listing-card
    container (currently `<a ... class="car-card-link">` wrapping a
    `.car-card` with `.car-title`, `.car-specs`, and `.car-price`), and update
    the patterns below. Consider swapping in BeautifulSoup
    (`pip install beautifulsoup4`) if the markup gets more complex.
    """
    cars = []
    anchors = list(CARD_LINK.finditer(html))
    seen_ids = set()
    for i, match in enumerate(anchors):
        url, car_id = match.group(1), int(match.group(2))
        if car_id in seen_ids:
            continue
        seen_ids.add(car_id)
        # This card's HTML: from its anchor up to the next card's anchor.
        block_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        block = html[match.start():block_end]

        title_m = re.search(r'class="car-title">\s*([^<]+?)\s*</div>', block)
        title = title_m.group(1).strip() if title_m else f"Car {car_id}"

        # Current asking price (the "actual offer") only: `class="car-price">€...`.
        # This deliberately does NOT match `car-price-label`.
        price_m = re.search(r'class="car-price">\s*€\s?([\d,]+)', block)
        price = int(price_m.group(1).replace(",", "")) if price_m else None

        # `car-price-was` is the struck-through original price shown when a car
        # is discounted (on offer). Absent for full-price cars.
        was_m = re.search(r'class="car-price-was">\s*€\s?([\d,]+)', block)
        price_was = int(was_m.group(1).replace(",", "")) if was_m else None

        # Specs live as <span> items inside <div class="car-specs">:
        # e.g. 2017 / 2.0L Diesel / 139,000 mi / Co. Meath. Match each span by
        # its content rather than position, so reordering doesn't break it.
        specs_m = re.search(r'class="car-specs">(.*?)</div>', block, re.S)
        specs = (
            re.findall(r'<span>\s*([^<]+?)\s*</span>', specs_m.group(1))
            if specs_m else []
        )
        year = mileage = None
        mileage_unit = "km"
        engine = ""
        for s in specs:
            if year is None and re.fullmatch(r'(?:19|20)\d{2}', s):
                year = int(s)
            mm = re.match(r'([\d,]+)\s*(km|mi)\b', s, re.I)
            if mileage is None and mm:
                mileage = int(mm.group(1).replace(",", ""))
                mileage_unit = mm.group(2).lower()
            if not engine and re.search(r'(diesel|petrol|hybrid|electric)', s, re.I):
                engine = s
        if year is None:  # fall back to a year embedded in the title
            ym = re.search(r'\b(?:19|20)\d{2}\b', title)
            year = int(ym.group(0)) if ym else None

        trans_m = re.search(r'\b(Automatic|Auto|Manual)\b', title, re.I)
        transmission = trans_m.group(1).title() if trans_m else ""

        deposit = _is_deposit(title, url)
        # A deposit-taken card hides the make/model, so we can't derive a make.
        make = "Unknown" if deposit else (title.split()[0] if title else "")

        on_offer = bool(price_was and price is not None and price < price_was)
        cars.append({
            "id": car_id,
            "title": title,
            "year": year,
            "price": price,          # current asking price (actual offer)
            "price_was": price_was,  # original price if discounted, else None
            "on_offer": on_offer,
            "mileage": mileage,
            "mileage_unit": mileage_unit,
            "engine": engine,
            "transmission": transmission,
            "url": url,
            "make": make,
            "status": "Deposit Taken" if deposit else "Available",
        })
    return cars


def compute_stats(cars):
    available = [c for c in cars if c.get("status") != "Deposit Taken"]
    deposit = [c for c in cars if c.get("status") == "Deposit Taken"]
    prices = [c["price"] for c in cars if c.get("price")]
    avail_prices = [c["price"] for c in available if c.get("price")]
    return {
        "total_cars": len(cars),
        "available": len(available),
        "deposit_taken": len(deposit),
        "on_offer": sum(1 for c in cars if c.get("on_offer")),
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
            old_deposit = old.get("status") == "Deposit Taken" or _is_deposit(old.get("title"), old.get("url"))
            new_deposit = car.get("status") == "Deposit Taken" or _is_deposit(car.get("title"), car.get("url"))
            if old_deposit != new_deposit:
                status = "Deposit taken" if new_deposit else "Deposit released"
                entries.append({
                    "date": TODAY, "type": "Status change", "carId": cid,
                    "title": car["title"], "details": status,
                })
            # Offer tracking: a car put on offer (a new struck-through
            # original price appears) or a change to the discounted-from price.
            old_was, new_was = old.get("price_was"), car.get("price_was")
            if new_was and new_was != old_was:
                entries.append({
                    "date": TODAY, "type": "Offer", "carId": cid,
                    "title": car["title"], "price": car.get("price"),
                    "details": f"On offer: EUR {car.get('price')} (down from EUR {new_was})",
                })
            elif old_was and not new_was:
                entries.append({
                    "date": TODAY, "type": "Offer ended", "carId": cid,
                    "title": car["title"], "price": car.get("price"),
                    "details": f"Offer ended: now EUR {car.get('price')} (was on offer from EUR {old_was})",
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

    # Keep the real name/make for cars that have since gone deposit-taken.
    carry_forward_identity(new_cars, old_cars)

    changelog_entries = diff_cars(old_cars, new_cars)
    stats = compute_stats(new_cars)

    # Update baseline.json
    save_json("baseline.json", {
        "scraped_at": TODAY,
        "source": SOURCE_URL,
        "cars": new_cars,
        "stats": stats,
    })

    # Append history snapshot (one per check). If a snapshot for today already
    # exists — e.g. the job was re-run manually the same day — replace it
    # instead of adding a duplicate point to the trend chart.
    history = load_json("history.json", [])
    snapshot = {
        "date": TODAY,
        "total": stats["total_cars"],
        "available": stats["available"],
        "depositTaken": stats["deposit_taken"],
        "onOffer": stats.get("on_offer", 0),
        "aumAvailable": stats["aum_available_eur"],
        "aumAll": stats["aum_all_eur"],
        "avgPrice": stats["avg_price_eur"],
    }
    if history and history[-1].get("date") == TODAY:
        history[-1] = snapshot
    else:
        history.append(snapshot)
    save_json("history.json", history)

    # Append changelog entries
    changelog = load_json("changelog.json", [])
    changelog.extend(changelog_entries)
    save_json("changelog.json", changelog)

    print(f"OK: {len(new_cars)} cars, {len(changelog_entries)} changelog entries added.")


if __name__ == "__main__":
    main()
