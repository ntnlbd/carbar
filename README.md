# CarBarSale.ie Stock Tracker (GitHub Pages edition)

Self-hosted version of the stock tracker dashboard. GitHub Actions scrapes
carbarsale.ie every day at 09:00 UTC, updates the JSON data files,
rebuilds the dashboard, and publishes it to GitHub Pages automatically.
No external database, no paid tier, nothing to renew — this runs forever
on GitHub's free plan (public repos get unlimited Actions minutes; private
repos get 2,000 free minutes/month, and this job uses well under a minute
a day).

## One-time setup (5 minutes)

1. **Create a new repo** on github.com (public or private — public is
   simplest since it gets unlimited free Actions minutes and free Pages
   hosting with no minute cap).
2. **Upload these files**, preserving the folder structure exactly:
   - `data/baseline.json`, `data/history.json`, `data/changelog.json`
   - `scripts/scrape_and_update.py`, `scripts/generate_dashboard.py`
   - `.github/workflows/weekly-update.yml`
   - `docs/index.html`
   - This `README.md`

   Easiest way: on the repo's main page, use **Add file → Upload files**
   and drag the whole folder in (GitHub preserves subfolders when you drag
   a folder into the upload box). Commit directly to `main`.

3. **Enable GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment", set **Source** to **GitHub Actions**. (Not "Deploy from a
   branch" — the workflow deploys via the Pages Actions API.)

4. **Enable Actions permissions**: repo → Settings → Actions → General →
   under "Workflow permissions", select **Read and write permissions**.
   This lets the daily job commit the updated data files back to the repo.

5. That's it. The workflow will run automatically every day. To run it
   immediately (don't wait for the next 09:00 UTC run), go to the **Actions**
   tab → "Daily stock update" → **Run workflow**.

Your dashboard will be live at:
`https://<your-username>.github.io/<repo-name>/`

## How it works

- `scripts/scrape_and_update.py` fetches `https://carbarsale.ie/cars`,
  parses the listing cards, diffs against `data/baseline.json` to detect
  new listings / sold cars / price changes / deposit status changes,
  appends to `data/history.json` and `data/changelog.json`, and overwrites
  `data/baseline.json` with the fresh scrape.
- `scripts/generate_dashboard.py` reads those three JSON files and
  generates `docs/index.html` — the same dashboard (stock table, trends
  tab with sales/revenue stats and monthly charts, changelog tab) you've
  already been using, now as a fully static, self-contained HTML file.
- The GitHub Actions workflow (`.github/workflows/weekly-update.yml`) ties
  it together: run the scraper, rebuild the HTML, commit the changed JSON
  + HTML back to the repo, and publish `docs/` to GitHub Pages.

## If the scraper stops finding cars

carbarsale.ie's HTML markup could change at some point, which would break
the regex-based parsing in `scrape_and_update.py`. The script is written
to fail loudly (exit with an error, no data overwritten) rather than
silently writing empty data if it parses 0 cars — so a broken scrape shows
up as a red X on the Actions tab rather than corrupting your history. If
that happens, open a car listing page in a browser, view source, and
adjust the regex patterns in `parse_cars()` to match the new markup (or
swap in `BeautifulSoup` for a more robust parse — add
`beautifulsoup4` to a `requirements.txt` and `pip install -r
requirements.txt` as a workflow step).

## Manual local run (optional)

```bash
python3 scripts/scrape_and_update.py   # scrape + update data/*.json
python3 scripts/generate_dashboard.py  # rebuild docs/index.html
```
