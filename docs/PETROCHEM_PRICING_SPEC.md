# Petrochem Pricing Database — Spec for Salestracker Integration

## Overview

Shared PostgreSQL database for daily petrochemical price data. **Crawl writes, Salestracker reads.**

- **Host:** `crawl-monitor-db.postgres.database.azure.com`
- **DB:** `crawlmonitor` (existing) or new `petrochem_prices` DB
- **Writer:** Crawl petrochem scraper (daily, 01:00-03:00 UTC)
- **Reader:** Salestracker (for regression model, dashboards, valuation)

## Schema

### Table 1: `daily_prices` — scraped spot prices (Crawl writes daily)

```sql
CREATE TABLE daily_prices (
    id              BIGSERIAL PRIMARY KEY,
    scrape_date     DATE NOT NULL,              -- e.g. 2026-05-16
    source          VARCHAR(50) NOT NULL,        -- echemi, sunsirs, eia, barchart, jpx
    product         VARCHAR(100) NOT NULL,       -- Toluene, Benzene, Xylene, Kerosene, etc.
    region          VARCHAR(100),                -- FOB Korea, China Domestic, US Gulf, etc.
    price_type      VARCHAR(30) NOT NULL,        -- spot, domestic, international, regional
    price           DECIMAL(12,4) NOT NULL,      -- 1033.00, 6931.00, 4.265
    currency        VARCHAR(10) NOT NULL,        -- USD, CNY, JPY
    unit            VARCHAR(20) NOT NULL,        -- /mt, /gal, /bbl, /kl
    price_usd_mt    DECIMAL(12,4),              -- normalized to USD/MT for comparison
    change_amount   DECIMAL(12,4),              -- daily change if available
    change_pct      DECIMAL(8,4),               -- % change if available
    incoterm        VARCHAR(10),                 -- FOB, CIF, CFR (for international)
    raw_date        VARCHAR(50),                 -- date string from source (e.g. "May 15, 2026")
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(scrape_date, source, product, region, price_type)
);

CREATE INDEX idx_daily_prices_date ON daily_prices(scrape_date DESC);
CREATE INDEX idx_daily_prices_product ON daily_prices(product, scrape_date DESC);
CREATE INDEX idx_daily_prices_source ON daily_prices(source, scrape_date DESC);
```

### Table 2: `futures_prices` — forward curve data (Crawl writes daily)

```sql
CREATE TABLE futures_prices (
    id              BIGSERIAL PRIMARY KEY,
    scrape_date     DATE NOT NULL,              -- date we scraped it
    source          VARCHAR(50) NOT NULL,        -- barchart, jpx
    product         VARCHAR(100) NOT NULL,       -- Barge Kerosene, Dubai Crude Oil, NWE Naphtha Crack
    contract_month  VARCHAR(20) NOT NULL,        -- 202606, 202607, Jun '26
    settlement_price DECIMAL(12,4) NOT NULL,     -- settlement/last price
    currency        VARCHAR(10) NOT NULL,        -- USD, JPY
    unit            VARCHAR(20) NOT NULL,        -- /mt, /kl, /bbl
    price_usd_mt    DECIMAL(12,4),              -- normalized to USD/MT
    ticker          VARCHAR(20),                 -- INOK26, UAM26, CBN26
    exchange        VARCHAR(20),                 -- NYMEX, JPX/TOCOM, ICE
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(scrape_date, source, product, contract_month)
);

CREATE INDEX idx_futures_date ON futures_prices(scrape_date DESC);
CREATE INDEX idx_futures_product ON futures_prices(product, contract_month);
```

### Table 3: `monthly_anchors` — truth source prices (Salestracker writes monthly)

```sql
CREATE TABLE monthly_anchors (
    id              BIGSERIAL PRIMARY KEY,
    period          DATE NOT NULL,              -- first of month: 2026-05-01
    source          VARCHAR(50) NOT NULL,        -- fred_ppi, comtrade, intratec
    product         VARCHAR(100) NOT NULL,       -- Toluene, Benzene, Xylene, Aromatics PPI
    region          VARCHAR(100),                -- US Gulf, NEA FOB, China EXW, Global
    value           DECIMAL(12,4) NOT NULL,      -- index value or $/MT
    value_type      VARCHAR(20) NOT NULL,        -- index, usd_mt, derived
    unit            VARCHAR(20),                 -- index points, USD/MT
    series_id       VARCHAR(100),                -- FRED series ID, Comtrade HS code
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(period, source, product, region)
);

CREATE INDEX idx_monthly_period ON monthly_anchors(period DESC);
```

### Table 4: `synthetic_prices` — regression output (Salestracker writes daily)

```sql
CREATE TABLE synthetic_prices (
    id              BIGSERIAL PRIMARY KEY,
    price_date      DATE NOT NULL,
    product         VARCHAR(100) NOT NULL,       -- Toluene, Benzene, Xylene
    region          VARCHAR(100) NOT NULL,        -- US Gulf, NEA FOB, NWE CIF
    price_usd_mt    DECIMAL(12,4) NOT NULL,      -- synthetic price in USD/MT
    confidence_low  DECIMAL(12,4),               -- lower band
    confidence_high DECIMAL(12,4),               -- upper band
    model_version   VARCHAR(20),                 -- v1.0, v1.1
    anchor_source   VARCHAR(50),                 -- fred_ppi, intratec
    anchor_period   DATE,                        -- which monthly anchor was used
    inputs_json     JSONB,                       -- {naphtha: 650, crude: 105, spread: 120}
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(price_date, product, region)
);

CREATE INDEX idx_synthetic_date ON synthetic_prices(price_date DESC);
CREATE INDEX idx_synthetic_product ON synthetic_prices(product, price_date DESC);
```

## Data Flow

```
CRAWL (daily 01:00-03:00 UTC)
  Echemi spot prices ──────────┐
  Sunsirs spot prices ─────────┤
  EIA API benchmarks ──────────┼──→ daily_prices table
  Barchart futures ────────────┤    + blob (backup)
  JPX/TOCOM futures ───────────┤──→ futures_prices table
                               │
SALESTRACKER (monthly, 3rd business day)
  FRED PPI Aromatics API ──────┤
  UN Comtrade HS 2902.30 ──────┼──→ monthly_anchors table
  Intratec API (future) ───────┘
                               
SALESTRACKER (daily, after Crawl completes)
  Read daily_prices ───────────┐
  Read futures_prices ─────────┤
  Read monthly_anchors ────────┼──→ regression model ──→ synthetic_prices table
                               │
  Power BI / dashboards ◄──────┘
```

## What Crawl writes (178 records/day currently)

| Source | Records/Day | Table | Products |
|--------|-------------|-------|----------|
| Echemi ZYC pages | 20 | daily_prices | Toluene, Benzene, Xylene, Naphtha, Paraffin, etc. (China domestic) |
| Echemi price curves | 62 | daily_prices | Toluene, Benzene, Xylene (international FOB/CIF + regional) |
| Sunsirs | 28 | daily_prices | 28 Chinese commodities (Toluene, Xylene, Styrene, etc.) |
| EIA API | 20 | daily_prices | Jet Kero USGC, Brent Crude, WTI, Heating Oil (10 days each) |
| Barchart | 3 | futures_prices | NWE Naphtha Crack, SG Fuel Oil, ICE Brent (front month) |
| JPX/TOCOM | 45 | futures_prices | Kerosene, Gasoline, Dubai Crude (6-8 months forward) |

## What Salestracker writes

| Source | Frequency | Table | Products |
|--------|-----------|-------|----------|
| FRED PPI (PCU325110325110P) | Monthly | monthly_anchors | Aromatics index (benzene+toluene+xylene basket) |
| UN Comtrade (HS 2902.30) | Monthly | monthly_anchors | Toluene trade value/tonnage → derived $/MT |
| Intratec (future sub) | Monthly | monthly_anchors | Toluene US Gulf CIF, China EXW, NEA FOB |
| Regression model | Daily | synthetic_prices | Toluene, Benzene, Xylene (daily synthetic with confidence bands) |

## Free API Details for Salestracker

### FRED PPI Aromatics
```
GET https://api.stlouisfed.org/fred/series/observations
  ?series_id=PCU325110325110P
  &api_key=<free_key>
  &file_type=json
  &sort_order=desc
  &limit=12
```
- Free registration: https://fred.stlouisfed.org/docs/api/api_key.html
- No rate limit concerns (monthly data, 1 call/month)

### UN Comtrade
```
GET https://comtradeapi.un.org/data/v1/get/C/M/HS
  ?reporterCode=all
  &period=202605
  &partnerCode=0
  &cmdCode=290230
  &flowCode=X
```
- Free tier: 500 calls/day, no auth needed for basic queries
- HS 2902.30 = Toluene
- Returns: trade value (USD) + net weight (kg) → divide for $/MT

## Regression Model (for Salestracker to implement)

```python
# Simplified: Toluene ≈ Naphtha_price * coefficient + regional_spread
# Fit monthly against Intratec/FRED anchors
# Apply daily using EIA naphtha/crude inputs

# Toluene-to-naphtha correlation: ~85-90%
# Typical spread: $50-150/MT depending on aromatics tightness

toluene_usd_mt = (naphtha_usd_mt * beta) + alpha + seasonal_adjustment
confidence_band = ± (residual_std * 1.96)  # 95% CI
```

## Connection Details

```
Host: crawl-monitor-db.postgres.database.azure.com
Database: crawlmonitor
User: crawladmin
Password: Azure Key Vault → "db-password" in crawlkeyvault
SSL: required
Port: 5432
```

## Currency Conversion for price_usd_mt

| Currency | Conversion | Source |
|----------|------------|--------|
| CNY → USD | Divide by ~7.05 (fetch daily from EIA or FRED) | FRED DEXCHUS |
| JPY → USD | Divide by ~155 (fetch daily) | FRED DEXJPUS |
| USD | Already USD | — |
| Yen/kl → USD/MT | ÷ JPY rate, × density factor (~1.19 for kerosene) | — |
