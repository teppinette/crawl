# Salestracker — Per-Leg Futures Contract Lookup Spec

**For:** Salestracker MTM team (copapdevvm)
**From:** Crawl / petrochem-scraper (crawl-verify → crawl-monitor-db)
**Effective:** 2026-05-19

## What changed (and why)

The petrochem scraper used to download only one Barchart contract per ticker
family — the wrong one. For today (2026-05-19) it was pulling JJAM26 (June '26
Naphtha Japan C&F) when the active spot/PO basis is JJAK26 (May '26).

Starting with the run on 2026-05-19, the scraper downloads **three months
per ticker** (current calendar month, next month, month-after) into
`futures_prices`. Each is stored under its own `contract_code` (e.g. JJAK26,
JJAM26, JJAN26) with the full daily settlement history.

Salestracker must now query the contract month that matches each leg's basis,
not pick a fixed contract_code per ticker family.

## Contract code convention

Format: `{ticker}{month_code}{2-digit-year}`

| Ticker | Product | Unit |
|--------|---------|------|
| JJA    | Japan C&F Naphtha (Platts) Swap | USD/bbl |
| JKS    | Singapore Jet Kerosene (Platts) Swap | USD/bbl |
| H9     | SGX Benzene | USD/mt |
| INO    | NWE Naphtha Crack | USD/bbl |
| CB     | Brent Crude ICE | USD/bbl |

Month codes (CME convention):
| Code | Month | | Code | Month |
|------|-------|-|------|-------|
| F    | Jan   | | N    | Jul   |
| G    | Feb   | | Q    | Aug   |
| H    | Mar   | | U    | Sep   |
| J    | Apr   | | V    | Oct   |
| K    | May   | | X    | Nov   |
| M    | Jun   | | Z    | Dec   |

Example (today is May 2026):
- JJAK26 = Naphtha Japan May '26 (the May settling contract — spot basis through 2026-05-31)
- JJAM26 = Naphtha Japan June '26 (forward — becomes spot from 2026-06-01)
- JJAN26 = Naphtha Japan July '26 (forward)

## Schema (`futures_prices`)

```
contract_code      text   -- e.g. 'JJAK26'  (PRIMARY KEY component)
settlement_date    date   -- e.g. '2026-05-18'  (PRIMARY KEY component)
settlement_price   numeric
currency           text   -- 'USD'
unit               text   -- 'USD/bbl' or 'USD/mt'
scraped_at         timestamptz
```

Rows are accumulated daily — back-history for each contract is ~2 years
(Barchart returns the full settlement series in each CSV).

## Per-leg query pattern

### Booking a PO line (purchase, May basis)

```sql
SELECT settlement_date, settlement_price
FROM futures_prices
WHERE contract_code = 'JJAK26'   -- ← PO basis month
ORDER BY settlement_date DESC
LIMIT 1;                         -- or DATE_TRUNC AVG over the BL_MONTH window
```

### Booking a sell line (sale, June basis)

```sql
SELECT settlement_date, settlement_price
FROM futures_prices
WHERE contract_code = 'JJAM26'   -- ← Sell basis month
ORDER BY settlement_date DESC
LIMIT 1;
```

### Worked example — SO 111421

| Leg | Basis month | contract_code | Use |
|-----|-------------|---------------|-----|
| PO (purchase) | May 2026 | `JJAK26` | Cost MTM — invoice price will settle on May 2026 MOPJ average |
| Sell           | Jun 2026 | `JJAM26` | Revenue MTM — sell invoice settles on June 2026 MOPJ average |

P&L on the line = sell-leg MTM − PO-leg MTM (in matching units; FX/unit conversion stays on Salestracker side).

## Contract roll (month expiry)

Naphtha Japan C&F settles at month-end. Each contract code's data stops
updating once it expires:

- JJAK26 will keep updating daily through 2026-05-31, then go static.
- From 2026-06-01, JJAM26 becomes the spot/current-month and starts seeing
  active fills; JJAN26 stays a forward.
- The scraper itself always downloads three months from the current calendar
  date — so once the calendar rolls to June, you'll see JJAM26 / JJAN26 / JJAQ26.

If a PO line's basis month is in the past (i.e. the contract expired), use
the last available settlement_date — that's the final mark.

## Mapping from BL_MONTH to contract_code

The basis month on each SO/PO line maps directly to the contract month:

```python
MONTH_CODE = "FGHJKMNQUVXZ"   # F=Jan ... Z=Dec

def contract_code(ticker: str, bl_year: int, bl_month: int) -> str:
    return f"{ticker}{MONTH_CODE[bl_month - 1]}{str(bl_year)[-2:]}"
```

For SO 111421 with `BL_MONTH=2026-05` and ticker `JJA`:
- `contract_code('JJA', 2026, 5)` → `JJAK26`

For the sell side with `BL_MONTH=2026-06`:
- `contract_code('JJA', 2026, 6)` → `JJAM26`

## What's NOT changing

- Echemi spot (`daily_prices` source='echemi') still publishes `naphtha_japan_cfr`
  as a single spot reading. That's a single time series, not month-keyed.
  Salestracker should keep using it for spot indices that aren't basis-month
  dependent (e.g. Mopja-style daily index reference).
- Sunsirs, EIA, JPX series — no change.
- Anchor-price / premium fields on the SO line — that's Salestracker's
  responsibility; Crawl just supplies the curve points.

## Migration / backfill note

No data migration needed. Existing JJAM26 rows are correct June '26 settlements
— they were just being read as if they were the spot. From 2026-05-19's
scraper run forward, JJAK26 (May), JJAN26 (July), and JJAM26 will all be in
the table with full backfilled history (Barchart returns the full settlement
series per CSV).

## Contact

Crawl-side issues with this feed: post in the COPAP Crawl channel or check
`/home/copapadmin/petrochem_scraper.log` on crawl-verify (180.20.0.4).
