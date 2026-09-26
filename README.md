# PSX Terminal Intelligence Suite — v2

Every quote, chart, company list and company profile comes from the official
Pakistan Stock Exchange data portal (`dps.psx.com.pk`) and corporate site
(`psx.com.pk`). No third-party middlemen, no invented tickers, no simulated
prices.

## Run it

    pip install -r requirements.txt
    streamlit run app.py

## What changed vs v1

| Issue in v1 | Fix in v2 |
|---|---|
| Silent fallback to synthetic random-walk prices | Removed entirely. If official data fails, the app shows an explicit error — never fake data. |
| 537 placeholder tickers (PSX001...) padded the roster | Real roster from PSX `/symbols` (1,020 scrips incl. debt) and live `/market-watch` (~495 equity scrips). |
| Hardcoded, partly wrong sector & company names | Official `/sector-summary/sectorwise` (39 sectors) + `data-srip` symbols straight from the live table. |
| Chart endpoint mismatch (intraday used for all timeframes) | One official series: `/timeseries/eod/<SYM>`; timeframes slice it properly (1M…MAX). |
| `hash(symbol)` seed made fake data non-reproducible | Moot — no synthetic data exists anymore. |
| API failures swallowed by bare `except` | Central `http_get` with retries, backoff, per-source health ledger surfaced in the UI. |
| Wrong market hours (2:30 Fri close etc.) | Official hours: Mon–Thu 09:15 pre-open → 15:30 close; Friday closes 12:30. |
| Unverifiable "intelligence" verdicts from mixed data | Trend reads computed only from official EOD history, clearly labeled. |

## Double-checking engine (Data Health tab)

Eight automated cross-checks run on every load and report every anomaly —
they never silently alter data:

1. **CHANGE consistency** — `CURRENT − LDCP` must equal reported CHANGE
2. **CHANGE% consistency** — implied % from LDCP vs reported %
3. **Circuit limit** — |change%| within ±10% (PSX standard breaker)
4. **OHLC sanity** — High ≥ max(Open, Current), Low ≤ min(Open, Current)
5. **LDCP vs EOD** — market-watch LDCP cross-checked against official
   `/timeseries/eod` previous close (sampled per load)
6. **Cross-source quote** — company-page close vs market-watch CURRENT
7. **Sector master** — sector codes validated against the official symbol list
8. **Freshness** — every source is timestamped; stale feeds are flagged

## Risk & Signal engine (inlined in app.py, before the market-hours section)

A composite advisory model that fuses five live factors — macro (SBP policy
rate, reserves, KIBOR, World Bank CPI/GDP), news/geopolitical sentiment
(Google News RSS + Pakistan-specific lexicon), company fundamentals (official
PSX financials/ratios), price perception (trend vs MAs, 52-week range,
drawdown, realised volatility from official EOD) and buying/selling flow
(intraday trade prints vs VWAP, price-volume correlation, 20-session return).

Each factor is scored −1…+1, combined by fixed weights (macro 22%, news 16%,
fundamentals 26%, perception 18%, flow 18%; re-normalised if a source is
missing) into a −100…+100 composite that yields **PULL IN / RETAIN / PULL OUT**
with full factor breakdown. Missing inputs degrade to "unknown" — never
simulated. Strictly analytical, not financial advice.

## Endpoints used (all official PSX)

- `GET dps.psx.com.pk/market-watch` — live quote table
- `GET dps.psx.com.pk/timeseries/eod/<SYM>` — daily history
- `GET dps.psx.com.pk/symbols` — full symbol master
- `GET dps.psx.com.pk/sector-summary/sectorwise` — sector stats
- `GET dps.psx.com.pk/company/<SYM>` — profile, financials, ratios, announcements
- `GET dps.psx.com.pk/company/reports/<SYM>` — report PDF links
- `GET dps.psx.com.pk/performers` — top active / advancers / decliners
- `GET dps.psx.com.pk/timeseries/int/<SYM>` — intraday tick prints (real trades)

Plus, for the risk engine: SBP (policy rate, reserves, KIBOR), World Bank API
(CPI, GDP growth), Google News RSS (market & geopolitical sentiment).

Data is used strictly under PSX's published terms; for commercial use or
redistribution contact PSX directly.
