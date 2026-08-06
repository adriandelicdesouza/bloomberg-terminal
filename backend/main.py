"""
Bloomberg-style Financial Terminal — Backend
FastAPI + SQLite + yfinance + FRED + RSS
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import WebSocket, WebSocketDisconnect
import urllib.request
import urllib.parse
import uvicorn
import asyncio
import json
import time
import logging
import sqlite3
import os
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
DB_PATH  = DATA_DIR / "terminal.db"
ALERT_LOG = LOG_DIR / "alerts.log"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("terminal")

app = FastAPI(title="Bloomberg Terminal", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# ── Exception Handlers ────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    log.error(f"Global exception: {exc}\n{traceback.format_exc()}")
    return {
        "error": str(exc),
        "detail": "Internal server error. Check logs for details.",
        "status": 500
    }

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            shares REAL NOT NULL,
            avg_cost REAL NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            condition TEXT NOT NULL,
            threshold REAL NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS price_cache (
            ticker TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS macro_cache (
            key TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Cache helpers ─────────────────────────────────────────────────────────────
CACHE_TTL = {
    "price": 60,       # 1 min
    "macro": 3600,     # 1 hour
    "news":  600,      # 5 min
    "history": 300,
}

def cache_get(table: str, key: str, ttl: int):
    conn = get_db()
    row = conn.execute(f"SELECT data, updated_at FROM {table} WHERE {'ticker' if table == 'price_cache' else 'key'} = ?", (key,)).fetchone()
    conn.close()
    if row and (time.time() - row["updated_at"]) < ttl:
        return json.loads(row["data"])
    return None

def cache_set(table: str, key: str, data):
    conn = get_db()
    key_col = "ticker" if table == "price_cache" else "key"
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({key_col}, data, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(data), time.time())
    )
    conn.commit()
    conn.close()

# ── Market Data (yfinance) ────────────────────────────────────────────────────
def fetch_quote(ticker: str) -> dict:
    cached = cache_get("price_cache", ticker, CACHE_TTL["price"])
    if cached:
        return cached

    try:
        import yfinance as yf
        t = yf.Ticker(ticker, session=None)  # Use default session with timeouts
        info = t.fast_info
        hist = t.history(period="2d", interval="1d", timeout=10)

        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
        price      = float(info.last_price) if hasattr(info, "last_price") else None

        if price is None and len(hist) > 0:
            price = float(hist["Close"].iloc[-1])

        change_pct = ((price - prev_close) / prev_close * 100) if price and prev_close else 0.0

        result = {
            "ticker": ticker,
            "price": round(price, 2) if price else None,
            "change_pct": round(change_pct, 2),
            "prev_close": round(prev_close, 2) if prev_close else None,
            "market_cap": getattr(info, "market_cap", None),
            "volume": getattr(info, "three_month_average_volume", None),
        }
        cache_set("price_cache", ticker, result)
        return result
    except Exception as e:
        log.error(f"Quote fetch failed for {ticker}: {e}")
        raise HTTPException(status_code=503, detail=f"Could not fetch {ticker}: {e}")

def fetch_history(ticker: str, period: str = "1mo") -> list:
    cache_key = f"{ticker}_{period}"
    cached = cache_get("price_cache", cache_key, CACHE_TTL["history"])
    if cached:
        return cached

    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period, interval="1d", timeout=10)
        result = [
            {"date": str(idx.date()), "close": round(float(row["Close"]), 2)}
            for idx, row in hist.iterrows()
        ]
        cache_set("price_cache", cache_key, result)
        return result
    except Exception as e:
        log.error(f"History fetch failed for {ticker}: {e}")
        raise HTTPException(status_code=503, detail=str(e))

def fetch_stock_detail(ticker: str) -> dict:
    cache_key = f"detail_{ticker}"
    cached = cache_get("price_cache", cache_key, CACHE_TTL["history"])
    if cached:
        return cached

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info  # info dict is cached, no timeout needed for it
        result = {
            "ticker": ticker,
            "name":       info.get("longName", ticker),
            "price":      info.get("currentPrice") or info.get("regularMarketPrice"),
            "change_pct": round(((info.get("currentPrice", 0) - info.get("previousClose", 1)) / info.get("previousClose", 1)) * 100, 2) if info.get("previousClose") else 0,
            "pe_ratio":   info.get("trailingPE"),
            "market_cap": info.get("marketCap"),
            "volume":     info.get("volume"),
            "avg_volume": info.get("averageVolume"),
            "52w_high":   info.get("fiftyTwoWeekHigh"),
            "52w_low":    info.get("fiftyTwoWeekLow"),
            "sector":     info.get("sector"),
            "industry":   info.get("industry"),
            "description":info.get("longBusinessSummary", "")[:400],
        }
        cache_set("price_cache", cache_key, result)
        return result
    except Exception as e:
        log.error(f"Detail fetch failed for {ticker}: {e}")
        raise HTTPException(status_code=503, detail=str(e))

# ── Macro Data (FRED) ─────────────────────────────────────────────────────────
FRED_SERIES = {
    "fed_rate":       "FEDFUNDS",
    "cpi_us":         "CPIAUCSL",
    "unemployment_us":"UNRATE",
    "gdp_us":         "A191RL1Q225SBEA",
}

def fetch_fred_series(series_id: str, limit: int = 24) -> list:
    import urllib.request
    import urllib.error
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                lines = r.read().decode().strip().split("\n")[1:]
            data = []
            for line in lines[-limit:]:
                parts = line.split(",")
                if len(parts) == 2 and parts[1].strip() not in ("", "."):
                    data.append({"date": parts[0].strip(), "value": float(parts[1].strip())})
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            if attempt < 2:
                log.warning(f"FRED fetch attempt {attempt + 1} failed for {series_id}: {e}, retrying...")
                time.sleep(2 ** attempt)
            else:
                log.error(f"FRED fetch failed for {series_id} after 3 attempts: {e}")
                return []

def fetch_boc_rate() -> dict:
    """Bank of Canada overnight rate from public API."""
    cached = cache_get("macro_cache", "boc_rate", CACHE_TTL["macro"])
    if cached:
        return cached
    try:
        import urllib.request
        url = "https://www.bankofcanada.ca/valet/observations/IROICDD/json?recent=1"
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.loads(r.read())
        obs = d["observations"][-1]
        result = {"date": obs["d"], "value": float(obs["IROICDD"]["v"])}
        cache_set("macro_cache", "boc_rate", result)
        return result
    except Exception as e:
        log.error(f"BoC rate fetch failed: {e}")
        return {"date": "N/A", "value": None}

def get_macro_data() -> dict:
    cached = cache_get("macro_cache", "all_macro", CACHE_TTL["macro"])
    if cached:
        return cached

    result = {}
    for key, series_id in FRED_SERIES.items():
        data = fetch_fred_series(series_id, limit=24)
        result[key] = {
            "series": data,
            "latest": data[-1] if data else None,
        }
    result["boc_rate"] = {"series": [], "latest": fetch_boc_rate()}

    cache_set("macro_cache", "all_macro", result)
    return result

# ── News Feed (RSS) ───────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("Yahoo Finance",      "https://finance.yahoo.com/news/rssindex"),
    ("Google — Markets",   "https://news.google.com/rss/search?q=stock+market&hl=en-US&gl=US&ceid=US:en"),
    ("Google — Economy",   "https://news.google.com/rss/search?q=economy+fed+rates&hl=en-US&gl=US&ceid=US:en"),
    ("Google — Crypto",    "https://news.google.com/rss/search?q=bitcoin+crypto&hl=en-US&gl=US&ceid=US:en"),
    ("Google — Canada",    "https://news.google.com/rss/search?q=tsx+bank+of+canada+economy&hl=en-CA&gl=CA&ceid=CA:en"),
]

def fetch_news(limit: int = 150) -> list:
    cached = cache_get("macro_cache", "news_feed", CACHE_TTL["news"])
    if cached:
        return cached

    import urllib.request
    import urllib.error
    import xml.etree.ElementTree as ET

    items = []
    for source, url in NEWS_FEEDS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    root = ET.fromstring(r.read())
                ns = ""
                for item in root.iter("item"):
                    title   = item.findtext("title", "").strip()
                    link    = item.findtext("link", "").strip()
                    pubdate = item.findtext("pubDate", "").strip()
                    # Google News puts actual source in <source> tag, fall back to feed source name
                    src_el  = item.find("source")
                    src     = src_el.text.strip() if src_el is not None and src_el.text else source
                    if title and link:
                        items.append({
                            "source": src,
                            "title": title,
                            "link": link,
                            "date": pubdate,
                        })
                break  # Success, move to next feed
            except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
                if attempt < 1:
                    log.warning(f"RSS fetch attempt {attempt + 1} failed [{source}]: {e}, retrying...")
                    time.sleep(1)
                else:
                    log.warning(f"RSS fetch failed [{source}]: {e}")

    items = items[:limit]
    cache_set("macro_cache", "news_feed", items)
    return items

# ── Alert Engine ──────────────────────────────────────────────────────────────
def check_alerts():
    conn = get_db()
    alerts = conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()
    conn.close()
    for alert in alerts:
        try:
            q = fetch_quote(alert["ticker"])
            price = q.get("price")
            if price is None:
                continue
            triggered = False
            if alert["condition"] == "above" and price > alert["threshold"]:
                triggered = True
            elif alert["condition"] == "below" and price < alert["threshold"]:
                triggered = True
            elif alert["condition"] == "change_pct_above" and abs(q.get("change_pct", 0)) > alert["threshold"]:
                triggered = True
            if triggered:
                msg = f"ALERT [{alert['ticker']}] {alert['condition']} {alert['threshold']} | price={price}"
                log.warning(msg)
                with open(ALERT_LOG, "a") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except:
            pass

# ── API Routes ────────────────────────────────────────────────────────────────

WATCHLIST = ["^GSPC", "^IXIC", "XIU.TO", "BTC-USD"]

@app.get("/api/health")
def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "ok",
        "timestamp": time.time(),
        "database": "connected" if DB_PATH.exists() else "missing"
    }

@app.get("/api/diagnostics")
def diagnostics():
    """Diagnostic endpoint to test all systems."""
    import socket
    diagnostics_report = {
        "backend": "ok",
        "database": "ok" if DB_PATH.exists() else "missing",
        "filesystem": "ok" if DATA_DIR.exists() and LOG_DIR.exists() else "missing",
        "hostname": socket.gethostname(),
        "timestamp": time.time(),
    }
    
    # Test yfinance
    try:
        import yfinance as yf
        yf.Ticker("AAPL").info  # Quick call
        diagnostics_report["yfinance"] = "ok"
    except Exception as e:
        diagnostics_report["yfinance"] = f"error: {str(e)[:50]}"
    
    # Test FRED
    try:
        data = fetch_fred_series("FEDFUNDS", limit=1)
        diagnostics_report["fred"] = "ok" if data else "no_data"
    except Exception as e:
        diagnostics_report["fred"] = f"error: {str(e)[:50]}"
    
    # Test news
    try:
        fetch_news(limit=1)
        diagnostics_report["news_feed"] = "ok"
    except Exception as e:
        diagnostics_report["news_feed"] = f"error: {str(e)[:50]}"
    
    return diagnostics_report

@app.get("/api/market/overview")
def market_overview():
    results = []
    labels = {"^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^XIU.TO": "TSX", "BTC-USD": "BTC/USD"}
    for ticker in WATCHLIST:
        try:
            q = fetch_quote(ticker)
            q["label"] = labels.get(ticker, ticker)
            results.append(q)
        except:
            results.append({"ticker": ticker, "label": labels.get(ticker, ticker), "price": None, "change_pct": 0})
    return {"data": results}

@app.get("/api/market/history/{ticker}")
def market_history(ticker: str, period: str = "1mo"):
    if period not in ("1d", "5d", "1mo", "3mo", "1y"):
        raise HTTPException(400, "Invalid period")
    interval = "5m" if period == "1d" else "1d"
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period=period, interval=interval, timeout=10)
        data = [
            {"date": str(idx), "close": round(float(row["Close"]), 2)}
            for idx, row in hist.iterrows()
        ]
        return {"ticker": ticker, "period": period, "data": data}
    except Exception as e:
        raise HTTPException(503, str(e))

@app.get("/api/stock/{ticker}")
def stock_detail(ticker: str):
    return fetch_stock_detail(ticker.upper())

@app.get("/api/stock/{ticker}/history")
def stock_history(ticker: str, period: str = "3mo"):
    return {"ticker": ticker, "data": fetch_history(ticker.upper(), period)}

@app.get("/api/macro")
def macro_data():
    return get_macro_data()

@app.get("/api/news")
def news_feed(limit: int = 150):
    return {"items": fetch_news(limit)}

# Portfolio endpoints
@app.get("/api/portfolio")
def get_portfolio():
    conn = get_db()
    holdings = [dict(r) for r in conn.execute("SELECT * FROM portfolio").fetchall()]
    conn.close()

    enriched = []
    total_value = 0
    total_cost  = 0
    for h in holdings:
        try:
            q = fetch_quote(h["ticker"])
            price = q.get("price") or h["avg_cost"]
            value = price * h["shares"]
            cost  = h["avg_cost"] * h["shares"]
            pnl   = value - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            total_value += value
            total_cost  += cost
            enriched.append({
                **h,
                "current_price": price,
                "value": round(value, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "change_pct": q.get("change_pct", 0),
            })
        except:
            enriched.append({**h, "current_price": h["avg_cost"], "value": h["avg_cost"] * h["shares"], "pnl": 0, "pnl_pct": 0, "change_pct": 0})

    total_pnl = total_value - total_cost
    return {
        "holdings": enriched,
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round((total_pnl / total_cost * 100) if total_cost else 0, 2),
    }

@app.post("/api/portfolio")
def add_holding(ticker: str, shares: float, avg_cost: float):
    conn = get_db()
    conn.execute("INSERT INTO portfolio (ticker, shares, avg_cost) VALUES (?, ?, ?)",
                 (ticker.upper(), shares, avg_cost))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/portfolio/{id}")
def delete_holding(id: int):
    conn = get_db()
    conn.execute("DELETE FROM portfolio WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# Alert endpoints
@app.get("/api/alerts")
def get_alerts():
    conn = get_db()
    alerts = [dict(r) for r in conn.execute("SELECT * FROM alerts").fetchall()]
    conn.close()
    return {"alerts": alerts}

@app.post("/api/alerts")
def add_alert(ticker: str, condition: str, threshold: float):
    if condition not in ("above", "below", "change_pct_above"):
        raise HTTPException(400, "Invalid condition")
    conn = get_db()
    conn.execute("INSERT INTO alerts (ticker, condition, threshold) VALUES (?, ?, ?)",
                 (ticker.upper(), condition, threshold))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/alerts/{id}")
def delete_alert(id: int):
    conn = get_db()
    conn.execute("DELETE FROM alerts WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/alerts/check")
def trigger_alert_check(background_tasks: BackgroundTasks):
    background_tasks.add_task(check_alerts)
    return {"status": "checking"}

# Serve frontend
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend" / "static")), name="static")

# ── WebSocket live prices ─────────────────────────────────────────────────────
connected_clients: list[WebSocket] = []

@app.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        connected_clients.remove(websocket)

async def broadcast_prices():
    while True:
        await asyncio.sleep(5)
        if not connected_clients:
            continue
        try:
            data = market_overview()  # reuse existing function
            msg = json.dumps(data)
            for ws in connected_clients.copy():
                try:
                    await ws.send_text(msg)
                except:
                    connected_clients.remove(ws)
        except:
            pass

@app.on_event("startup")
async def startup():
    log.info("=" * 60)
    log.info("Bloomberg Terminal Backend Starting")
    log.info(f"Database: {DB_PATH} {'✓' if DB_PATH.exists() else '✗ MISSING'}")
    log.info(f"Data Dir: {DATA_DIR} {'✓' if DATA_DIR.exists() else '✗ MISSING'}")
    log.info(f"Logs Dir: {LOG_DIR} {'✓' if LOG_DIR.exists() else '✗ MISSING'}")
    log.info("CORS: Enabled for all origins")
    log.info("Features: yfinance, FRED, RSS, WebSocket prices")
    log.info("=" * 60)
    asyncio.create_task(broadcast_prices())

# ── DCF Engine ────────────────────────────────────────────────────────────────
def fetch_dcf_historicals(ticker: str) -> dict:
    cache_key = f"dcf_{ticker}"
    cached = cache_get("price_cache", cache_key, 3600)
    if cached:
        return cached

    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.info

        # Retry logic for network issues
        max_retries = 3
        for attempt in range(max_retries):
            try:
                income  = t.financials        # annual, columns = fiscal year ends
                balance = t.balance_sheet
                cash    = t.cashflow
                if income is not None and not income.empty:
                    break
            except Exception as e:
                if attempt < max_retries - 1:
                    log.warning(f"DCF fetch attempt {attempt + 1} failed for {ticker}: {e}, retrying...")
                    time.sleep(2 ** attempt)  # exponential backoff
                else:
                    raise
    except Exception as e:
        log.error(f"DCF fetch failed for {ticker}: {e}")
        raise HTTPException(status_code=503, detail=f"Could not fetch DCF data for {ticker}: {e}")

    def row(df, *keys):
        for k in keys:
            for idx in df.index:
                if k.lower() in str(idx).lower():
                    return df.loc[idx]
        return None

    def to_series(series):
        if series is None:
            return {}
        out = {}
        for col, val in series.items():
            try:
                year = str(col.year)
                # Always include the year, even if value is NaN/None
                try:
                    out[year] = float(val) if val == val else None  # val != val catches NaN
                except:
                    out[year] = None
            except:
                pass
        return out

    # Handle missing data gracefully
    try:
        revenue_s    = to_series(row(income,  "Total Revenue", "Revenue")) if income is not None and not income.empty else {}
        ebit_s       = to_series(row(income,  "EBIT", "Operating Income")) if income is not None and not income.empty else {}
        da_s         = to_series(row(cash,    "Depreciation", "Depreciation And Amortization")) if cash is not None and not cash.empty else {}
        capex_s      = to_series(row(cash,    "Capital Expenditure", "Capex")) if cash is not None and not cash.empty else {}
        tax_s        = to_series(row(income,  "Tax Provision", "Income Tax")) if income is not None and not income.empty else {}
        pretax_s     = to_series(row(income,  "Pretax Income", "Pre Tax Income")) if income is not None and not income.empty else {}
    except Exception as e:
        log.error(f"Error extracting financial rows for {ticker}: {e}")
        revenue_s = ebit_s = da_s = capex_s = tax_s = pretax_s = {}

    # total debt and cash for net debt
    total_debt   = info.get("totalDebt", 0) or 0
    total_cash   = info.get("totalCash", 0) or 0
    shares       = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding") or 1
    current_price= info.get("currentPrice") or info.get("regularMarketPrice")
    beta         = info.get("beta") or 1.0
    currency     = info.get("currency", "USD")

    # FIX: Sort years chronologically (oldest first), then take last 10
    all_years_set = set(
        list(revenue_s.keys()) + list(ebit_s.keys()) +
        list(da_s.keys()) + list(capex_s.keys())
    )
    all_years_chrono = sorted(all_years_set, key=lambda x: int(x))[-10:]
    # Reverse for display (newest first)
    all_years = list(reversed(all_years_chrono))

    rows = []

    for i, yr in enumerate(all_years_chrono):
        rev    = revenue_s.get(yr)
        ebit   = ebit_s.get(yr)
        da     = da_s.get(yr)
        capex  = capex_s.get(yr)
        tax    = tax_s.get(yr)
        pretax = pretax_s.get(yr)

        ebit_margin = (ebit / rev * 100) if rev and ebit else None

        # Previous fiscal year in chronological order
        prev_rev = revenue_s.get(all_years_chrono[i - 1]) if i > 0 else None

        rev_growth = (
            ((rev - prev_rev) / abs(prev_rev) * 100)
            if rev is not None and prev_rev is not None and prev_rev != 0
            else None
        )

        tax_rate = (
            (tax / pretax * 100)
            if tax is not None and pretax is not None and pretax != 0
            else None
        )

        capex_abs = abs(capex) if capex is not None else None

        fcf = (
            ebit * (1 - (tax_rate or 25) / 100)
            + (da or 0)
            - (capex_abs or 0)
        ) if ebit is not None else None

        fcf_margin = (fcf / rev * 100) if fcf is not None and rev else None
        da_pct = (da / rev * 100) if da is not None and rev else None
        capex_pct = (capex_abs / rev * 100) if capex_abs is not None and rev else None

        rows.append({
            "year":        yr,
            "revenue":     rev,
            "rev_growth":  round(rev_growth, 1) if rev_growth is not None else None,
            "ebit":        ebit,
            "ebit_margin": round(ebit_margin, 1) if ebit_margin is not None else None,
            "da":          da,
            "da_pct":      round(da_pct, 1) if da_pct is not None else None,
            "capex":       capex_abs,
            "capex_pct":   round(capex_pct, 1) if capex_pct is not None else None,
            "tax_rate":    round(tax_rate, 1) if tax_rate is not None else None,
            "fcf":         fcf,
            "fcf_margin":  round(fcf_margin, 1) if fcf_margin is not None else None,
        })

    # Newest first for API/frontend display
    rows.reverse()
    # derive defaults
    def avg(vals):
        v = [x for x in vals if x is not None]
        return sum(v) / len(v) if v else None

    def median(vals):
        v = sorted(x for x in vals if x is not None)
        if not v: return None
        m = len(v) // 2
        return (v[m] + v[m-1]) / 2 if len(v) % 2 == 0 else v[m]

    def cagr(vals):
        v = [x for x in vals if x is not None]
        if len(v) < 2: return None
        return ((v[-1] / v[0]) ** (1 / (len(v)-1)) - 1) * 100

    
    chronological_rows = list(reversed(rows))

    rev_growths  = [r["rev_growth"]  for r in chronological_rows]
    ebit_margins = [r["ebit_margin"] for r in chronological_rows]
    da_pcts      = [r["da_pct"]      for r in chronological_rows]
    capex_pcts   = [r["capex_pct"]   for r in chronological_rows]
    tax_rates    = [r["tax_rate"]    for r in chronological_rows]
    revenues     = [r["revenue"]     for r in chronological_rows]

    # risk-free rate from FRED (10Y treasury)
    try:
        rf_data = fetch_fred_series("DGS10", limit=1)
        rf_rate = rf_data[-1]["value"] if rf_data else 4.5
    except:
        rf_rate = 4.5

    erp   = 5.5  # equity risk premium
    wacc  = round(rf_rate + beta * erp, 2)

    defaults = {
        "rev_growth_1_5":  { "avg": avg(rev_growths[-5:]),  "median": median(rev_growths), "cagr": cagr(revenues), "3y": avg(rev_growths[-3:]) },
        "rev_growth_6_10": { "avg": avg(rev_growths[-5:]),  "median": median(rev_growths), "cagr": cagr(revenues), "3y": avg(rev_growths[-3:]) },
        "ebit_margin":     { "avg": avg(ebit_margins),      "median": median(ebit_margins),"cagr": None,           "3y": avg(ebit_margins[-3:]) },
        "da_pct":          { "avg": avg(da_pcts),           "median": median(da_pcts),     "cagr": None,           "3y": avg(da_pcts[-3:]) },
        "capex_pct":       { "avg": avg(capex_pcts),        "median": median(capex_pcts),  "cagr": None,           "3y": avg(capex_pcts[-3:]) },
        "tax_rate":        { "avg": avg(tax_rates),         "median": median(tax_rates),   "cagr": None,           "3y": avg(tax_rates[-3:]) },
        "wacc":            wacc,
        "rf_rate":         rf_rate,
        "terminal_growth": 2.5,
    }

    result = {
        "ticker":        ticker,
        "name":          info.get("longName", ticker),
        "current_price": current_price,
        "currency":      currency,
        "shares":        shares,
        "net_debt":      total_debt - total_cash,
        "beta":          beta,
        "sector":        info.get("sector", ""),
        "rows":          rows,
        "defaults":      defaults,
    }
    cache_set("price_cache", cache_key, result)
    return result


@app.get("/api/dcf/{ticker}")
def dcf_historicals(ticker: str):
    try:
        return fetch_dcf_historicals(ticker.upper())
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"DCF endpoint error for {ticker}: {e}")
        raise HTTPException(status_code=503, detail=f"DCF data unavailable for {ticker}: {str(e)[:100]}")


@app.post("/api/dcf/calculate")
async def dcf_calculate(request: Request):
    body = await request.json()

    ticker       = body["ticker"]
    shares       = float(body["shares"])
    net_debt     = float(body["net_debt"])
    current_price= float(body["current_price"])
    last_revenue = float(body["last_revenue"])
    assumptions  = body["assumptions"]

    g1      = float(assumptions["rev_growth_1_5"])  / 100
    g2      = float(assumptions["rev_growth_6_10"]) / 100
    margin  = float(assumptions["ebit_margin"])     / 100
    tax     = float(assumptions["tax_rate"])        / 100
    da_pct  = float(assumptions["da_pct"])          / 100
    cx_pct  = float(assumptions["capex_pct"])       / 100
    wacc    = float(assumptions["wacc"])            / 100
    tgr     = float(assumptions["terminal_growth"]) / 100
    periods = int(assumptions.get("periods", 10))

    # Project FCFs
    fcfs   = []
    revs   = []
    rev    = last_revenue
    for yr in range(1, periods + 1):
        g   = g1 if yr <= 5 else g2
        rev = rev * (1 + g)
        ebit= rev * margin
        nopat = ebit * (1 - tax)
        da  = rev * da_pct
        cx  = rev * cx_pct
        fcf = nopat + da - cx
        fcfs.append(fcf)
        revs.append(rev)

    # Terminal value
    tv      = fcfs[-1] * (1 + tgr) / (wacc - tgr) if wacc > tgr else 0
    pv_fcfs = [fcf / (1 + wacc)**i for i, fcf in enumerate(fcfs, 1)]
    pv_tv   = tv / (1 + wacc)**periods

    enterprise_value = sum(pv_fcfs) + pv_tv
    equity_value     = enterprise_value - net_debt
    intrinsic_value  = equity_value / shares

    upside = ((intrinsic_value - current_price) / current_price * 100) if current_price else 0

    if upside > 20:
        signal = "UNDERVALUED"
    elif upside < -20:
        signal = "OVERVALUED"
    else:
        signal = "FAIR VALUE"

    # Sensitivity: wacc ± 2% in 0.5 steps, tgr ± 1% in 0.5 steps
    wacc_range = [round(wacc * 100 + x * 0.5, 1) for x in range(-2, 3)]
    tgr_range  = [round(tgr  * 100 + x * 0.5, 1) for x in range(-2, 3)]
    sensitivity = []
    for w in wacc_range:
        row_s = []
        for tg in tgr_range:
            w_  = w  / 100
            tg_ = tg / 100
            if w_ <= tg_:
                row_s.append(None)
                continue
            tv_s  = fcfs[-1] * (1 + tg_) / (w_ - tg_)
            pv_s  = sum(fcf / (1 + w_)**i for i, fcf in enumerate(fcfs, 1))
            pv_tv_s = tv_s / (1 + w_)**periods
            iv_s  = ((pv_s + pv_tv_s) - net_debt) / shares
            row_s.append(round(iv_s, 2))
        sensitivity.append({"wacc": w, "values": row_s})

    return {
        "intrinsic_value": round(intrinsic_value, 2),
        "equity_value":    round(equity_value, 0),
        "enterprise_value":round(enterprise_value, 0),
        "pv_fcfs":         round(sum(pv_fcfs), 0),
        "pv_tv":           round(pv_tv, 0),
        "upside":          round(upside, 1),
        "signal":          signal,
        "margin_of_safety":round(upside, 1),
        "projected_fcfs":  [round(f, 0) for f in fcfs],
        "projected_revs":  [round(r, 0) for r in revs],
        "sensitivity":     sensitivity,
        "wacc_range":      wacc_range,
        "tgr_range":       tgr_range,
    }

@app.get("/api/news/search")
def news_search(q: str, limit: int = 40):
    import urllib.request
    import urllib.error
    import xml.etree.ElementTree as ET
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
    items = []
    
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                root = ET.fromstring(r.read())
            for item in root.iter("item"):
                title   = item.findtext("title", "").strip()
                link    = item.findtext("link", "").strip()
                pubdate = item.findtext("pubDate", "").strip()
                source  = item.findtext("source", "Google News").strip()
                if title and link:
                    items.append({"source": source, "title": title, "link": link, "date": pubdate})
            break  # Success
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            if attempt < 1:
                log.warning(f"News search attempt {attempt + 1} failed: {e}, retrying...")
                time.sleep(1)
            else:
                log.error(f"News search failed after retries: {e}")
    
    return {"items": items[:limit]}

@app.options("/{full_path:path}")
async def preflight_handler(full_path: str):
    """Handle CORS preflight requests."""
    return {"status": "ok"}

@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    index = BASE_DIR / "frontend" / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"error": "Frontend not found"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)