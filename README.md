# Lithuania Flip Scanner

A disciplined **resale-arbitrage decision engine** for Lithuanian local-pickup
listings (camera lenses, guitar/music gear, Lego/collectibles, and other
high-ticket items).

It does **not** auto-buy anything and it does **not** scrape any website. You
give it candidate listings in a CSV and it tells you which ones are worth
driving out to **inspect in person**. The whole point is to avoid trash and only
spend your time on high-confidence, inspectable opportunities.

## The rules it enforces

A listing must clear **all** of these to pass:

| Rule | Default |
| --- | --- |
| Minimum expected **net profit** (after all costs) | €100 |
| Minimum **net ROI** (after all costs) | 60% |
| Minimum **gross spread** (resale vs. asking, before costs) | 100% |
| Local pickup / inspectable in person | required |
| No broken / repair / unknown-condition / suspicious wording | required |

Anything that fails a hard rule is marked `PASS` (meaning "pass it by").

## Install

Requires **Python 3.8+**. The scanner uses only the standard library, so there
is nothing to install to run it.

```bash
git clone <this-repo>
cd LTflip
```

To run the test suite you need `pytest`:

```bash
pip install -r requirements.txt
```

## Run

Score the bundled examples:

```bash
python3 listing_scanner.py example_listings.csv --config config.json --output scan_results.csv
```

Score your own listings (the `--config` flag is optional — built-in defaults are
used when it is omitted):

```bash
python3 listing_scanner.py input_template.csv --output scan_results.csv
```

Then open `scan_results.csv` in any spreadsheet program. The console also prints
the top opportunities, strongest first:

```
INSPECT          € 130.20 ROI   71.1% | Canon EF 85mm f/1.8 USM objektyvas
PASS             €  54.30 ROI   48.9% | Sigma 17-50 f2.8 Canon defektas
PASS             €  34.35 ROI   34.2% | Boss DD-7 Digital Delay pedal
```

## Workflow

1. Collect candidate local listings manually (or via permitted exports/alerts).
2. Look up **sold** eBay comps for each item (see the warning below).
3. Enter the listing and your conservative comp numbers into a copy of
   `input_template.csv`.
4. Run the scanner.
5. Only inspect `PRIORITY_INSPECT` and `INSPECT` rows.
6. **Never buy without testing the item in person.**

## CSV fields

Fill one row per listing. Empty cost cells are treated as `0`.

| Field | Meaning |
| --- | --- |
| `url` | Link to the listing. |
| `category` | `lens`, `music_gear`, `lego_collectible`, or `general`. Drives category-specific reject keywords. |
| `title` | Listing title (Lithuanian is fine). |
| `description` | Listing description. Title + description are scanned for keywords. |
| `location` | City. `vilnius` / `kaunas` score higher by default. |
| `asking_price_eur` | The seller's asking price. |
| `expected_resale_eur` | Your own resale estimate. Used only as a fallback if no comps are given. |
| `comp_low_eur` | **Conservative** eBay *sold* comp. Used as the resale figure when present (preferred). |
| `comp_median_eur` | Median eBay *sold* comp. If `comp_low` is blank, the engine uses `comp_median × 0.85`. |
| `liquidity_sold_count` | How many recent *sold* comps you found. Below 3 is penalized. |
| `platform_fee_pct` | Selling-platform fee. Accepts `0.13` **or** `13` (both mean 13%). |
| `risk_reserve_pct` | Buffer for repair/risk, as a fraction of resale price. Accepts `0.10` or `10`. Default 10%. |
| `buy_transport_cost_eur` | Cost to travel to pick the item up. |
| `resale_shipping_cost_eur` | Cost to ship it to your buyer. |
| `packaging_cost_eur` | Box, bubble wrap, etc. |
| `misc_cost_eur` | Any other cost. |
| `can_inspect_in_person` | `yes`/`no` (also accepts `taip`). **`no` is an automatic reject.** |
| `photo_quality` | `good`, `medium`, or `poor`. Affects score only, not pass/fail. |

Numbers accept comma decimals (`130,50`) and a `€` sign.

## How the scoring math works

For each row:

```
resale          = comp_low_eur            (preferred conservative comp)
risk_reserve    = resale × risk_reserve_pct
platform_fee    = resale × platform_fee_pct
all_in_cost     = asking + buy_transport + resale_shipping + packaging + misc + risk_reserve
net_profit      = resale − platform_fee − all_in_cost
net_roi         = net_profit / all_in_cost
gross_spread    = (resale − asking) / asking
```

### Worked example — the Canon lens in `example_listings.csv`

```
asking            = €120
resale (comp_low) = €360
risk_reserve      = 360 × 0.10 = €36.00
platform_fee      = 360 × 0.13 = €46.80
all_in_cost       = 120 + 5 + 18 + 4 + 0 + 36 = €183.00
net_profit        = 360 − 46.80 − 183 = €130.20     ✓ ≥ €100
net_roi           = 130.20 / 183     = 71.1%         ✓ ≥ 60%
gross_spread      = (360 − 120) / 120 = 200%         ✓ ≥ 100%
```

All three hard rules clear → decision **`INSPECT`**.

### Decision tiers

- `PRIORITY_INSPECT` — strongest. Clears all rules, score ≥ 90, net ≥ €150, ROI ≥ 80%.
- `INSPECT` — clears all rules with a solid score.
- `WATCHLIST` — not rejected, but not strong enough to act on yet.
- `PASS` — fails a hard rule (skip it).

Results are sorted strongest-first: by decision tier, then highest net profit,
then highest score.

## ⚠️ eBay asking prices are NOT comps

When you fill in `comp_low_eur` / `comp_median_eur`, use **sold** prices only —
filter eBay for *Sold Items*. Active listings are wishful asking prices and
people anchor far too high. Using asking prices as comps will make almost
everything look profitable and is the fastest way to lose money. Be
conservative: when in doubt, use the lower end of what actually **sold**.

## ⚠️ Inspection-first rule

This tool decides only whether a listing is **worth inspecting**. It cannot
prove an item works. For lenses, music gear, and electronics, **never buy
without testing the item in person.** See `inspection_checklists.md` for
per-category checks (fungus/haze on lenses, powering up pedals, completeness of
Lego sets, etc.).

## Configuration

`config.json` overrides the defaults (thresholds, allowed cities, keyword
lists). Any key you omit falls back to the built-in default in
`listing_scanner.py`.

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

## Compliance

Respect marketplace terms, `robots.txt`, and privacy rules. Do **not** bypass
logins, rate limits, or CAPTCHAs, and do **not** harvest personal/contact data.
This stays a manual CSV decision engine on purpose so you validate the economics
before ever automating collection.
