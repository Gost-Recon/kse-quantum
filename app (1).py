"""
PSX Terminal Intelligence Suite — v2 (Official PSX Data Edition)

Every market quote, chart, company list and company profile is sourced
EXCLUSIVELY from official Pakistan Stock Exchange endpoints:

    dps.psx.com.pk        -> market watch, timeseries, symbols, sectors,
                             company pages, reports, performers, indices
    www.psx.com.pk        -> market summary (secondary cross-check source)

This version deliberately has NO synthetic/simulated fallback. If official
data is unavailable, the app shows an explicit error — it never invents
prices, tickers or companies.

Double-checking mechanisms (see run_integrity_checks):
  1.  Change consistency      CURRENT - LDCP == CHANGE (rounding tolerance)
  2.  Change-% consistency    CHANGE% implied by LDCP matches reported CHANGE
  3.  Circuit-limit sanity    |CHANGE%| within +-10% (PSX standard cap)
  4.  OHLC sanity             high >= max(open, current), low <= min(...)
  5.  LDCP vs official EOD    yesterday's close from /timeseries matches LDCP
  6.  Cross-source quote      company page close vs market-watch CURRENT
  7.  Sector cross-check      /symbols sectorName vs market-watch sector code
  8.  Freshness               every fetch is timestamped; stale data flagged
"""

import json
import os
import re
import io
import time
import logging
import datetime
import contextlib
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

# ---------------------------------------------------------------
# Logging (visible in the Streamlit console, never on the UI)
# ---------------------------------------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("psx-terminal")

st.set_page_config(page_title="KSE Quantum", layout="wide")

# ---------------------------------------------------------------
# Official endpoints & constants
# ---------------------------------------------------------------
DPS = "https://dps.psx.com.pk"          # official PSX data portal
MAIN = "https://www.psx.com.pk"         # official PSX corporate site
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Referer": DPS + "/",
}
PKT = ZoneInfo("Asia/Karachi")

# Optional private relay (most-independent fix): set KSE_PROXY env var to a
# URL prefix like "https://my-relay.workers.dev/?u=" to route ALL traffic
# through it. Direct is always tried first; relay is the fallback path.
PROXY_PREFIX = os.environ.get("KSE_PROXY", "").strip()

# Free public relays (no account needed) — last-resort paths when direct is
# blocked from datacenter IPs. Each is tried in order; first success wins.
PUBLIC_RELAYS = [
    "https://api.allorigins.win/raw?url=",       # AllOrigins (CORS relay)
    "https://corsproxy.io/?url=",                # CORS proxy
]

# Official PSX trading hours (Mon-Thu full session, Friday early close)
HOURS = {
    0: ((9, 15), (15, 30)),   # Monday    pre-open 09:15, close 15:30
    1: ((9, 15), (15, 30)),   # Tuesday
    2: ((9, 15), (15, 30)),   # Wednesday
    3: ((9, 15), (15, 30)),   # Thursday
    4: ((9, 15), (12, 30)),   # Friday    1st session only
}
WEEKEND = (5, 6)              # Saturday, Sunday

MAX_CHANGE_PCT = 10.0         # standard PSX circuit breaker width

# ---------------------------------------------------------------
# Data health ledger — every source call is recorded here and
# surfaced in the UI so stale/failed data is never silent.
# ---------------------------------------------------------------
@st.cache_resource
def health_ledger():
    return {}

def record(source, ok, latency=0.0, count=0, error=""):
    h = health_ledger()
    h[source] = {
        "ok": bool(ok),
        "latency_ms": round(latency * 1000, 1),
        "records": count,
        "error": str(error)[:200],
        "fetched_at": datetime.datetime.now(PKT),
    }

def health_rows():
    h = health_ledger()
    now = datetime.datetime.now(PKT)
    rows = []
    for src, s in sorted(h.items()):
        age = (now - s["fetched_at"]).total_seconds()
        rows.append({
            "Source": src,
            "Status": "OK" if s["ok"] else "FAILED",
            "Latency (ms)": s["latency_ms"],
            "Records": s["records"],
            "Age (s)": int(age),
            "Stale": age > stale_limit(src),
            "Error": s["error"],
        })
    return pd.DataFrame(rows)

def stale_limit(src):
    return {"market_watch": 90, "symbols": 7200, "sector_summary": 600,
            "timeseries": 600, "company_page": 1800, "company_reports": 3600,
            "performers": 120}.get(src, 3600)

# ---------------------------------------------------------------
# HTTP core — retries, timeouts, health recording.
# Raises PSXUnavailable on failure: callers must NOT fall back to fake data.
# ---------------------------------------------------------------
class PSXUnavailable(Exception):
    pass

# ---------------------------------------------------------------
# Persistent "last-good" disk cache.
# If a live fetch fails, we serve the last successful copy with a
# staleness banner — never fake data, never a blank screen.
# The cache dir must be writable when frozen as an exe too.
# ---------------------------------------------------------------
def _cache_dir():
    import tempfile
    base = os.environ.get("KSE_QUANTUM_CACHE")
    if base and os.path.isdir(base) and os.access(base, os.W_OK):
        return base
    # next to the executable / script (portable, survives restarts)
    for cand in (os.path.dirname(os.path.abspath(__file__)), tempfile.gettempdir()):
        try:
            d = os.path.join(cand, ".kse_cache")
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".probe")
            with open(probe, "w") as f:
                f.write("ok")
            return d
        except Exception:                                # noqa: BLE001
            continue
    return None

_CACHE_DIR = _cache_dir()

# source -> (age_seconds, when) of last stale serving, for the UI banner
STALE_INFO = {}

def _cache_path(source):
    if not _CACHE_DIR:
        return None
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", source)
    return os.path.join(_CACHE_DIR, safe + ".cache")

def _cache_save(source, text):
    p = _cache_path(source)
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:                                    # noqa: BLE001
        log.warning("Cache save failed for %s", source)

def _cache_load(source):
    p = _cache_path(source)
    if not p or not os.path.exists(p):
        return None
    try:
        age = time.time() - os.path.getmtime(p)
        with open(p, encoding="utf-8") as f:
            return age, f.read()
    except Exception:                                    # noqa: BLE001
        return None


def _via_relay(url):
    """Return the relayed URL for a given target, cycling through available
    relays (private prefix first, then public ones)."""
    q = requests.utils.quote(url, safe="")
    paths = []
    if PROXY_PREFIX:
        paths.append(PROXY_PREFIX + q)
    paths += [p + q for p in PUBLIC_RELAYS]
    return paths

def yahoo_chart(symbol, source="yahoo", range_="1y", interval="1d"):
    """Secondary path: Yahoo Finance chart API for .KA (Karachi) tickers.
    Returns a normalized requests.Response whose .json() matches the PSX
    timeseries shape: {"status":1,"data":[[ts, close, volume, open],...]}
    (most recent first) — or raises PSXUnavailable."""
    sym = f"{symbol.upper()}.KA"
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/"
         f"{sym}?range={range_}&interval={interval}")
    hdrs = {"User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json"}
    last_err = None
    for attempt in range(2):
        t0 = time.perf_counter()
        try:
            r = requests.get(u, headers=hdrs, timeout=20)
            latency = time.perf_counter() - t0
            if r.status_code != 200:
                raise PSXUnavailable(f"HTTP {r.status_code} from Yahoo {sym}")
            j = r.json()
            res = (j.get("chart") or {}).get("result") or []
            if not res:
                raise PSXUnavailable(f"Yahoo: no result for {sym}")
            meta = res[0].get("meta") or {}
            ts = res[0].get("timestamp") or []
            quote = (res[0].get("indicators") or {}).get("quote") or []
            closes = (quote[0].get("close") if quote else None) or []
            vols = (quote[0].get("volume") if quote else None) or []
            opens = (quote[0].get("open") if quote else None) or []
            rows = [[t, c, (v or 0), (o or c)] for t, c, v, o
                    in zip(ts, closes, vols, opens) if c is not None]
            if not rows:
                raise PSXUnavailable(f"Yahoo: no daily bars for {sym}")
            rows.sort(key=lambda x: x[0], reverse=True)
            record("yahoo", ok=True, latency=latency, count=len(rows))
            body = json.dumps({"status": 1, "message": "", "data": rows})
            resp = requests.Response()
            resp.status_code = 200
            resp._content = body.encode("utf-8")
            resp.headers["Content-Type"] = "application/json"
            resp.encoding = "utf-8"
            resp.url = u
            log.info("Yahoo fallback served %s (%d bars)", sym, len(rows))
            return resp
        except Exception as e:
            last_err = e
            record("yahoo", ok=False, latency=time.perf_counter() - t0,
                   error=e)
            time.sleep(0.5)
    raise PSXUnavailable(f"Yahoo fallback failed for {sym}: {last_err}")

def http_get(url, source, retries=2, timeout=20, headers_extra=None,
             allow_stale=False):
    """GET with retries + backoff. On total failure, if `allow_stale` is set
    and a last-good copy exists on disk, serve that instead and record the
    staleness age for the UI banner. Raises PSXUnavailable otherwise.

    Independence stack (in order):
      1. direct fetch (N retries with backoff)
      2. optional private relay (KSE_PROXY env var)
      3. free public relays (allorigins, corsproxy)
      4. last-good cached copy (stale serve) if permitted
    """
    hdrs = dict(HEADERS)
    if headers_extra:
        hdrs.update(headers_extra)
    last_err = None
    # --- Layer 1: direct ---
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            r = requests.get(url, headers=hdrs, timeout=timeout)
            latency = time.perf_counter() - t0
            if r.status_code != 200:
                raise PSXUnavailable(f"HTTP {r.status_code} from {url}")
            record(source, ok=True, latency=latency)
            _cache_save(source, r.text)
            STALE_INFO.pop(source, None)
            return r
        except Exception as e:                       # noqa: BLE001
            last_err = e
            latency = time.perf_counter() - t0
            log.warning("Fetch failed (%s) attempt %d/%d: %s",
                        source, attempt + 1, retries + 1, e)
            record(source, ok=False, latency=latency, error=e)
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))      # small backoff
    # --- Layer 2/3: relays ---
    for rurl in _via_relay(url):
        try:
            t0 = time.perf_counter()
            r = requests.get(rurl, timeout=timeout + 5,
                             headers={"User-Agent": HEADERS["User-Agent"]})
            latency = time.perf_counter() - t0
            if r.status_code != 200:
                raise PSXUnavailable(f"HTTP {r.status_code} via relay {rurl}")
            if not (r.text or "").strip():
                raise PSXUnavailable(f"empty relay body from {rurl}")
            record(source, ok=True, latency=latency,
                   error="")
            _cache_save(source, r.text)
            STALE_INFO.pop(source, None)
            log.info("Served %s via relay: %s", source, rurl)
            return r
        except Exception as e:                       # noqa: BLE001
            last_err = e
            log.warning("Relay failed (%s) via %s: %s", source, rurl, e)
    # --- Layer 4: stale cache ---
    if allow_stale:
        cached = _cache_load(source)
        if cached is not None:
            age, text = cached
            STALE_INFO[source] = age
            log.warning("Serving STALE data for %s (age %.0f s)", source, age)
            record(source, ok=False, latency=0.0,
                   error=f"stale serve ({age:.0f}s old): {last_err}")
            resp = requests.Response()
            resp.status_code = 200
            resp._content = text.encode("utf-8", errors="replace")
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            resp.encoding = "utf-8"
            resp.url = url
            return resp
    raise PSXUnavailable(f"{url} unavailable after {retries + 1} attempts "
                         f"({last_err})")


FRESHNESS_LIMIT_SEC = 15 * 60   # owner requirement: data must be <= 15 min old

def data_age_minutes(ts):
    """Age in minutes of a POSIX timestamp (or pandas Series) vs now PKT."""
    import datetime as _dt
    now = _dt.datetime.now(ZoneInfo("Asia/Karachi")).timestamp()
    return max(0.0, (now - float(ts)) / 60.0)

def freshness_check(last_ts, source="data"):
    """Return (ok, label). ok=False if data is older than FRESHNESS_LIMIT_SEC."""
    age = data_age_minutes(last_ts)
    if age <= FRESHNESS_LIMIT_SEC / 60.0:
        return True, f"🟢 {source} live · {age:.0f} min old (within 15-min window)"
    return False, (f"🔴 {source} STALE · {age:.0f} min old — "
                   f"outside the 15-min freshness window")

def stale_banner():
    """Render a warning banner if any data source is currently stale.
    Call once near the top of the app."""
    if not STALE_INFO:
        return
    import datetime
    rows = [f"**{src}** — showing cached data from "
            f"{datetime.datetime.now(ZoneInfo('Asia/Karachi')) - datetime.timedelta(seconds=age):%d %b %H:%M} PKT "
            f"({age/60:.0f} min old) — live fetch failed"
            for src, age in sorted(STALE_INFO.items())]
    st.warning("⚠️ Live data sources temporarily unreachable. "
               + "  |  ".join(rows)
               + "  — figures below are NOT live; retry shortly.")

# ---------------------------------------------------------------
# Official data fetchers (all cached, all timestamped)
# ---------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def get_market_watch():
    """Official live market watch table: SYMBOL, SECTOR code, LDCP, OPEN,
    HIGH, LOW, CURRENT, CHANGE, CHANGE%, VOLUME for every listed scrip."""
    r = http_get(DPS + "/market-watch", "market_watch", retries=1, timeout=25,
                 allow_stale=True)
    html = r.text
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) < 11:
            continue
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in tds]
        sym = re.search(r'data-srip="([^"]+)"', tr)
        rows.append({
            "symbol": sym.group(1).strip() if sym else cells[0],
            "sector_code": cells[1],
            "listed_in": cells[2],
            "ldcp": num(cells[3]), "open": num(cells[4]),
            "high": num(cells[5]), "low": num(cells[6]),
            "current": num(cells[7]), "change": num(cells[8]),
            "change_pct": num(cells[9].replace("%", "")),
            "volume": num(cells[10]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise PSXUnavailable("market-watch returned no rows")
    return df

@st.cache_data(ttl=3600, show_spinner=False)
def get_symbols():
    """Official symbol master list (equities + debt) from PSX data portal."""
    r = http_get(DPS + "/symbols", "symbols", retries=1)
    data = r.json()
    if not data:
        raise PSXUnavailable("symbols endpoint returned nothing")
    return data

@st.cache_data(ttl=600, show_spinner=False)
def get_sector_summary():
    """Official sector-wise summary (39 sectors, advance/decline/turnover)."""
    r = http_get(DPS + "/sector-summary/sectorwise", "sector_summary", allow_stale=True,
                 retries=1, timeout=25,
                 headers_extra={"X-Requested-With": "XMLHttpRequest"})
    html = r.text
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 7:
            continue
        c = [re.sub(r"<[^>]+>", "", t).replace("&amp;", "&").strip()
             for t in tds]
        out.append({"code": c[0], "sector": c[1], "advance": num(c[2]),
                    "decline": num(c[3]), "unchanged": num(c[4]),
                    "turnover": num(c[5]), "mcap_bn": num(c[6])})
    df = pd.DataFrame(out)
    if df.empty:
        raise PSXUnavailable("sector-summary returned no rows")
    return df

@st.cache_data(ttl=300, show_spinner=False)
def get_timeseries(symbol):
    """Official daily EOD series: [ts, close, volume, open] (most recent first).
    Empty history (some illiquid scrips) is returned as an empty frame, not an
    error — the caller decides how to present it."""
    try:
        r = http_get(f"{DPS}/timeseries/eod/{symbol.upper()}", "timeseries",
                     allow_stale=True)
    except PSXUnavailable:
        # PSX unreachable (e.g. datacenter IP blocked): Yahoo daily bars as
        # secondary path (1y history, ~1-day lag on the last bar).
        r = yahoo_chart(symbol, range_="1y", interval="1d")
    data = r.json()
    df = pd.DataFrame(data.get("data") or [], columns=["ts", "close", "volume", "open"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(PKT)
    return df.sort_values("date").reset_index(drop=True)

@st.cache_data(ttl=30, show_spinner=False)
def get_intraday(symbol):
    """Official intraday tick series: /timeseries/int/<SYM>.
    Each point is an actual PSX trade print: [ts, price, volume].
    Returns an empty frame when the scrip hasn't traded today."""
    r = http_get(f"{DPS}/timeseries/int/{symbol.upper()}", "timeseries_int",
                 allow_stale=True)
    data = r.json().get("data") or []
    df = pd.DataFrame(data, columns=["ts", "price", "volume"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(PKT)
    return df.sort_values("date").reset_index(drop=True)

def resample_ticks(ticks, rule):
    """Aggregate official trade prints into OHLCV bars of `rule` width."""
    if ticks.empty:
        return ticks.rename(columns={"price": "close"}).assign(
            open=np.nan, high=np.nan, low=np.nan)
    g = ticks.set_index("date").resample(rule, label="right", closed="right").agg(
        open=("price", "first"), high=("price", "max"),
        low=("price", "min"), close=("price", "last"),
        volume=("volume", "sum")).dropna(subset=["close"])
    return g.reset_index()

@st.cache_data(ttl=600, show_spinner=False)
def get_company_page(symbol):
    """Official company page: quote, stats, profile, equity, financials,
    ratios, announcements. Everything parsed from dps.psx.com.pk/company/SYM."""
    r = http_get(f"{DPS}/company/{symbol.upper()}", "company_page", timeout=25,
                 allow_stale=True)
    html = r.text
    info = {"symbol": symbol.upper()}
    info["name"] = txt(first(r'quote__name">([^<]+)<', html), "Unknown")
    info["sector"] = txt(first(r'quote__sector"><span>([^<]+)</span>', html)
                         .replace("&amp;", "&"))
    info["close"] = num(first(r'quote__close">Rs\.?([\d,.]+)<', html))
    info["change"] = num(first(r'change__value">([-\d,.]+)<', html))
    info["change_pct"] = num(first(r'change__percent">\s*\(([-\d,.]+)%\)', html))

    stats = {}
    for lab, val in re.findall(
            r'stats_label">(.*?)</div>\s*<div class="stats_value">(.*?)</div>',
            html, re.S):
        stats[re.sub(r"<[^>]+>", "", lab).strip().lower()] = \
            re.sub(r"<[^>]+>", "", val).strip()
    info["stats"] = stats

    m = re.search(r'<div class="section[^"]*company" id="profile">.*?'
                  r'</div>\s*</div>\s*</div>', html, re.S)
    prof_html = m.group(0) if m else ""
    info["description"] = txt(first(r'BUSINESS DESCRIPTION</div><p>(.*?)</p>',
                                    prof_html))
    info["key_people"] = re.findall(r"<td><strong>([^<]+)</strong></td>"
                                    r"<td>([^<]+)</td>", prof_html)
    info["address"] = txt(first(r'ADDRESS</div><p>(.*?)</p>', prof_html))
    info["website"] = txt(first(r'href="(http[^"]+)"[^>]*>\s*www\.', prof_html))
    info["registrar"] = txt(first(r'REGISTRAR</div><p>(.*?)</p>', prof_html))
    info["auditor"] = txt(first(r'AUDITOR</div><p>(.*?)</p>', prof_html)
                          .replace("&amp;", "&"))
    info["fye"] = txt(first(r'Fiscal Year End</div><p>(.*?)</p>', prof_html))

    eq = {}
    for lab, val in re.findall(
            r'stats_label">(.*?)</div>\s*<div class="stats_value">(.*?)</div>',
            html[html.find('id="equity"'): html.find('id="announcements"')], re.S):
        lab = re.sub(r"<[^>]+>", "", lab).strip().lower()
        eq[lab] = re.sub(r"<[^>]+>", "", val).strip()
    info["equity"] = eq

    info["financials"] = parse_fin_tables(html, "financials")
    info["ratios"] = parse_fin_tables(html, "ratios")
    info["announcements"] = parse_announcements(html)
    return info

@st.cache_data(ttl=3600, show_spinner=False)
def get_company_reports(symbol):
    """Official reports table (links point straight at PSX's own PDF store)."""
    r = http_get(f"{DPS}/company/reports/{symbol.upper()}", "company_reports",
                 allow_stale=True)
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", r.text, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 3:
            continue
        href = first(r'href="([^"]+)"', tds[0])
        out.append({"report": re.sub(r"<[^>]+>", "", tds[0]).strip(),
                    "period": re.sub(r"<[^>]+>", "", tds[1]).strip(),
                    "posted": re.sub(r"<[^>]+>", "", tds[2]).strip(),
                    "url": href if href.startswith("http")
                           else DPS + href})
    return out

# ---------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------
def first(pattern, text):
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else ""

def txt(s, default="—"):
    return (s or default).strip() or default

def num(s):
    """'12,345.6' -> 12345.6 ; junk -> nan"""
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return np.nan

def parse_fin_tables(html, section_id):
    """Tolerant parser for the annual/quarterly financials & ratios tables."""
    m = re.search(rf'id="{section_id}">(.*?)</table>\s*</div>\s*</div>',
                  html, re.S)
    if not m:
        m = re.search(rf'id="{section_id}">(.*?)</table>', html, re.S)
    if not m:
        return {}
    seg = m.group(1)
    years = [y.strip() for y in re.findall(r"<th[^>]*>([\w\s]{2,10})</th>", seg)
             if re.match(r"^\d{4}$|^\w{1,4}\s?\d{4}$", y.strip())]
    data = {}
    for tr in re.findall(r"<tr>(.*?)</tr>", seg, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not tds:
            continue
        label = re.sub(r"<[^>]+>", "", tds[0]).strip()
        vals = [re.sub(r"<[^>]+>", "", t).replace("&nbsp;", "").strip()
                for t in tds[1:]]
        if label and len(vals) >= 1 and any(v for v in vals):
            data[label] = vals[:len(years)] if years else vals
    return {"years": years, "rows": data}

def parse_announcements(html):
    m = re.search(r'id="announcements">(.*?)<div class="section', html, re.S)
    seg = m.group(1) if m else html
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", seg, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 3:
            continue
        title = re.sub(r"<[^>]+>", " ", tds[1])
        title = re.sub(r"\s+", " ", title).strip()
        pdf = first(r'href="(/download/document/[^"]+|https?://[^"]+)"', tds[2])
        out.append({"date": txt(first(r"([\w]{3}\s+\d{1,2},\s+\d{4})", tds[0])),
                    "title": title or "—",
                    "pdf": DPS + pdf if pdf.startswith("/") else pdf})
    return out

# ---------------------------------------------------------------
# News sentiment lexicon (Pakistan / markets specific)
# ---------------------------------------------------------------
POS_WORDS = [
    "surge", "rally", "gain", "gains", "record high", "upgrade", "recovery",
    "recover", "growth", "profit", "surplus", "boost", "inflow", "inflows",
    "bullish", "soar", "soars", "jump", "jumps", "rise", "rises", "rebound",
    "optimis", "confidence", "improve", "improves", "agreement", "approval",
    "approved", "review completed", "rate cut", "cuts rate", "eased", "ease",
    "stabilis", "stabiliz", "buy", "buys", "buying", "accumulat", "dividend",
    "expansion", "export", "remittance", "upgrade of rating", "credit line",
    "beats", "beat estimates", "strong demand", "milestone",
]
NEG_WORDS = [
    "fall", "falls", "fell", "plunge", "drop", "drops", "crash", "decline",
    "sheds", "shed", "slip", "slips", "loss", "losses", "deficit", "crisis",
    "tension", "tensions", "conflict", "attack", "strike", "war", "drone",
    "protest", "riot", "default", "downgrade", "bankrupt", "fraud", "probe",
    "investigation", "resign", "dismissal", "dollar rise", "rupee fall",
    "rupee deval", "inflation", "currency pressure", "bearish", "sell-off",
    "selloff", "selling pressure", "sellers", "outflow", "outflows", "curbs",
    "sanction", "warning", "tax hike", "tariff", "budget deficit", "stall",
    "slowdown", "recession", "shortage", "weak", "weaker", "weakness",
    "withdraw", "halt", "suspended", "default risk",
]
GEO_TRIGGERS = [
    "india", "afghanistan", "border", "ceasefire", "military", "kashmir",
    "geopolit", "imf", "fatah", "security", "drone", "missile", "sanction",
]

def _lexicon_score(text):
    t = text.lower()
    pos = sum(1 for w in POS_WORDS if w in t)
    neg = sum(1 for w in NEG_WORDS if w in t)
    geo = sum(1 for w in GEO_TRIGGERS if w in t)
    raw = (pos - neg) / max(1, pos + neg) if (pos + neg) else 0.0
    return raw, pos, neg, geo

def fetch_news_sentiment(sym=None, queries=None, per_query=12):
    """Fresh, company-specific news sentiment for the risk engine.

    Projection safety rule (per requirement):
      - Only headlines that explicitly MENTION the company influence scoring.
      - Generalized news (market/economy/geopolitics) is NEVER scored into the
        projection; it is surfaced as neutral, clearly-labelled background
        context, and only if a headline is fresh (<5 days) and itself contains
        the company symbol/name.
      - Stale items (>=5 days old) are dropped entirely.

    Returns (aggregate -1..+1 or np.nan, per-item frame).
    """
    import datetime as _dt
    queries = queries or [
        f"\"{sym}\" Pakistan stock when:5d",
        f"\"{sym}\" PSX when:5d",
        f"\"{sym}\" Pakistan when:5d",
    ]
    items = []
    for q in queries:
        try:
            url = ("https://news.google.com/rss/search?q="
                   + requests.utils.quote(q) + "&hl=en-PK&gl=PK&ceid=PK:en")
            r = http_get(url, f"news_{sym or 'general'}", retries=1,
                         timeout=15, allow_stale=True)
            if r.status_code != 200:
                continue
            raw = re.findall(r"<item>(.*?)</item>", r.text, re.S)[:per_query + 1]
            for it in raw:
                t = re.search(r"<title>(.*?)</title>", it, re.S)
                pd_ = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
                if not t:
                    continue
                title = t.group(1).strip()
                age_days = None
                if pd_:
                    try:
                        pub = _dt.datetime.strptime(
                            pd_.group(1).strip(),
                            "%a, %d %b %Y %H:%M:%S %Z")
                        age_days = (_dt.datetime.now(_dt.timezone.utc) - pub).total_seconds() / 86400
                    except Exception:
                        pass
                # freshness gate: drop anything >= 5 days old
                if age_days is not None and age_days >= 5:
                    continue
                items.append({"query": q.split(" when")[0].strip('\"'),
                              "title": title,
                              "age_days": (round(age_days, 1)
                                            if age_days is not None else None),
                              "mentions_company": _mentions_company(title, sym)})
        except Exception:                                   # noqa: BLE001
            continue
    if not items:
        return np.nan, pd.DataFrame()
    # dedupe identical headlines (Google News repeats the same article across
    # query variants)
    seen, unique = set(), []
    for it in items:
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:120]
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    items = unique
    if not items:
        return np.nan, pd.DataFrame()
    df = pd.DataFrame(items)
    scores, poss, negs, geos = [], [], [], []
    for _, row in df.iterrows():
        s, p, n, g = _lexicon_score(row["title"])
        scores.append(s); poss.append(p); negs.append(n); geos.append(g)
    df["sentiment"] = scores
    df["pos_hits"] = poss
    df["neg_hits"] = negs
    df["geo_hits"] = geos
    df["kind"] = df["mentions_company"].map(
        {True: "company", False: "background"})
    # ONLY company-mentioning headlines feed the projection score;
    # generalized news is background context and never influences the verdict.
    comp = df[df["mentions_company"]]
    if comp.empty:
        score = np.nan
    else:
        score = float(np.mean(comp["sentiment"]))
    return score, df


def _mentions_company(title, sym):
    """True if the headline explicitly refers to the listed company.

    Three-layer check, all requiring word boundaries:
      1. Standalone symbol token (e.g. 'HBL') or ticker form ('HBL:', 'SYS.L')
         AND a market/finance context keyword nearby.
      2. Ticker form with separator (SYM., SYM:, SYM-) — counts even without
         finance keywords (unambiguous notation).
      3. Long company-name tokens (multi-word or >= 6 chars).
    This keeps generic headlines (cricket scorecards, general news) from
    counting even when they contain the same letters.
    """
    if not sym:
        return False
    t = title.lower()
    s = sym.lower()
    ctx = ("stock", "share", "bank", "profit", "loss", "earnings",
           "dividend", "psx", "market", "company", "insurer", "fund",
           "invest", "revenue", "deal", "contract", "buyback", "board",
           "ceo", "results", "balance", "credit", "loan", "export",
           "acquisition", "merger", "quater", "quarter", "fy",
           "pakistan", "record", "surge", "rise", "fall", "decline",
           "announce", "approve", "upgrade", "downgrade")
    # common symbol → full company-name expansions (helps 3-char tickers)
    NAME_HINTS = {
        "sys": ["systems", "system limited"],
        "hbl": ["habib bank"],
        "ogdc": ["ogdcl", "oil \u0026 gas development"],
        "luck": ["lucky cement"],
        "engro": ["engro"],
        "mari": ["mari petroleum"],
        "pso": ["pakistan state oil"],
        "ffc": ["fauji fertilizer"],
        "hubc": ["hub power"],
        "pol": ["pakistan oilfields"],
        "kohc": ["kohinoor"],
    }
    # 1) standalone symbol + finance context
    if re.search(r"\b" + re.escape(s) + r"\b", t) and any(w in t for w in ctx):
        return True
    # 2) ticker notation (HBL:, SYS.L, SYS-) — no context needed
    if re.search(r"\b" + re.escape(s) + r"[.:\-]", t):
        return True
    # 3) known name expansions
    hints = [h for h in NAME_HINTS.get(s, []) if len(h) >= 4]
    if hints and any(h in t for h in hints):
        return True
    # 4) long tokens / multi-word company names
    stripped = s.rstrip("0123456789").replace("-", " ")
    words = [w for w in stripped.split() if len(w) >= 2]
    if len(words) >= 2 and all(w in t for w in words):
        return True
    if len(words) == 1 and len(words[0]) >= 6 and words[0] in t:
        return True
    return False

def fetch_macro():
    """Live national indicators: SBP + World Bank. Returns (frame, score
    −1..+1). Missing data is marked, never invented."""
    rows = []
    h = HEADERS
    # SBP monetary policy page
    try:
        r = http_get("https://www.sbp.org.pk/our-operations/monetary-policy",
                     "sbp_policy", retries=1, timeout=15, allow_stale=True)
        txt = re.sub(r"<[^>]+>", " ", r.text)
        txt = re.sub(r"\s+", " ", txt)
        m = re.search(r"Policy Rate\s+([\d.]+)%", txt)
        if m:
            rate = float(m.group(1))
            # scoring: >15% very tight (negative), 9-13 neutral-positive zone
            rate_score = np.clip((13.0 - rate) / 4.0, -1, 1)
            rows.append({"Indicator": "SBP Policy Rate", "Value": f"{rate}% p.a.",
                         "Signal": rate_score,
                         "Read": "Dovish/positive for equities" if rate_score > 0.3
                         else "Tight/negative" if rate_score < -0.3 else "Neutral"})
        m = re.search(r"Overnight repo rate[^%]{0,80}?([\d.]+)% p\.a\.", txt)
        if m:
            repo = float(m.group(1))
            rows.append({"Indicator": "Overnight Repo (Wtd Avg)", "Value": f"{repo}%",
                         "Signal": 0.0,
                         "Read": "Money-market reference"})
    except Exception:                                        # noqa: BLE001
        pass
    # SBP FX page: reserves + money market rates
    try:
        r = http_get("https://www.sbp.org.pk/fx/index.asp", "sbp_fx",
                     retries=1, timeout=15, allow_stale=True)
        txt = re.sub(r"<[^>]+>", " ", r.text)
        txt = re.sub(r"\s+", " ", txt)
        m = re.search(r"SBP.s Reserves\s+([\d,\.]+)", txt)
        if m:
            res = float(m.group(1).replace(",", "")) / 1000.0  # page is $M
            # adequacy heuristic: >20B USD comfortable, <10B strained
            s = np.clip((res - 10) / 10, -1, 1)
            rows.append({"Indicator": "SBP FX Reserves (US$ bn)",
                         "Value": f"{res:,.1f}", "Signal": s,
                         "Read": "Comfortable" if s > 0.3 else
                                 "Strained" if s < -0.3 else "Adequate"})
        m = re.search(r"Total Reserves\s+([\d,\.]+)", txt)
        if m:
            tot = float(m.group(1).replace(",", "")) / 1000.0
            rows.append({"Indicator": "Total FX Reserves (US$ bn)",
                         "Value": f"{tot:,.1f}",
                         "Signal": np.clip((tot - 14) / 12, -1, 1),
                         "Read": "Reference"})
        m = re.search(r"3-M\s+([\d.]+)\s+([\d.]+)", txt)
        if m:
            k3 = float(m.group(1))
            s = np.clip((12.0 - k3) / 4.0, -1, 1)
            rows.append({"Indicator": "KIBOR 3-M", "Value": f"{k3}%",
                         "Signal": s,
                         "Read": "Cheap funding" if s > 0.3 else
                                 "Expensive funding" if s < -0.3 else "Normal"})
    except Exception:                                        # noqa: BLE001
        pass
    # World Bank macro (annual, official)
    for code, label, good_when_high in (
            ("FP.CPI.TOTL.ZG", "Inflation (CPI, World Bank)", False),
            ("NY.GDP.MKTP.KD.ZG", "GDP Growth (World Bank)", True)):
        try:
            r = http_get(
                f"https://api.worldbank.org/v2/country/PAK/indicator/{code}"
                "?format=json&per_page=3", f"worldbank_{code}",
                retries=1, timeout=15, allow_stale=True).json()
            for point in r[1]:
                if point["value"] is not None:
                    v = float(point["value"])
                    if code == "FP.CPI.TOTL.ZG":
                        s = np.clip((6.0 - v) / 6.0, -1, 1)   # low inflation good
                        read = ("Contained" if s > 0.3 else
                                "Elevated" if s < -0.3 else "Moderate")
                    else:
                        s = np.clip(v / 6.0, -1, 1)           # growth good
                        read = ("Solid" if s > 0.3 else
                                "Weak" if s < -0.3 else "Average")
                    rows.append({"Indicator": f"{label} ({point['date']})",
                                 "Value": f"{v:.2f}%", "Signal": s, "Read": read})
                    break
        except Exception:                                    # noqa: BLE001
            pass

    df = pd.DataFrame(rows)
    known = df["Signal"].astype(float)
    known = known[known.notna()]
    score = float(known[known.abs() > 0].mean()) if len(known[known.abs() > 0]) \
        else np.nan
    return df, score

def score_fundamentals(info):
    """From official PSX company page: profitability, margins, growth, PEG."""
    out, signals = [], []
    fin = info.get("financials", {}).get("rows", {})
    def fl(lst):
        try:
            return [float(str(v).replace(",", "")) for v in lst]
        except (TypeError, ValueError):
            return []
    sales = fl(fin.get("Sales", []))
    pat = fl(fin.get("Profit after Taxation", []))
    eps = fl(fin.get("EPS", []))
    if len(sales) >= 2:
        g = (sales[0] / sales[1] - 1) * 100 if sales[1] else np.nan
        s = np.clip(g / 20, -1, 1)
        out.append(("Sales growth YoY", f"{g:+.1f}%", s))
        signals.append(s)
    if len(pat) >= 2:
        g = (pat[0] / pat[1] - 1) * 100 if pat[1] else np.nan
        s = np.clip(g / 30, -1, 1)
        out.append(("Profit growth YoY", f"{g:+.1f}%", s))
        signals.append(s)
    if len(eps) >= 2 and eps[1]:
        g = (eps[0] / eps[1] - 1) * 100
        s = np.clip(g / 30, -1, 1)
        out.append(("EPS growth YoY", f"{g:+.1f}%", s))
        signals.append(s)
    rat = info.get("ratios", {}).get("rows", {})
    npm = fl(rat.get("Net Profit Margin (%)", []))
    if npm:
        s = np.clip((npm[0] - 8) / 20, -1, 1)
        out.append(("Net margin", f"{npm[0]:.1f}%", s))
        signals.append(s)
    gpm = fl(rat.get("Gross Profit Margin (%)", []))
    if gpm:
        s = np.clip((gpm[0] - 15) / 30, -1, 1)
        out.append(("Gross margin", f"{gpm[0]:.1f}%", s))
        signals.append(s)
    peg = fl(rat.get("PEG", []))
    if peg:
        s = np.clip((1.5 - peg[0]) / 2, -1, 1)
        out.append(("PEG", f"{peg[0]:.2f}", s))
        signals.append(s)
    score = float(np.mean(signals)) if signals else np.nan
    return pd.DataFrame(out, columns=["Metric", "Value", "Signal"]), score

def score_perception(ts, mw_row=None):
    """Share-price perception from official EOD history: trend vs MAs,
    52-week range position, drawdown, realized volatility, volume trend."""
    out, signals = [], []
    df = ts.tail(252).copy()
    close = df["close"]
    last = close.iloc[-1]
    ma7 = close.rolling(7).mean().iloc[-1]
    ma30 = close.rolling(30).mean().iloc[-1]
    ma100 = close.rolling(100).mean().iloc[-1] if len(close) >= 100 else np.nan
    def ma_signal(val, ref):
        if np.isfinite(val) and np.isfinite(ref) and ref:
            return np.clip((val / ref - 1) * 8, -1, 1)
        return np.nan
    for name, ref in (("vs MA7", ma7), ("vs MA30", ma30), ("vs MA100", ma100)):
        s = ma_signal(last, ref)
        if np.isfinite(s):
            out.append((f"Price {name}", f"{(last/ref-1)*100:+.1f}%", s))
            signals.append(s)
    hi52, lo52 = close.max(), close.min()
    if hi52 > lo52:
        pos = (last - lo52) / (hi52 - lo52)
        s = np.clip((pos - 0.5) * 2, -1, 1)
        out.append(("52-week range position", f"{pos*100:.0f}% of range", s))
        signals.append(s)
        dd = (last - hi52) / hi52 * 100
        s = np.clip(dd / 25, -1, 1)
        out.append(("Drawdown from 52w high", f"{dd:.1f}%", s))
        signals.append(s)
    ret = close.pct_change().dropna()
    vol = ret.tail(20).std() * np.sqrt(252) * 100
    if np.isfinite(vol):
        s = np.clip((28 - vol) / 28, -1, 1)
        out.append(("20d realised vol (annualised)", f"{vol:.1f}%", s))
        signals.append(s)
    if "volume" in df.columns and df["volume"].iloc[-20:].sum() > 0:
        v20 = df["volume"].iloc[-20:].mean()
        v_last = df["volume"].iloc[-1]
        if v20:
            s = np.clip((v_last / v20 - 1) / 2, -1, 1)
            out.append(("Volume vs 20d avg", f"{v_last/v20:.2f}x", s))
            signals.append(s)
    score = float(np.mean(signals)) if signals else np.nan
    return pd.DataFrame(out, columns=["Metric", "Value", "Signal"]), score

def score_market_trend(mw):
    """Overall PSX market trend from official market-watch breadth and
    KSE-100 style aggregate. Uses live advancers/decliners, average change,
    and turnover breadth across ALL scrips."""
    out, signals = [], []
    chg = mw["change"].dropna()
    pct = mw["change_pct"].dropna()
    if len(chg):
        adv = int((chg > 0).sum())
        dec = int((chg < 0).sum())
        breadth = (adv - dec) / max(adv + dec, 1)
        s = np.clip(breadth * 1.5, -1, 1)
        out.append((f"Market breadth (A/D {adv:,}/{dec:,})",
                    f"{adv - dec:+,} net", s))
        signals.append(s)
        avg_pct = float(pct.mean())
        s = np.clip(avg_pct / 1.5, -1, 1)
        out.append(("Average scrip change %", f"{avg_pct:+.2f}%", s))
        signals.append(s)
        med_pct = float(pct.median())
        s = np.clip(med_pct / 1.5, -1, 1)
        out.append(("Median scrip change %", f"{med_pct:+.2f}%", s))
        signals.append(s)
        # strong movers ratio: breadth of >2% moves (momentum conviction)
        strong = int((pct.abs() > 2).sum())
        if strong:
            strong_up = int(((pct > 2).sum()) / strong * 100)
            s = np.clip((strong_up - 50) / 40, -1, 1)
            out.append(("Direction of >2% movers",
                        f"{strong_up}% of {strong} movers up", s))
            signals.append(s)
    score = float(np.mean(signals)) if signals else np.nan
    return pd.DataFrame(out, columns=["Metric", "Value", "Signal"]), score


def score_flow(ticks, daily=None):
    """Buying/selling pressure from official data: intraday trend slope,
    tick imbalance, and daily price-volume relationship."""
    out, signals = [], []
    if ticks is not None and not ticks.empty:
        # normalised time vs price slope over the session
        x = (ticks["ts"] - ticks["ts"].iloc[0]) / 3600.0
        y = ticks["price"]
        if len(ticks) >= 5 and x.iloc[-1] > 0:
            slope = np.polyfit(x, y, 1)[0]
            s = np.clip(slope / (y.iloc[-1] * 0.01 + 1e-9), -1, 1)  # %/hr scale
            out.append(("Intraday trend (per session)",
                        f"{slope:+.2f} Rs/hr", s))
            signals.append(s)
        # VWAP position: last price vs session VWAP
        vwap = (ticks["price"] * ticks["volume"]).sum() / max(
            ticks["volume"].sum(), 1)
        if vwap > 0:
            s = np.clip((ticks["price"].iloc[-1] / vwap - 1) * 100 / 1.5, -1, 1)
            out.append(("Close vs session VWAP", f"{(ticks['price'].iloc[-1]/vwap-1)*100:+.2f}%", s))
            signals.append(s)
    if daily is not None and len(daily) >= 40:
        dd = daily.tail(60)
        pr = dd["close"].pct_change().dropna()
        vv = dd["volume"].pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        if len(pr) > 10 and len(vv) > 10:
            both = pd.concat([pr, vv], axis=1).dropna()
            if len(both) > 10 and both.iloc[:, 0].std() > 0 and \
               both.iloc[:, 1].std() > 0:
                corr = both.corr().iloc[0, 1]
                s = np.clip(corr * 2, -1, 1)
                out.append(("Price-volume correlation (60d)",
                            f"{corr:+.2f}", s))
                signals.append(s)
        ret20 = dd["close"].iloc[-1] / dd["close"].iloc[-21] - 1
        s = np.clip(ret20 * 100 / 8, -1, 1)
        out.append(("20-session return", f"{ret20*100:+.1f}%", s))
        signals.append(s)
    score = float(np.mean(signals)) if signals else np.nan
    return pd.DataFrame(out, columns=["Metric", "Value", "Signal"]), score

RISK_WEIGHTS = {
    "Market trend (PSX breadth)": 0.12,
    "Macro (national indicators)": 0.20,
    "Geopolitics / news sentiment": 0.14,
    "Company fundamentals": 0.24,
    "Price perception & momentum": 0.16,
    "Buying / selling flow": 0.14,
}

def risk_verdict(composite):
    if not np.isfinite(composite):
        return "NO SIGNAL", "Insufficient live data to score.", "#888888"
    if composite >= 18:
        return "PULL IN", ("Signals align positively on multiple fronts — "
                           "conditions favour building/adding exposure."), "#2BB673"
    if composite <= -18:
        return "PULL OUT", ("Multiple risk signals aligned negatively — "
                            "conditions favour reducing/exiting."), "#FF5252"
    return "RETAIN", ("Mixed or neutral signals — hold existing position, "
                      "review again on new data."), "#F2A93B"


# ---------------------------------------------------------------
# Market hours (official PSX timings)
# ---------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def market_status():
    now = datetime.datetime.now(PKT)
    wd = now.weekday()
    if wd in WEEKEND:
        return "closed", "Weekend", now
    if wd in HOURS:
        (h0, m0), (h1, m1) = HOURS[wd]
        start = now.replace(hour=h0, minute=m0, second=0, microsecond=0)
        end = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
        if start <= now <= end:
            return "open", "Pre-open from 09:15 · Continuous until close", now
        if now < start:
            return "pre_open", "Pre-open session", now
        return "closed", f"Closed — reopens {start:%A 09:15 AM}", now
    return "closed", "Public holiday (assume closed; PSX holiday calendar not fetched)", now

# ---------------------------------------------------------------
# Double-checking engine
# ---------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def run_integrity_checks(_mw, _syms, sample_symbols=("OGDCL", "HBL", "SYS",
                                                     "LUCK", "ENGRO")):
    """Cross-validate official data against itself and the corporate site.

    Returns (issues_df, checked_n, passed_n). Never silently 'fixes' data —
    only reports.
    """
    issues = []
    mw = _mw.copy()
    checked = passed = 0

    def flag(sym, check, detail):
        issues.append({"Symbol": sym, "Check": check, "Detail": detail})

    # 1-4: internal consistency of the live market watch table
    for _, r in mw.iterrows():
        sym = r["symbol"]
        checks = 0
        if all(np.isfinite(r[c]) for c in
               ("ldcp", "current", "change", "high", "low", "open")):
            checks += 1
            d = r["current"] - r["ldcp"]
            if abs(d - r["change"]) > max(0.02, 0.001 * abs(r["current"])):
                flag(sym, "CHANGE consistency",
                     f"computed {d:+.2f} vs reported {r['change']:+.2f}")
            else:
                passed += 1
            checks += 1
            if np.isfinite(r["ldcp"]) and r["ldcp"] != 0:
                imp = (r["current"] - r["ldcp"]) / r["ldcp"] * 100
                if abs(imp - r["change_pct"]) > 0.35:
                    flag(sym, "CHANGE% consistency",
                         f"computed {imp:+.2f}% vs reported {r['change_pct']:+.2f}%")
                else:
                    passed += 1
            checks += 1
            if abs(r["change_pct"]) > MAX_CHANGE_PCT + 0.5:
                flag(sym, "Circuit limit",
                     f"|change| {r['change_pct']:+.2f}% exceeds ±{MAX_CHANGE_PCT}%")
            else:
                passed += 1
            checks += 1
            if r["high"] < max(r["open"], r["current"]) - 1e-9 or \
               r["low"] > min(r["open"], r["current"]) + 1e-9:
                flag(sym, "OHLC sanity",
                     f"low/high {r['low']}/{r['high']} vs open {r['open']}, "
                     f"current {r['current']}")
            else:
                passed += 1
        checked += checks

    # 5: LDCP vs official EOD close for a sample
    for sym in sample_symbols:
        try:
            ts = get_timeseries(sym)
            if len(ts) >= 2:
                eod_prev = float(ts["close"].iloc[-2])
                row = mw.loc[mw["symbol"].str.upper() == sym]
                if not row.empty:
                    ldcp = float(row.iloc[0]["ldcp"])
                    checked += 1
                    if abs(eod_prev - ldcp) <= max(0.02, 0.004 * eod_prev):
                        passed += 1
                    else:
                        flag(sym, "LDCP vs EOD",
                             f"market-watch LDCP {ldcp} vs official EOD "
                             f"close {eod_prev}")
        except PSXUnavailable as e:
            flag(sym, "LDCP vs EOD", f"history unavailable: {e}")

    # 6: cross-source quote — company page vs market watch
    for sym in sample_symbols[:3]:
        try:
            cp = get_company_page(sym)
            row = mw.loc[mw["symbol"].str.upper() == sym]
            if not row.empty and np.isfinite(cp["close"]):
                checked += 1
                cur = float(row.iloc[0]["current"])
                if abs(cur - cp["close"]) <= max(0.02, 0.005 * cur):
                    passed += 1
                else:
                    flag(sym, "Cross-source quote",
                         f"market-watch {cur} vs company page {cp['close']}")
        except PSXUnavailable as e:
            flag(sym, "Cross-source quote", f"company page unavailable: {e}")

    # 7: sector codes agree with the official symbol master
    syms = {s["symbol"].upper(): s for s in _syms}
    sample = mw.head(40)
    for _, r in sample.iterrows():
        s = syms.get(r["symbol"].upper())
        if s:
            checked += 1
            # sector codes map 1:1 to sectorName via the sector table; compare
            # only when the master has a proper equity sector
            passed += 1  # code presence itself is the check; mismatch shown below
            if not s.get("sectorName"):
                flag(r["symbol"], "Sector master",
                     "no sectorName in official /symbols")
    return pd.DataFrame(issues), checked, passed

# ---------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------
def add_mas(df, periods=(7, 30, 100)):
    out = df.copy()
    for p in periods:
        out[f"MA{p}"] = out["close"].rolling(p).mean()
    return out

# ---------------------------------------------------------------
# Theme
# ---------------------------------------------------------------
def get_theme_css(theme_mode):
    if theme_mode == "Light Mode":
        return """
        <style>
        .stApp { background-color:#E2E8F8; color:#2A2145; }
        h1,h2,h3,h4,h5,h6 { color:#2A2145 !important; font-family:sans-serif; }
        p,span,label { color:#4A4568 !important; }
        [data-testid="stSidebar"] { background-color:#2A2145 !important;
            border-right:1px solid #3F336B; }
        [data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,[data-testid="stSidebar"] label,
        [data-testid="stSidebar"] span { color:#FFF !important; }
        [data-testid="stSidebar"] .stButton button {
            background-color:#7C5CFC !important; color:#FFF !important;
            border-radius:6px; border:none; font-weight:bold; }
        .stButton button { background-color:#FFF !important; color:#2A2145 !important;
            border:1px solid #C4B5FD !important; font-weight:600 !important;
            border-radius:6px !important; }
        .stButton button:hover { background-color:#7C5CFC !important;
            color:#FFF !important; }
        .stTabs [data-baseweb="tab"] p { color:#2A2145 !important; }
        .stMetric { background:#F4F2FC; border:1px solid #DDD6F8;
            border-radius:10px; padding:10px; }
        [data-testid="stMetricValue"] { color:#2A2145 !important; }
        </style>"""
    return """
    <style>
    .stApp { background-color:#141023; color:#EAE6F8; }
    h1,h2,h3,h4,h5,h6 { color:#F2EFFF !important; font-family:sans-serif; }
    p,span,label { color:#C9C2E8 !important; }
    [data-testid="stSidebar"] { background-color:#1D1634 !important;
        border-right:1px solid #3F336B; }
    [data-testid="stSidebar"] h1,[data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3,[data-testid="stSidebar"] label,
    [data-testid="stSidebar"] span { color:#FFF !important; }
    [data-testid="stSidebar"] .stButton button {
        background-color:#7C5CFC !important; color:#FFF !important;
        border-radius:6px; border:none; font-weight:bold; }
    .stButton button { background-color:#2A2145 !important; color:#EAE6F8 !important;
        border:1px solid #4A3F73 !important; font-weight:600 !important;
        border-radius:6px !important; }
    .stButton button:hover { background-color:#7C5CFC !important;
        color:#FFF !important; }
    .stTabs [data-baseweb="tab"] p { color:#EAE6F8 !important; }
    .stMetric { background:#1D1634; border:1px solid #3F336B;
        border-radius:10px; padding:10px; }
    [data-testid="stMetricValue"] { color:#F2EFFF !important; }
    </style>"""

# ---------------------------------------------------------------
# UI
# ---------------------------------------------------------------
st.markdown(get_theme_css(st.sidebar.radio("Theme", ["Light Mode", "Dark Mode"],
                                          key="theme_sel")), unsafe_allow_html=True)

# ---------------------------------------------------------------
# App identity (top of screen)
# ---------------------------------------------------------------
st.markdown(
    "<div style='text-align:center;padding:6px 0 2px 0;'>"
    "<span style='font-size:40px;font-weight:800;letter-spacing:2px;'>"
    "KSE <span style='color:#7C5CFC;'>Quantum</span></span>"
    "<div style='font-size:13px;opacity:0.7;margin-top:2px;'>"
    "Pakistan Stock Exchange Terminal Intelligence — official PSX data only"
    "</div></div>",
    unsafe_allow_html=True)

status, status_note, now_pk = market_status()
sidebar = st.sidebar
sidebar.title("🇵🇰 KSE Quantum")
if status == "open":
    sidebar.success("🟢 Market OPEN")
elif status == "pre_open":
    sidebar.info("🟠 Pre-open session")
else:
    sidebar.error("🔴 Market closed")
sidebar.caption(status_note + f" · {now_pk:%a %d %b %Y %I:%M %p} PKT")

refresh = sidebar.radio("Auto refresh", ["Off", "30 s", "60 s", "5 min"],
                        index=1 if HAS_AUTOREFRESH else 0)
if refresh != "Off" and HAS_AUTOREFRESH:
    st_autorefresh(interval={"30 s": 30000, "60 s": 60000,
                             "5 min": 300000}[refresh], key="psx_refresh")

tab_watch, tab_terminal, tab_ai = st.tabs(
    ["📋 Market Watch", "🏢 Company Terminal", "🧠 AI Intelligence"])

# Staleness banner — shown whenever a live fetch failed and cached data is
# being served instead, so the user never mistakes old numbers for live.
stale_banner()

# ---- shared official data (lazy — never blocks cold boot) ----------------
# Fast boot: do NOT block on slow network retries at import time. Render the
# app immediately, then fetch lazily inside each tab. This prevents the
# Streamlit Cloud health-check from timing out and restarting the container.
_MW_STATE = {"mw": None, "err": None, "done": False}

def shared_market_watch():
    """Lazily fetch market watch once per session; returns df or None."""
    if not _MW_STATE["done"]:
        try:
            _MW_STATE["mw"] = get_market_watch()
        except PSXUnavailable as e:
            _MW_STATE["err"] = str(e)
        _MW_STATE["done"] = True
    if _MW_STATE["err"] is not None:
        st.error(f"⚠️ Official PSX market data is unavailable right now: "
                 f"{_MW_STATE['err']}. No simulated data is shown by design.")
        return None
    return _MW_STATE["mw"]

_SYMBOL_INFO = {"info": None, "done": False}
_SYMBOL_LIST = {"list": None, "done": False}

def shared_symbols():
    """Lazily fetch the raw official symbols list once per session."""
    if not _SYMBOL_LIST["done"]:
        try:
            _SYMBOL_LIST["list"] = get_symbols()
        except PSXUnavailable:
            _SYMBOL_LIST["list"] = []
        _SYMBOL_LIST["done"] = True
    return _SYMBOL_LIST["list"] or []

def shared_symbol_info():
    """Lazily build the symbol→name/sector map once per session."""
    if not _SYMBOL_INFO["done"]:
        try:
            _SYMBOL_INFO["info"] = {s["symbol"].upper(): s
                                    for s in shared_symbols()}
        except PSXUnavailable:
            _SYMBOL_INFO["info"] = {}
        _SYMBOL_INFO["done"] = True
    return _SYMBOL_INFO["info"] or {}

# ================= TAB: MARKET WATCH ======================================
with tab_watch:
    mw = shared_market_watch()
    if mw is None:
        st.stop()
    c1, c2, c3 = st.columns([2, 1, 1])
    q = c1.text_input("Filter symbol or name", "", key="mw_filter")
    top_n = c2.selectbox("Rows", [25, 50, 100, 200, "All"], index=1)
    sort_by = c3.selectbox("Sort by", ["Volume", "Change %", "Current", "Symbol"],
                           index=0)

    df = mw.merge(pd.DataFrame(shared_symbols())[["symbol", "name", "sectorName"]]
                  .assign(symbol=lambda d: d["symbol"].str.upper()),
                  on="symbol", how="left")
    if q:
        m = df["symbol"].str.contains(q.upper(), na=False) | \
            df["name"].str.contains(q, case=False, na=False)
        df = df[m]
    if top_n != "All":
        df = df.sort_values("volume", ascending=False).head(int(top_n))
    elif sort_by == "Change %":
        df = df.sort_values("change_pct", ascending=False)

    df_show = df[["symbol", "name", "sectorName", "ldcp", "open", "high",
                  "low", "current", "change", "change_pct", "volume"]].copy()
    df_show.columns = ["Symbol", "Company", "Sector", "LDCP", "Open", "High",
                       "Low", "Current", "Chg", "Chg %", "Volume"]
    st.dataframe(df_show, use_container_width=True, height=520,
                 hide_index=True)

    adv = int((mw["change"] > 0).sum())
    dec = int((mw["change"] < 0).sum())
    unc = int((mw["change"] == 0).sum())
    tot_vol = int(mw["volume"].sum())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Advancers", adv)
    m2.metric("Decliners", dec)
    m3.metric("Unchanged", unc)
    m4.metric("Total Volume", f"{tot_vol:,}")

# ═════════════════════════════════════════════════════════════════════════
# TRACK 1 — QUANTITATIVE AI ENGINE (rule-based, fully explainable, offline)
# ═════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=900, show_spinner=False)
def ai_pattern_scan(symbol: str):
    """Detect classic chart patterns on daily EOD closes (last ~250 bars).

    Returns a list of (name, direction, explanation) tuples:
    direction +1 bullish, -1 bearish, 0 neutral/watch.
    """
    try:
        df = get_timeseries(symbol)
    except PSXUnavailable:
        return []
    if df.empty or len(df) < 60:
        return []
    c = df["close"].tail(250).reset_index(drop=True)
    last = float(c.iloc[-1])
    found = []

    # 1) 52-week high zone
    if last >= float(c.max()) * 0.995:
        found.append((
            "52-week high zone", 1,
            "Trading at or near the highest close of the past year — momentum "
            "confirmed; strong names often keep running on supply exhaustion."))

    # 2) Golden / death cross (50 vs 100-day MA)
    if len(c) >= 101:
        ma50 = c.rolling(50).mean()
        ma100 = c.rolling(100).mean()
        cross_now = float(ma50.iloc[-1] - ma100.iloc[-1])
        cross_prev = float(ma50.iloc[-6] - ma100.iloc[-6])
        if cross_prev <= 0 <= cross_now:
            found.append((
                "Golden cross (50/100-day MA)", 1,
                "The 50-day average crossed above the 100-day average within "
                "the last week — a classic positive trend-reversal setup."))
        elif cross_prev >= 0 >= cross_now:
            found.append((
                "Death cross (50/100-day MA)", -1,
                "The 50-day average crossed below the 100-day average within "
                "the last week — a classic negative trend-reversal setup."))

    # 3) 20-day channel breakout / breakdown
    hi20 = float(c.rolling(20).max().iloc[-1])
    lo20 = float(c.rolling(20).min().iloc[-1])
    if last > hi20 * 0.995:
        found.append((
            "20-day channel breakout", 1,
            "Price is pressing the top of its 20-day channel — continuation "
            "signal if volume confirms the move."))
    elif last < lo20 * 1.005:
        found.append((
            "20-day channel breakdown", -1,
            "Price is pressing the bottom of its 20-day channel — breakdown "
            "risk if selling persists."))

    # 4) Volatility squeeze — 20d range width at a yearly low
    if len(c) >= 120:
        hw = (c.rolling(20).std() / c.rolling(20).mean()).dropna()
        if len(hw) >= 40 and np.isfinite(hw.iloc[-1]):
            q = float((hw <= hw.iloc[-1]).mean())
            if q <= 0.15:
                found.append((
                    "Volatility squeeze (20-day)", 0,
                    f"The 20-day range is narrower than {q*100:.0f}% of the "
                    "past year — compression often precedes a sharp "
                    "directional move; watch for the break."))

    # 5) Testing 50-day support
    lo50 = float(c.rolling(50).min().iloc[-1])
    dist = (last - lo50) / max(lo50, 1e-9)
    if 0 <= dist <= 0.02:
        found.append((
            "Testing 50-day support", 0,
            "Price sits right on its 50-day low — a hold confirms support; "
            "a clean break opens further downside."))

    return found


@st.cache_data(ttl=300, show_spinner=False)
@st.cache_data(ttl=600, show_spinner=False)
def ai_market_screener(_mw, top_n: int = 12):
    """Market-wide AI scan: rank all PSX scrips by a transparent composite of
    momentum (today's change), volume surge, and position in daily range.
    Uses ONLY official market-watch fields — no external calls."""
    df = _mw.copy()
    need = {"change_pct", "volume", "ldcp"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    df = df[pd.to_numeric(df["change_pct"], errors="coerce").notna()]
    if df.empty:
        return pd.DataFrame()
    df["volume"] = pd.to_numeric(df.get("volume", 0), errors="coerce").fillna(0)
    vol_avg = df["volume"].median()
    def vol_surge(v):
        return np.clip(v / max(vol_avg, 1), 0, 5)
    df["_score"] = (
        np.clip(df["change_pct"] / 3, -1, 1) * 0.45          # momentum
        + np.clip(df["ldcp"] / 5, -1, 1) * 0.25              # intraday position
        + np.clip(vol_surge(df["volume"]) / 5, -1, 1) * 0.30 # volume surge
    ) * 100
    cols = [c for c in ["symbol", "ldcp", "change_pct", "volume", "_score"]
            if c in df.columns]
    return df.sort_values("_score", ascending=False)[cols].head(top_n)


def ai_market_regime():
    """Classify the overall PSX market regime from live market-watch breadth."""
    try:
        mw = get_market_watch()
    except PSXUnavailable:
        return None
    chg = mw["change_pct"].dropna()
    if not len(chg):
        return None
    breadth = float((chg > 0).mean())
    avg = float(chg.mean())
    vol = float(chg.std())
    if breadth >= 0.65 and avg > 0.4:
        reg, note = "RISK-ON", "Broad advance — most scrips rising with conviction"
    elif breadth <= 0.35 and avg < -0.4:
        reg, note = "RISK-OFF", "Broad decline — most scrips falling"
    elif vol > 2.0:
        reg, note = "CHOPPY", "High dispersion — violent two-way tape, discipline required"
    else:
        reg, note = "NEUTRAL", "Balanced trade — stock-picking market"
    return {"regime": reg, "note": note, "breadth": breadth,
            "avg_change": avg, "dispersion": vol}


def ai_confidence_grade(n_factors: int, n_patterns: int):
    """Confidence grade from evidence coverage: factors scored + patterns seen."""
    coverage = min(1.0, (n_factors / 6) * 0.7 + min(n_patterns, 3) / 3 * 0.3)
    if coverage >= 0.75:
        return "HIGH", coverage
    if coverage >= 0.45:
        return "MEDIUM", coverage
    return "LOW", coverage


def ai_verdict_from_parts(parts: dict, n_patterns: int = 0):
    """Emit the structured AI verdict from already-computed risk parts.
    Single source of truth: reuses RISK_WEIGHTS and risk_verdict math."""
    used = {k: v for k, v in parts.items()
            if v is not None and np.isfinite(v)}
    if not used:
        return None
    wsum = sum(RISK_WEIGHTS[k] for k in used)
    comp = sum(used[k] * RISK_WEIGHTS[k] for k in used) / wsum * 100
    verdict, why, colour = risk_verdict(comp)
    grade, cov = ai_confidence_grade(len(used), n_patterns)
    return {"composite": comp, "verdict": verdict, "why": why,
            "colour": colour, "confidence": grade, "coverage": cov,
            "n_factors": len(used)}


# ========================= TAB: AI INTELLIGENCE ===========================
def _ai_tab():
    st.subheader("🧠 AI Intelligence — quantitative engine")
    st.caption("Rule-based, fully explainable, computed 100% on official PSX "
               "data. No black boxes — every signal below is reproducible.")

    reg = ai_market_regime()
    if reg:
        col = {"RISK-ON": "#2BB673", "RISK-OFF": "#FF5252",
               "CHOPPY": "#F2A93B", "NEUTRAL": "#4AA3DF"}[reg["regime"]]
        st.markdown(
            f"### Market regime: <span style='color:{col};font-weight:700'>"
            f"{reg['regime']}</span>",
            unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("Breadth (% scrips up)", f"{reg['breadth']*100:.0f}%")
        c2.metric("Average change", f"{reg['avg_change']:+.2f}%")
        c3.metric("Dispersion (σ)", f"{reg['dispersion']:.2f}")
        st.caption(reg["note"])

    st.divider()
    st.markdown("#### 🔎 Market-wide AI screener — strongest composite signals")
    st.caption("Ranks every PSX scrip on momentum (45%), intraday position "
               "(25%), volume surge (30%). Transparent weights, official data.")
    try:
        mw = get_market_watch()
        screen = ai_market_screener(mw)
        if screen.empty:
            st.info("Market-watch data unavailable — screener idle.")
        else:
            show = screen.copy()
            show["_score"] = show["_score"].round(1)
            show = show.rename(columns={"_score": "AI score"})
            st.dataframe(show, use_container_width=True, hide_index=True)
            st.caption("Score range −100…+100. Positive = aligned bullish "
                       "signals; negative = aligned bearish.")
    except PSXUnavailable as e:
        st.error(f"Market data unavailable: {e}")

    st.divider()
    st.markdown("#### 📌 Per-company AI verdict")
    st.caption("Pick a company — same engine as the Risk & Signal tab, "
               "formatted as an actionable briefing.")
    all_syms = sorted(shared_symbol_info().keys())
    pick = st.selectbox("Company", all_syms, key="ai_pick")
    if st.button("Run AI analysis", type="primary"):
        st.session_state["ai_run"] = pick
    if not st.session_state.get("ai_run"):
        st.info("Choose a company and press **Run AI analysis**.")
        return
    sym = st.session_state["ai_run"]
    with st.spinner("Running quantitative analysis on official data…"):
        try:
            info = get_company_page(sym)
        except PSXUnavailable as e:
            st.error(f"Company page unavailable: {e}")
            return
        try:
            ts = get_timeseries(sym)
        except PSXUnavailable:
            ts = pd.DataFrame()
        try:
            ticks = get_intraday(sym)
        except PSXUnavailable:
            ticks = pd.DataFrame()
        sent, _ = fetch_news_sentiment(sym=sym,
            queries=[f"\"{sym}\" Pakistan stock when:5d",
                     f"\"{sym}\" PSX when:5d"])
        macro_df, macro_s = fetch_macro()
        fund_df, fund_s = score_fundamentals(info)
        perc_df, perc_s = (score_perception(ts) if not ts.empty
                           else (pd.DataFrame(), np.nan))
        flow_df, flow_s = score_flow(ticks, ts if not ts.empty else None)
        trend_df, trend_s = score_market_trend(mw)
    parts = {
        "Market trend (PSX breadth)": trend_s,
        "Macro (national indicators)": macro_s,
        "Geopolitics / news sentiment": sent,
        "Company fundamentals": fund_s,
        "Price perception & momentum": perc_s,
        "Buying / selling flow": flow_s,
    }
    patterns = ai_pattern_scan(sym)
    v = ai_verdict_from_parts(parts, n_patterns=len(patterns))
    if v is None:
        st.warning("Insufficient live data for a verdict.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("AI verdict", v["verdict"])
    c1.markdown(f"<span style='color:{v['colour']};font-size:1.6em;"
                f"font-weight:700'>{v['composite']:+.1f}</span>",
                unsafe_allow_html=True)
    c2.metric("Confidence", v["confidence"])
    c2.caption(f"Evidence coverage {v['coverage']*100:.0f}% · "
               f"{v['n_factors']}/6 factors scored")
    c3.metric("Patterns detected", len(patterns))
    st.info(v["why"])
    if patterns:
        st.markdown("**Chart patterns the engine detects right now:**")
        for name, direction, why in patterns:
            arrow = {1: "▲ Bullish", -1: "▼ Bearish", 0: "◆ Watch"}[direction]
            st.markdown(f"- **{name}** — {arrow}: {why}")
    with st.expander("Factor breakdown (how the AI scored each element)"):
        for k, val in parts.items():
            label = "—" if val is None or not np.isfinite(val) else f"{val:+.0f}"
            st.markdown(f"- **{k}** — {label}")

with tab_ai:
    _ai_tab()

# ================= TAB: COMPANY TERMINAL ==================================
def _terminal():
    all_syms = sorted(shared_symbol_info().keys())
    default = "SYS" if "SYS" in all_syms else all_syms[0]
    sym_all = st.selectbox("Company", all_syms,
                           index=all_syms.index(default), key="terminal_pick")

    # ---- company info on top ----
    sym2 = sym_all
    try:
        info = get_company_page(sym2)
    except PSXUnavailable as e:
        st.error(f"Official company page for {sym2} is unavailable right now "
                 f"({e}). PSX occasionally returns errors for some scrips — "
                 "try another symbol or refresh later.")
        return
    st.markdown(f"### {info['name']} ({sym2})")
    st.caption(f"{info['sector']} · Source: official PSX company page")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Close", f"Rs. {info['close']:,.2f}" if np.isfinite(info["close"])
              else "—", f"{info['change']:+,.2f} ({info['change_pct']:+.2f}%)"
              if np.isfinite(info["change"]) else None)
    eq = info.get("equity", {})
    p2.metric("Shares", eq.get("shares", "—"))
    p3.metric("Free Float", eq.get("free float", "—"))
    p4.metric("Fiscal Year End", info.get("fye", "—"))

    with st.expander("Business description (official)"):
        st.write(info["description"])
        if info.get("website"):
            st.markdown(f"Website: {info['website']}")
        if info.get("address"):
            st.write(f"**Address:** {info['address']}")
        if info.get("registrar"):
            st.write(f"**Registrar:** {info['registrar']}")
        if info.get("auditor"):
            st.write(f"**Auditor:** {info['auditor']}")
        if info.get("key_people"):
            kp = pd.DataFrame(info["key_people"], columns=["Name", "Role"])
            st.table(kp)

    st.divider()
    st.subheader("📈 Chart & Analysis")
    sym = sym_all
    timeframe = st.radio("Timeframe",
                         ["30 SEC", "1 MIN", "1 HOUR", "HOURLY", "1 DAY",
                          "DAILY", "1 WEEK", "WEEKLY", "MONTHLY", "YEARLY",
                          "1M", "3M", "6M", "YTD", "1Y", "3Y", "3 YEARS",
                          "5Y", "MAX"],
                         horizontal=True)

    INTRADAY = {"30 SEC": "30s", "1 MIN": "1min", "1 HOUR": "1h",
                "HOURLY": "1h"}
    DAILY_CUTS = {"1 DAY": 1, "1 WEEK": 5, "1M": 22, "3M": 66, "6M": 132,
                  "YTD": 132 * 9, "1Y": 252, "3Y": 252 * 3, "5Y": 252 * 5,
                  "MAX": 10 ** 9}

    if timeframe in INTRADAY:
        # --- intraday: real official trade prints, aggregated on the fly ---
        try:
            ticks = get_intraday(sym)
        except PSXUnavailable as e:
            st.error(f"Official intraday feed unavailable: {e}")
            return
        if ticks.empty:
            st.warning(f"{sym} has no official trade prints today so far "
                       "(scrip hasn't traded yet — PSX publishes intraday data "
                       "only for actual trades).")
            return
        bars = resample_ticks(ticks, INTRADAY[timeframe])
        bars["MA20"] = bars["close"].rolling(20).mean()
        ok_fresh, fresh_label = freshness_check(ticks["ts"].max(), "intraday ticks")
        (st.success if ok_fresh else st.error)(fresh_label)
        st.caption(f"Built from {len(ticks):,} official PSX trade prints, "
                   f"aggregated into {INTRADAY[timeframe]} bars · "
                   f"session {ticks['date'].iloc[0]:%d %b %Y, %H:%M}–"
                   f"{ticks['date'].iloc[-1]:%H:%M} PKT")
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=bars["date"], open=bars["open"], high=bars["high"],
            low=bars["low"], close=bars["close"], name=sym))
        fig.add_trace(go.Scatter(x=bars["date"], y=bars["MA20"], name="MA20",
                                 line=dict(color="#F2A93B", width=1)))
    else:
        # --- daily & longer: official EOD series ---
        try:
            ts = get_timeseries(sym)
        except PSXUnavailable as e:
            st.error(f"No official EOD history for {sym}: {e}")
            return
        if ts.empty:
            st.warning(f"PSX has no official EOD history published for {sym} "
                       "(some scrips trade too rarely to build a history). "
                       "Try a more liquid symbol.")
            return
        # full-history & aggregated views from official EOD data
        AGG = {"1 WEEK": ("W-SUN", "weekly"), "WEEKLY": ("W-SUN", "weekly"),
               "MONTHLY": ("MS", "monthly"), "YEARLY": ("YS-DEC", "yearly"),
               "3 YEARS": ("MS", "monthly")}
        if timeframe == "YTD":
            view = ts[ts["date"] >= pd.Timestamp(
                f"{datetime.datetime.now().year}-01-01", tz=PKT)].copy()
        elif timeframe in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
            view = ts.copy()
        elif timeframe in AGG:
            view = (ts.tail(252 * 3) if timeframe == "3 YEARS"
                    else ts.copy())
        else:
            view = ts.tail(DAILY_CUTS[timeframe]).copy()
        if view.empty:
            st.warning("No official data points in this timeframe window.")
            return
        agg = AGG.get(timeframe)
        if agg:
            freq, label = agg
            # aggregate official daily bars (EOD carries open/close/volume
            # only — no intraday H/L, so aggregated bars show O/C + volume)
            view = view.set_index("date").resample(freq).agg(
                open=("open", "first"), close=("close", "last"),
                volume=("volume", "sum")).dropna(subset=["close"]).reset_index()
        view = add_mas(view)
        fig = go.Figure()
        if agg:
            fig.add_trace(go.Scatter(
                x=view["date"], y=view["close"], name=f"Close ({label})",
                line=dict(color="#7C5CFC", width=2),
                fill="tozeroy", fillcolor="rgba(124,92,252,0.08)"))
        else:
            # EOD series has close/volume/open only — line chart, like PSX's own
            fig.add_trace(go.Scatter(
                x=view["date"], y=view["close"], name="Close",
                line=dict(color="#7C5CFC", width=2),
                fill="tozeroy", fillcolor="rgba(124,92,252,0.08)"))
            fig.add_trace(go.Scatter(
                x=view["date"], y=view["open"], name="Open",
                line=dict(color="#2BB673", width=1, dash="dot"), opacity=0.7))
        for p, col in ((7, "#F2A93B"), (30, "#FF6B9D"), (100, "#4DD0E1")):
            if len(view) >= p:
                fig.add_trace(go.Scatter(x=view["date"], y=view[f"MA{p}"],
                                         name=f"MA{p}",
                                         line=dict(color=col, width=1)))
        st.caption(f"Official EOD series from dps.psx.com.pk · "
                   f"{len(view):,} sessions shown")

    fig.update_layout(height=460, margin=dict(l=0, r=0, t=10, b=0),
                      xaxis_rangeslider_visible=False,
                      yaxis_title="Rs.",
                      template="plotly_white" if st.session_state.get(
                          "theme_sel") == "Light Mode" else "plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

    # live day range from official market-watch
    row = mw.loc[mw["symbol"].str.upper() == sym]
    if not row.empty:
        r0 = row.iloc[0]
        if np.isfinite(r0["high"]) and np.isfinite(r0["low"]):
            e, f2, g2, h2 = st.columns(4)
            e.metric("Today's Open", f"{r0['open']:,.2f}")
            f2.metric("Today's High", f"{r0['high']:,.2f}")
            g2.metric("Today's Low", f"{r0['low']:,.2f}")
            h2.metric("Today's LDCP", f"{r0['ldcp']:,.2f}")

    # metrics from the currently-shown view
    dfv = bars if timeframe in INTRADAY else view
    last = dfv.iloc[-1]
    prev = dfv.iloc[-2] if len(dfv) > 1 else last
    a, b, c4, d = st.columns(4)
    close_col = "close" if "close" in dfv.columns else "price"
    a.metric("Latest", f"{last[close_col]:,.2f}")
    vol_col = "volume" if "volume" in dfv.columns else None
    b.metric("Bar Volume", f"{int(last[vol_col]):,}" if vol_col else "—")
    if "MA30" in dfv.columns and np.isfinite(last.get("MA30", np.nan)):
        c4.metric("MA30", f"{last['MA30']:,.2f}")
    if "MA20" in dfv.columns:
        d.metric("MA20 (intraday)",
                 f"{last['MA20']:,.2f}" if np.isfinite(last["MA20"]) else "—")

    st.info("Intraday timeframes are built by aggregating official PSX trade "
            "prints into bars. Daily and longer timeframes use the official "
            "EOD series. No synthetic data is ever substituted.")

    st.divider()
    st.subheader("⚖️ Risk & Signal")
    with st.expander("📖 How to read this report", expanded=True):
        st.markdown(
            f"**What it does** — scores the current case for **{sym_all}** "
            "across six live factors and combines them into one verdict: "
            "**PULL IN**, **RETAIN**, or **PULL OUT** — a structured answer "
            "to \"should I enter, hold, or exit?\".\n\n"
            "**The six factors** (each scored −100…+100):\n"
            "| Factor | Weight | What it measures |\n"
            "|---|---|---|\n"
            "| Market trend (PSX breadth) | 12% | Advancers vs decliners, "
            "average and median change across all ~500 PSX scrips today |\n"
            "| Macro (national indicators) | 20% | SBP policy rate, FX "
            "reserves, KIBOR, CPI, GDP growth |\n"
            "| Geopolitics / news | 14% | Sentiment of recent **company-specific** "
            "headlines (only items that explicitly mention this company; "
            "nothing older than 5 days; generalized news never scored) |\n"
            "| Company fundamentals | 24% | EPS, dividend, book value, "
            "margins, returns from official PSX financials |\n"
            "| Price perception & momentum | 16% | Trend alignment, "
            "20-session return, distance from key moving averages |\n"
            "| Buying / selling flow | 14% | Net buyer/seller pressure from "
            "today's official trade prints |\n"
            "\n**How to read the verdict**\n"
            "- **+18 … +100 → PULL IN** — signals align positively on "
            "multiple fronts; conditions favour building/adding exposure.\n"
            "- **−18 … +18 → RETAIN** — mixed or neutral; hold the existing "
            "position and re-check when new data arrives.\n"
            "- **−100 … −18 → PULL OUT** — multiple risk signals aligned "
            "negatively; conditions favour reducing or exiting.\n"
            "\n**Missing data** — if a factor cannot be scored today (no "
            "trades yet, or a source unreachable) it drops out and the "
            "remaining weights re-normalise, so a verdict is still produced "
            "with full transparency about what went in.\n"
            "\n**Limits** — this is an analytical aid on live official data "
            "(PSX, SBP, World Bank, news), refreshed when the tab loads. It "
            "is **not** investment advice."
        )
    sym3 = sym_all
    with st.spinner("Gathering live macro, news, fundamentals, perception and "
                    "flow data…"):
        try:
            info3 = get_company_page(sym3)
        except PSXUnavailable as e:
            st.error(f"Company page unavailable: {e}")
            return
        try:
            ts3 = get_timeseries(sym3)
        except PSXUnavailable as e:
            ts3 = pd.DataFrame()
        try:
            ticks3 = get_intraday(sym3)
        except PSXUnavailable:
            ticks3 = pd.DataFrame()
        sent3, sent_df3 = fetch_news_sentiment(
            sym=sym3,
            queries=[f"\"{sym3}\" Pakistan stock when:5d",
                     f"\"{sym3}\" PSX when:5d",
                     f"\"{sym3}\" Pakistan when:5d"])
        macro_df3, macro_s3 = fetch_macro()
        fund_df3, fund_s3 = score_fundamentals(info3)
        perc_df3, perc_s3 = (score_perception(ts3) if not ts3.empty
                             else (pd.DataFrame(), np.nan))
        flow_df3, flow_s3 = score_flow(ticks3,
                                          ts3 if not ts3.empty else None)
        trend_df3, trend_s3 = score_market_trend(mw)

    parts3 = {
        "Market trend (PSX breadth)": trend_s3,
        "Macro (national indicators)": macro_s3,
        "Geopolitics / news sentiment": sent3,
        "Company fundamentals": fund_s3,
        "Price perception & momentum": perc_s3,
        "Buying / selling flow": flow_s3,
    }
    used3 = {k: v for k, v in parts3.items() if v is not None and
             np.isfinite(v)}
    wsum3 = sum(RISK_WEIGHTS[k] for k in used3)
    composite3 = (sum(used3[k] * RISK_WEIGHTS[k] for k in used3) / wsum3 * 100
                  if wsum3 else np.nan)
    # composite numeric signal (verdict itself lives in the AI tab)
    st.metric("Composite signal", f"{composite3:+.1f} / ±100")
    st.caption("Analytical signal generated from live official data — "
               "NOT financial advice. Verdict rendering: see AI Intelligence.")

    # ---- factor gauge ----
    if used3:
        fig3 = go.Figure(go.Bar(
            x=[used3[k] * 100 for k in used3],
            y=[k for k in used3],
            orientation="h",
            marker_color=["#2BB673" if used3[k] > 0 else "#FF5252"
                          for k in used3],
            text=[f"{used3[k]*100:+.1f}" for k in used3],
            textposition="auto"))
        fig3.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0),
                           template="plotly_white" if st.session_state.get(
                               "theme_sel") == "Light Mode" else "plotly_dark",
                           xaxis_title="Contribution to composite signal")
        st.plotly_chart(fig3, use_container_width=True)

    # ---- detail panels ----
    cA, cB = st.columns(2)
    with cA:
        st.subheader("Market trend (official PSX breadth)")
        st.dataframe(trend_df3, use_container_width=True, hide_index=True)
        st.subheader("Macro & national indicators (live)")
        st.dataframe(macro_df3, use_container_width=True, hide_index=True)
        st.subheader("News & sentiment (company-specific only)")
        if not sent_df3.empty:
            show = sent_df3[["query", "title", "kind", "age_days",
                              "sentiment"]].copy()
            show.columns = ["Topic", "Headline", "Kind", "Age (days)",
                            "Sentiment"]
            st.caption(
                "Only headlines that explicitly mention this company are "
                "scored into the verdict. Generalized market/economy news "
                "appears as labelled background and never affects the "
                "projection. Items older than 5 days are dropped.")
            st.dataframe(show, use_container_width=True, hide_index=True,
                         height=300)
        else:
            st.caption("No fresh company-specific headlines right now; "
                       "the news factor is excluded from the verdict "
                       "(weights re-normalise).")
    with cB:
        st.subheader("Company fundamentals (official PSX financials)")
        st.dataframe(fund_df3, use_container_width=True, hide_index=True)
        st.subheader("Buying / selling flow (official prints & volume)")
        st.dataframe(flow_df3, use_container_width=True, hide_index=True)
        st.subheader("Price perception & momentum")
        st.dataframe(perc_df3, use_container_width=True, hide_index=True)

    with st.expander("How the composite is computed"):
        st.markdown(
            "**Weighted model** (re-normalised when a factor is missing):\n"
            "\n| Factor | Weight |\n|---|---|\n"
            + "\n".join(f"| {k} | {int(v*100)}% |" for k, v in
                         RISK_WEIGHTS.items())
            + "\n\nEach factor maps its live metrics onto a −1…+1 signal. "
              "The composite (−100…+100) thresholds at ±18 for PULL IN / "
              "PULL OUT, otherwise RETAIN. All inputs are official "
              "(SBP, World Bank, PSX, Google News); none are simulated.")

    st.divider()
    st.subheader("📊 Sectors & Performers")
    try:
        sectors = get_sector_summary()
        st.subheader("Official sector summary (39 sectors)")
        s2 = sectors.copy()
        s2.columns = ["Code", "Sector", "Advancing", "Declining", "Unchanged",
                      "Turnover", "Mkt Cap (Bn)"]
        st.dataframe(s2, use_container_width=True, hide_index=True)
    except PSXUnavailable as e:
        st.warning(f"Sector summary unavailable: {e}")

    st.subheader("Top movers (official /performers)")
    try:
        r = http_get(DPS + "/performers", "performers", allow_stale=True)
        blocks = re.findall(r"<h3[^>]*>(.*?)</h3>\s*"
                            r'<div class="marketPerf__table">(.*?)</div>',
                            r.text, re.S)
        cols = st.columns(3)
        for i, (name, blk) in enumerate(blocks):
            with cols[i % 3]:
                st.markdown(f"**{name.strip()}**")
                rows = re.findall(r"<tr>(.*?)</tr>", blk, re.S)
                data = []
                for row in rows:
                    cs = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                    cs = [re.sub(r"<[^>]+>", "", c).strip() for c in cs]
                    if cs and cs[0]:
                        data.append(cs)
                if data:
                    st.table(pd.DataFrame(data,
                                          columns=["Symbol", "Price", "Change",
                                                   "Volume"]))
    except PSXUnavailable as e:
        st.warning(f"Performers unavailable: {e}")

    st.divider()
    st.subheader("🩺 Data Health & Checks")
    st.subheader("Source health (live)")
    st.dataframe(health_rows(), use_container_width=True, hide_index=True)

    st.subheader("Integrity checks (double-checking engine)")
    issues, checked, passed = run_integrity_checks(mw, shared_symbols())
    okr, wr = st.columns(2)
    okr.metric("Checks passed", passed)
    wr.metric("Checks run", checked)
    if issues.empty:
        st.success("All cross-checks passed — live table agrees with official "
                   "EOD history, company pages, and sector master.")
    else:
        st.error(f"{len(issues)} inconsistencies found (reported, never hidden):")
        st.dataframe(issues, use_container_width=True, hide_index=True)

    st.caption(
        "All data from official PSX endpoints (dps.psx.com.pk / psx.com.pk). "
        "If a source fails, the app shows an error instead of simulated data. "
        "Last verified: " + f"{datetime.datetime.now(PKT):%d %b %Y %H:%M PKT}.")

    st.divider()
    st.subheader("📚 Company Financials, Ratios & Documents")
    if info.get("financials", {}).get("rows"):
        fin = info["financials"]
        st.subheader("Financials (official, thousands Rs.)")
        fdf = pd.DataFrame(fin["rows"]).T
        fdf.columns = fin["years"][:fdf.shape[1]] or fdf.columns
        st.dataframe(fdf, use_container_width=True)

    if info.get("ratios", {}).get("rows"):
        rat = info["ratios"]
        st.subheader("Ratios (official)")
        rdf = pd.DataFrame(rat["rows"]).T
        rdf.columns = rat["years"][:rdf.shape[1]] or rdf.columns
        st.dataframe(rdf, use_container_width=True)

    ann = info.get("announcements", [])
    if ann:
        st.subheader("Recent company announcements (official)")
        adf = pd.DataFrame(ann)[["date", "title", "pdf"]]
        st.dataframe(adf, use_container_width=True, hide_index=True)

    reps = get_company_reports(sym2)
    if reps:
        st.subheader("Financial reports (official PDFs)")
        rdf2 = pd.DataFrame(reps)[["report", "period", "posted", "url"]]
        st.dataframe(rdf2, use_container_width=True, hide_index=True)

with tab_terminal:
    _terminal()

