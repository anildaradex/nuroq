"""
NuroQ React-frontend backend.

FastAPI app that wraps the existing dashboard helpers as JSON endpoints.
The Python computation layer is unchanged — we only add a presentation API.

Run (dev):
    cd stock-ai-startup
    ./.venv/bin/uvicorn backend.api:app --port 8000 --reload

Run (prod):
    ./.venv/bin/uvicorn backend.api:app --port 8000 --workers 1

In dev, Vite serves the frontend on :5173 and proxies /api → :8000.
In prod, set FRONTEND_DIST=/path/to/frontend/dist; FastAPI mounts the built
SPA at / and serves the API at /api/*.
"""

from __future__ import annotations

import os
import sys
import time
import sqlite3
from pathlib import Path
from typing import Optional

# Make the repo root importable so we can reach dashboard.py + siblings.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Disable background services BEFORE importing dashboard — the React backend
# manages its own lifecycle and we don't want the Gradio Telegram bot competing
# for the bot-token getUpdates slot.
os.environ.setdefault("NUROQ_BACKGROUND_SERVICES", "1")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Pull in everything we need from the existing dashboard module. This re-uses
# all the proven helpers (status pills, today cards, next actions, channel
# bar, drill-down, etc.) — we just return their data as JSON instead of HTML.
import dashboard as dash  # noqa: E402

app = FastAPI(title="NuroQ API", version="2.0")


# ─── Auto-start the live agent on backend boot ───────────────────────────────
#
# When NUROQ_AUTOSTART_AGENT != "0", the FastAPI startup event tries to start
# the live agent automatically. This means: launch the backend before market
# open (or have it always running), and the agent comes online without you
# clicking START AGENT in the UI.
#
# Idempotent (skips if already running). Failures (empty watchlist, market
# closed without force, WebSocket connect refused) are logged but don't crash
# the backend — the UI still works, just no live agent.
#
# Disable: NUROQ_AUTOSTART_AGENT=0 ./.venv/bin/uvicorn backend.api:app …
_AUTOSTART_AGENT = os.getenv("NUROQ_AUTOSTART_AGENT", "1") != "0"


@app.on_event("startup")
def _autostart_agent() -> None:
    if not _AUTOSTART_AGENT:
        print("[autostart] NUROQ_AUTOSTART_AGENT=0 — skipping agent autostart.")
        return
    try:
        if dash.agent.is_running:
            print("[autostart] Agent already running — skipping.")
            return
        msg = dash.agent.start()
        print(f"[autostart] {msg}")
    except Exception as e:
        # Don't crash startup if the agent can't come up (e.g. WebSocket
        # connection refused outside market hours). UI still works.
        print(f"[autostart] ⚠️  Agent autostart failed: {e}")


app.add_middleware(
    CORSMiddleware,
    # Vite dev (5173), Capacitor iOS (capacitor://localhost), Capacitor Android
    # (https://localhost), and any LAN-IP origin during native testing.
    allow_origin_regex=r"^(http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+):\d+|capacitor://localhost|https://localhost|ionic://localhost)$",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ─── Schemas ─────────────────────────────────────────────────────────────────

class StatusPillsResp(BaseModel):
    agent: str
    stream: str
    telegram: str
    alpaca: str
    ts: float


class AlpacaSummaryResp(BaseModel):
    connected: bool
    status: str = ""
    equity: float = 0
    cash: float = 0
    buying_power: float = 0
    positions_value: float = 0
    todays_pl: float = 0
    todays_pl_pct: float = 0
    thirty_day_return_pct: Optional[float] = None


class PendingOrderResp(BaseModel):
    symbol: str
    side: str
    qty: float
    order_type: str
    is_bracket: bool
    limit_price: Optional[float]
    stop_price: Optional[float]
    status: str
    submitted_at: Optional[str]


class WatchlistMeta(BaseModel):
    buys: int
    holds: int
    sells: int
    generated_at: Optional[float]


class AgentStatus(BaseModel):
    running: bool
    subscribed_tickers: int = 0
    bars_processed: int = 0
    buys_fired_today: int = 0
    buys_cap: int = 0
    sells_fired_today: int = 0
    started_at: Optional[str] = None
    latest_bar_ts: Optional[float] = None


class TodayCardsResp(BaseModel):
    watchlist: WatchlistMeta
    agent: AgentStatus
    news_24h: dict


class NextActionResp(BaseModel):
    level: str  # "ok" | "warn" | "err"
    text: str


class FeedEventResp(BaseModel):
    ts: float
    kind: str  # "trigger" | "news"
    ticker: str
    classification: Optional[str] = None
    action: Optional[str] = None
    direction: Optional[str] = None
    score_before: Optional[int] = None
    score_after: Optional[int] = None
    price: Optional[float] = None
    headline: Optional[str] = None


class WatchlistRow(BaseModel):
    ticker: str
    rank: int
    ai_score: Optional[int]
    quant_score: Optional[int]
    recommendation: str
    price: float
    change_pct: float
    technicals_summary: str
    fundamentals_summary: str
    generated_at: float


class PortfolioRowResp(BaseModel):
    ticker: str
    shares: float
    avg_price: float
    current_price: float
    total_value: float
    pnl_pct: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    ai_score: Optional[int]
    ai_rating: Optional[str]
    entry_date: Optional[str]


class MutationResp(BaseModel):
    ok: bool
    message: str


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _pill_state_for(label_md: str) -> str:
    """Map the Markdown emoji-pill format (🟢 / 🟡 / 🔴 / ⚪) back to a state."""
    if "🟢" in label_md: return "ok"
    if "🟡" in label_md: return "warn"
    if "🔴" in label_md: return "err"
    return "off"


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/status/pills", response_model=StatusPillsResp)
def status_pills():
    # Parse the existing render_status_pills() markdown to extract the 4 states.
    md = dash.render_status_pills()
    parts = md.split("·")  # the renderer joins with " · "
    # Pills are in order: Agent, Stream, Telegram, Alpaca
    return StatusPillsResp(
        agent    = _pill_state_for(parts[0] if len(parts) > 0 else ""),
        stream   = _pill_state_for(parts[1] if len(parts) > 1 else ""),
        telegram = _pill_state_for(parts[2] if len(parts) > 2 else ""),
        alpaca   = _pill_state_for(parts[3] if len(parts) > 3 else ""),
        ts       = time.time(),
    )


@app.get("/api/alpaca/summary", response_model=AlpacaSummaryResp)
def alpaca_summary():
    acct = dash.alpaca_api.get_account_summary()
    if not acct.get("connected"):
        return AlpacaSummaryResp(connected=False)
    hist = dash.alpaca_api.get_portfolio_history(period_days=30)
    thirty = (hist.get("return_pct") if hist.get("connected") and hist.get("equity_series") else None)
    return AlpacaSummaryResp(
        connected=True,
        status=acct["status"],
        equity=acct["equity"],
        cash=acct["cash"],
        buying_power=acct["buying_power"],
        positions_value=acct["positions_value"],
        todays_pl=acct["todays_pl"],
        todays_pl_pct=acct["todays_pl_pct"],
        thirty_day_return_pct=thirty,
    )


class EquityHistoryResp(BaseModel):
    equity_series: list[float]
    timestamps: list[int]
    return_pct: float
    period_days: int


@app.get("/api/alpaca/history", response_model=EquityHistoryResp)
def alpaca_history(days: int = 30):
    """
    Equity-value series for the sparkline on the Today view. Returns the
    same shape Alpaca's portfolio_history API provides.
    """
    hist = dash.alpaca_api.get_portfolio_history(period_days=days)
    return EquityHistoryResp(
        equity_series=hist.get("equity_series", []) or [],
        timestamps=hist.get("timestamps", []) or [],
        return_pct=float(hist.get("return_pct", 0) or 0),
        period_days=days,
    )


@app.get("/api/alpaca/orders", response_model=list[PendingOrderResp])
def pending_orders():
    orders = dash.alpaca_api.get_open_orders(limit=25) or []
    return [
        PendingOrderResp(
            symbol=o["symbol"], side=o["side"],
            qty=o["qty"], order_type=o["order_type"],
            is_bracket=o["is_bracket"],
            limit_price=o.get("limit_price"),
            stop_price=o.get("stop_price"),
            status=o["status"],
            submitted_at=o.get("submitted_at"),
        )
        for o in orders
    ]


@app.get("/api/today/cards", response_model=TodayCardsResp)
def today_cards():
    all_wl = dash.watchlist_today.get_all()
    meta = dash.watchlist_today.get_metadata()

    wl_meta = WatchlistMeta(
        buys=sum(1 for r in all_wl if r["recommendation"] == "BUY"),
        holds=sum(1 for r in all_wl if r["recommendation"] == "HOLD"),
        sells=sum(1 for r in all_wl if r["recommendation"] == "SELL"),
        generated_at=meta["generated_at"],
    )

    try:
        a = dash.agent.status()
        agent_status = AgentStatus(
            running=bool(a.get("running")),
            subscribed_tickers=int(a.get("subscribed_tickers", 0)),
            bars_processed=int(a.get("bars_processed", 0)),
            buys_fired_today=int(a.get("buys_fired_today", 0)),
            buys_cap=int(a.get("buys_cap", 0)),
            sells_fired_today=int(a.get("sells_fired_today", 0)),
            started_at=a.get("started_at"),
            latest_bar_ts=a.get("latest_bar_ts"),
        )
    except Exception:
        agent_status = AgentStatus(running=False)

    news_24h = {}
    try:
        with sqlite3.connect("nuroq.db") as conn:
            rows = conn.execute(
                "SELECT classification, COUNT(*) FROM news_cache "
                "WHERE ingested_at > ? AND classification != 'NEUTRAL' "
                "GROUP BY classification",
                (time.time() - 86400,),
            ).fetchall()
        news_24h = {k: n for k, n in rows}
    except Exception:
        pass

    return TodayCardsResp(watchlist=wl_meta, agent=agent_status, news_24h=news_24h)


@app.get("/api/today/next-actions", response_model=list[NextActionResp])
def next_actions():
    # Parse the existing render_next_actions() markdown into structured items.
    md = dash.render_next_actions()
    out: list[NextActionResp] = []
    for line in md.split("\n"):
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].lstrip()
        level = "ok"
        if body.startswith("🔴"): level = "err"
        elif body.startswith("🟡"): level = "warn"
        elif body.startswith("🟢"): level = "ok"
        # Strip the leading emoji + space
        text = body.split(" ", 1)[1] if " " in body else body
        out.append(NextActionResp(level=level, text=text))
    return out


@app.get("/api/today/feed", response_model=list[FeedEventResp])
def today_feed():
    events: list[FeedEventResp] = []
    day_ago = time.time() - 86400
    try:
        with sqlite3.connect("nuroq.db") as conn:
            for ts, ticker, direction, sb, sa, price, action, _notes in conn.execute(
                "SELECT ts, ticker, direction, score_before, score_after, price, action, notes "
                "FROM live_triggers WHERE ts > ? ORDER BY ts DESC LIMIT 30",
                (day_ago,),
            ).fetchall():
                events.append(FeedEventResp(
                    ts=ts, kind="trigger", ticker=ticker,
                    direction=direction, action=action,
                    score_before=sb, score_after=sa, price=price,
                ))
            for ticker, headline, classification, ingested_at in conn.execute(
                "SELECT ticker, headline, classification, ingested_at FROM news_cache "
                "WHERE ingested_at > ? AND classification != 'NEUTRAL' "
                "ORDER BY ingested_at DESC LIMIT 20",
                (day_ago,),
            ).fetchall():
                events.append(FeedEventResp(
                    ts=ingested_at, kind="news", ticker=ticker,
                    classification=classification, headline=headline,
                ))
    except Exception as e:
        raise HTTPException(500, f"Feed query failed: {e}")
    events.sort(key=lambda e: e.ts, reverse=True)
    return events[:50]


@app.get("/api/watchlist", response_model=list[WatchlistRow])
def watchlist():
    return [WatchlistRow(**r) for r in dash.watchlist_today.get_all()]


@app.get("/api/portfolio", response_model=list[PortfolioRowResp])
def portfolio():
    df = dash.portfolio_mgr.get_portfolio()
    if df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append(PortfolioRowResp(
            ticker=str(r.get("Ticker", "")),
            shares=float(r.get("Shares", 0) or 0),
            avg_price=float(r.get("Avg Price", 0) or 0),
            current_price=float(r.get("Current Price", 0) or 0),
            total_value=float(r.get("Total Value", 0) or 0),
            pnl_pct=float(r.get("PnL %", 0) or 0),
            stop_loss=(float(r["Stop Loss"]) if r.get("Stop Loss") not in (None, "", "—") else None),
            take_profit=(float(r["Take Profit"]) if r.get("Take Profit") not in (None, "", "—") else None),
            ai_score=(int(r["AI Score"]) if r.get("AI Score") not in (None, "", "—") else None),
            ai_rating=(str(r["AI Rating"]) if r.get("AI Rating") not in (None, "", "—") else None),
            entry_date=str(r.get("Entry Date", "") or "") or None,
        ))
    return out


@app.post("/api/agent/start", response_model=MutationResp)
def agent_start():
    msg = dash.agent.start()
    return MutationResp(ok=dash.agent.is_running, message=msg)


@app.post("/api/agent/stop", response_model=MutationResp)
def agent_stop():
    msg = dash.agent.stop()
    return MutationResp(ok=not dash.agent.is_running, message=msg)


@app.get("/api/agent/status", response_model=AgentStatus)
def agent_status():
    try:
        a = dash.agent.status()
        return AgentStatus(
            running=bool(a.get("running")),
            subscribed_tickers=int(a.get("subscribed_tickers", 0)),
            bars_processed=int(a.get("bars_processed", 0)),
            buys_fired_today=int(a.get("buys_fired_today", 0)),
            buys_cap=int(a.get("buys_cap", 0)),
            sells_fired_today=int(a.get("sells_fired_today", 0)),
            started_at=a.get("started_at"),
            latest_bar_ts=a.get("latest_bar_ts"),
        )
    except Exception:
        return AgentStatus(running=False)


@app.post("/api/research-cycle", response_model=MutationResp)
def research_cycle():
    # Note: in v2.0 we trust the React UI to enforce the consent gate.
    # In a hardened deploy we'd require a confirm token here.
    msg = dash.trigger_research_cycle_async()
    return MutationResp(ok=True, message=msg)


class ScanReq(BaseModel):
    mode: str  # "top20" | "global"


@app.post("/api/scan")
def scan(req: ScanReq):
    if req.mode == "global":
        rows, summary = dash.deep_market_scan()
    else:
        rows, summary = dash.scan_market()
    return {"rows": rows.to_dict(orient="records") if hasattr(rows, "to_dict") else [],
            "summary": summary}


# ─── Trade (Quick Trade endpoint) ────────────────────────────────────────────

class TradeReq(BaseModel):
    ticker: str
    shares: float
    action: str          # "buy" | "sell"
    order_type: str = "Market"   # "Market" | "Limit" | "Stop" | "Stop Limit" | "Trailing Stop"
    tif: str = "GTC"     # "Day" | "GTC" | "OPG" | "IOC" | "FOK"
    sl: Optional[float] = None   # Stop loss (bracket if both sl + tp on a Market order)
    tp: Optional[float] = None   # Take profit
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    # Client-supplied UUID for double-submit prevention. If the same key is
    # seen within IDEMPOTENCY_TTL_SECONDS, the cached response is replayed
    # without re-hitting Alpaca. The React Quick Trade generates one per
    # button click; rapid retaps within the TTL collapse to a single order.
    idempotency_key: Optional[str] = None
    # Set true to bypass the wash-sale block on this submission. The React UI
    # only sends this after explicitly showing the user the risk warning and
    # getting a second confirmation tap.
    wash_sale_override: bool = False


# Idempotency cache for /api/trade. OrderedDict for O(1) FIFO eviction.
# Real submissions are rare (manual clicks); 256 entries is plenty.
from collections import OrderedDict as _OrderedDict
_TRADE_IDEMPOTENCY: "_OrderedDict[str, tuple[MutationResp, float]]" = _OrderedDict()
_TRADE_IDEMPOTENCY_TTL = 10.0   # seconds
_TRADE_IDEMPOTENCY_MAX = 256


def _trade_idempotency_lookup(key: str) -> Optional[MutationResp]:
    """Return the cached response if `key` was seen within TTL, else None."""
    if not key:
        return None
    now = time.time()
    # Purge stale entries from the FIFO head
    while _TRADE_IDEMPOTENCY:
        k, (_, ts) = next(iter(_TRADE_IDEMPOTENCY.items()))
        if now - ts > _TRADE_IDEMPOTENCY_TTL:
            _TRADE_IDEMPOTENCY.popitem(last=False)
        else:
            break
    if key in _TRADE_IDEMPOTENCY:
        resp, _ = _TRADE_IDEMPOTENCY[key]
        return resp
    return None


def _trade_idempotency_remember(key: str, resp: MutationResp) -> None:
    if not key:
        return
    _TRADE_IDEMPOTENCY[key] = (resp, time.time())
    # Bound the cache size
    while len(_TRADE_IDEMPOTENCY) > _TRADE_IDEMPOTENCY_MAX:
        _TRADE_IDEMPOTENCY.popitem(last=False)


@app.post("/api/trade", response_model=MutationResp)
def trade(req: TradeReq):
    """
    Submits a Quick Trade order via Alpaca. Wraps dashboard.handle_quick_trade
    so the routing logic (bracket vs single, validation) stays in one place.

    Double-submit prevention: if `idempotency_key` matches a recent submission
    (within IDEMPOTENCY_TTL_SECONDS), returns the cached response without
    re-hitting Alpaca. This catches the "user double-tapped BUY before React
    disabled the button" failure mode that produced two SAN brackets at
    2026-05-26 08:48:51 and 08:48:52.
    """
    if not req.ticker.strip():
        raise HTTPException(400, "ticker required")
    if req.shares is None or req.shares <= 0:
        raise HTTPException(400, "shares must be > 0")

    # Idempotency replay
    if req.idempotency_key:
        cached = _trade_idempotency_lookup(req.idempotency_key)
        if cached is not None:
            return MutationResp(
                ok=cached.ok,
                message=f"♻️ (Replayed within {_TRADE_IDEMPOTENCY_TTL:.0f}s) {cached.message}",
            )

    msg = dash.handle_quick_trade(
        ticker=req.ticker.upper().strip(),
        shares=req.shares,
        action=req.action.lower().strip(),
        order_type=req.order_type,
        tif=req.tif,
        limit_price=req.limit_price,
        stop_price=req.stop_price,
        sl_price=req.sl,
        tp_price=req.tp,
        wash_sale_override=req.wash_sale_override,
    )
    ok = msg.startswith("✅") or "Simulated" in msg
    resp = MutationResp(ok=ok, message=msg)
    _trade_idempotency_remember(req.idempotency_key or "", resp)
    return resp


# ─── Wash-sale check (read-only) ─────────────────────────────────────────────

class WashSaleResp(BaseModel):
    ticker: str
    risk: bool
    recent_sells: list
    likely_loss_sells: list
    hint: str
    days_until_safe: int


class AskReq(BaseModel):
    ticker: str
    question: str


class AskResp(BaseModel):
    ticker: str
    question: str
    answer: str
    sources: list
    grounded: bool


@app.post("/api/ask", response_model=AskResp)
def ask(req: AskReq):
    """
    Free-form 'AI mode' Q&A about a ticker (the search bar under the chart).
    Grounds Gemma with price action + news + SEC-filing RAG + fundamentals.
    Runs LLM inference — allow several seconds.
    """
    if not req.ticker.strip():
        raise HTTPException(400, "ticker required")
    if not req.question.strip():
        raise HTTPException(400, "question required")
    r = dash.ask_about_ticker(req.ticker, req.question)
    return AskResp(
        ticker=r["ticker"], question=r["question"], answer=r["answer"],
        sources=r["sources"], grounded=r["grounded"],
    )


class ProtectReq(BaseModel):
    ticker: str
    shares: Optional[int] = None   # default: full position qty from Alpaca
    sl: Optional[float] = None     # default: local tracker's stop_loss
    tp: Optional[float] = None     # default: local tracker's take_profit


@app.post("/api/protect", response_model=MutationResp)
def protect(req: ProtectReq):
    """
    Place a REAL protective OCO (stop-loss + take-profit) SELL at Alpaca on an
    existing long position, so it auto-exits even if NuroQ is offline. Defaults
    shares/sl/tp to the position's current qty + the local tracker's levels.
    """
    t = req.ticker.upper().strip()
    if not t:
        raise HTTPException(400, "ticker required")

    # Resolve shares from Alpaca position if not given
    shares = req.shares
    sl = req.sl
    tp = req.tp
    try:
        positions = dash.alpaca_api.list_positions() or []
        pos = next((p for p in positions if p["symbol"] == t), None)
        if pos is None:
            raise HTTPException(404, f"No live Alpaca position for {t}")
        if shares is None:
            shares = int(pos["qty"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Position lookup failed: {e}")

    # Fall back to local tracker SL/TP
    if sl is None or tp is None:
        df = dash.portfolio_mgr.get_portfolio()
        row = df[df["Ticker"].str.upper() == t] if not df.empty else None
        if row is not None and not row.empty:
            if sl is None:
                sl = float(row.iloc[0].get("Stop Loss") or 0)
            if tp is None:
                tp = float(row.iloc[0].get("Take Profit") or 0)

    if not sl or not tp:
        raise HTTPException(400, f"{t}: SL/TP required (none in tracker). Pass sl & tp.")

    msg = dash.alpaca_api.submit_protective_oco(t, int(shares), float(sl), float(tp))
    ok = msg.startswith("✅") or "Simulated" in msg
    return MutationResp(ok=ok, message=msg)


@app.get("/api/wash-sale/{ticker}", response_model=WashSaleResp)
def wash_sale(ticker: str):
    """
    Returns wash-sale risk assessment for `ticker`. The React iOS UI calls
    this BEFORE submitting a BUY so it can show the warning + override flow
    in the OrderReviewModal without round-tripping to /api/trade and back.
    """
    r = dash.wash_sale_check(ticker)
    return WashSaleResp(
        ticker=r["ticker"], risk=r["risk"],
        recent_sells=r["recent_sells"],
        likely_loss_sells=r["likely_loss_sells"],
        hint=r["hint"], days_until_safe=r["days_until_safe"],
    )


# ─── Trade setup (sized order ready to review + submit) ──────────────────────

class TradeSetupResp(BaseModel):
    ticker: str
    price: float
    shares: int
    sl: float
    tp: float
    atr: float


@app.get("/api/trade-setup/{ticker}", response_model=TradeSetupResp)
def trade_setup(ticker: str):
    """
    ATR-based sized BUY order ready to pre-fill the Quick Trade form.
    Used by Watchlist row "Trade" buttons so the user can click a ticker
    and immediately get a reviewed, sized order without running full analysis.

    Faster than /api/analyze/{ticker} (no Gemma inference). Uses cached daily
    bars + the same calculate_sizing helper the live agent uses.
    """
    t = ticker.upper().strip()
    if not t:
        raise HTTPException(400, "ticker required")
    bars = dash.history_cache.get(t, allow_stale=True) or []
    if not bars:
        raise HTTPException(404, f"No cached price history for {t}")
    price = float(bars[-1].get("c") or 0)
    if price <= 0:
        raise HTTPException(400, f"Invalid price for {t}")
    techs = dash.calculate_technicals(bars) or {}
    atr = float(techs.get("atr") or max(price * 0.02, 0.5))
    sizing = dash.calculate_sizing(price, atr=atr, account=dash._live_equity())
    return TradeSetupResp(
        ticker=t,
        price=round(price, 2),
        shares=int(sizing.get("shares", 0)),
        sl=round(float(sizing.get("sl", 0)), 2),
        tp=round(float(sizing.get("tp", 0)), 2),
        atr=round(atr, 2),
    )


# ─── Analyze (deep single-ticker analysis) ───────────────────────────────────

class AnalyzeResp(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    industry: Optional[str] = None
    price: float
    change_pct: float
    final_score: int
    rating: str
    technicals: dict
    fundamentals: dict
    ai_score: Optional[int] = None
    ai_reasoning: Optional[str] = None
    ai_bull_case: Optional[str] = None
    ai_bear_case: Optional[str] = None
    ai_key_risk: Optional[str] = None
    ai_considerations: list[str] = []
    trade_setup: dict           # shares, sl, tp, atr, position_value, days_to_earnings
    chart: dict                  # bars[], sma20[], upper_bb[], lower_bb[]


@app.get("/api/analyze/{ticker}", response_model=AnalyzeResp)
def analyze(ticker: str):
    """
    Deep analysis: technicals + fundamentals + Gemma reasoning + trade setup
    + chart data. Runs the full LLM inference, so allow 3-5 seconds.
    """
    ticker = ticker.upper().strip()
    if not ticker:
        raise HTTPException(400, "ticker required")

    result = dash.analyze_single_ticker_data(ticker)
    if not result:
        raise HTTPException(404, f"No data available for {ticker}")

    # Pull the cached AI score (analyze_single_ticker_data writes it through)
    cached_ai = dash.ai_score_cache.get(ticker) or {}

    # Re-fetch fundamentals (memoized) + history for chart
    funds = dash.get_fundamentals(ticker) or {}
    history = dash.history_cache.get(ticker, allow_stale=True) or []
    techs = dash.calculate_technicals(history) if history else {}

    # ATR-based sizing (same formula as the live agent uses)
    price = float(result.get("Price", 0))
    atr = float(techs.get("atr") or max(price * 0.02, 0.5))
    sizing = dash.calculate_sizing(price, atr=atr, account=dash._live_equity())
    e_risk = dash.get_earnings_risk(ticker) or {"risk": False, "days": 30}

    # Chart series: last 120 bars + SMA20 + Bollinger bands. The frontend
    # renders these as overlaid lines on a candlestick or line chart.
    bars = history[-120:] if len(history) > 120 else history
    sma20, upper_bb, lower_bb = [], [], []
    if bars:
        closes = [b.get("c", 0) for b in bars]
        for i, _ in enumerate(closes):
            window = closes[max(0, i - 19): i + 1]
            if len(window) < 5:
                sma20.append(None); upper_bb.append(None); lower_bb.append(None)
                continue
            m = sum(window) / len(window)
            sma20.append(round(m, 2))
            if len(window) >= 20:
                import statistics
                sd = statistics.pstdev(window)
                upper_bb.append(round(m + 2 * sd, 2))
                lower_bb.append(round(m - 2 * sd, 2))
            else:
                upper_bb.append(None); lower_bb.append(None)

    return AnalyzeResp(
        ticker=ticker,
        company_name=funds.get("name"),
        industry=funds.get("industry"),
        price=price,
        change_pct=float(result.get("Change %", 0)),
        final_score=int(result.get("Score", 0)),
        rating=str(result.get("Rating", "HOLD")),
        technicals={
            "rsi":         techs.get("rsi"),
            "percent_b":   techs.get("percent_b"),
            "atr":         techs.get("atr"),
            "trend":       techs.get("trend"),
            "rel_vol":     techs.get("rel_vol"),
            "sma_20":      techs.get("sma_20"),
            "gain_20d":    techs.get("gain_20d"),
            "semantic_rsi": techs.get("semantic_rsi"),
            "semantic_bb": techs.get("semantic_bb"),
            "volatility":  techs.get("volatility"),
        },
        fundamentals={
            "pe":     funds.get("pe"),
            "growth": funds.get("growth"),
            "name":   funds.get("name"),
            "industry": funds.get("industry"),
        },
        ai_score=int(cached_ai["score"]) if cached_ai.get("score") is not None else None,
        ai_reasoning=cached_ai.get("reasoning"),
        ai_bull_case=cached_ai.get("bull_case"),
        ai_bear_case=cached_ai.get("bear_case"),
        ai_key_risk=cached_ai.get("key_risk"),
        ai_considerations=cached_ai.get("considerations") or [],
        trade_setup={
            "shares":         int(sizing.get("shares", 0)),
            "sl":             round(float(sizing.get("sl", 0)), 2),
            "tp":             round(float(sizing.get("tp", 0)), 2),
            "atr":            round(atr, 2),
            "position_value": round(int(sizing.get("shares", 0)) * price, 2),
            "earnings_days":  int(e_risk.get("days", 30)),
            "earnings_risk":  bool(e_risk.get("risk", False)),
        },
        chart={
            "bars": [
                {"t": b.get("t"), "o": b.get("o"), "h": b.get("h"),
                 "l": b.get("l"), "c": b.get("c"), "v": b.get("v")}
                for b in bars
            ],
            "sma20": sma20,
            "upper_bb": upper_bb,
            "lower_bb": lower_bb,
        },
    )


# ─── Portfolio remove ────────────────────────────────────────────────────────

class RemovePositionReq(BaseModel):
    ticker: str


@app.post("/api/portfolio/remove", response_model=MutationResp)
def remove_position(req: RemovePositionReq):
    t = req.ticker.upper().strip()
    if not t:
        raise HTTPException(400, "ticker required")
    try:
        dash.portfolio_mgr.remove_position(t)
        return MutationResp(ok=True, message=f"Removed {t} from portfolio.")
    except Exception as e:
        raise HTTPException(500, f"Remove failed: {e}")


# ─── Signal history ──────────────────────────────────────────────────────────

class SignalRow(BaseModel):
    timestamp: str
    ticker: str
    name: Optional[str]
    industry: Optional[str]
    price: float
    technicals: Optional[str]
    fundamentals: Optional[str]
    signal: str
    ai_score: Optional[int]
    quant_score: Optional[int]


@app.get("/api/signals", response_model=list[SignalRow])
def signals():
    df = dash.portfolio_mgr.get_buy_signals()
    if df is None or df.empty:
        return []
    out: list[SignalRow] = []
    for _, r in df.iterrows():
        try:
            out.append(SignalRow(
                timestamp=str(r.get("Timestamp", "")),
                ticker=str(r.get("Ticker", "")),
                name=str(r.get("Name", "")) or None,
                industry=str(r.get("Industry", "")) or None,
                price=float(r.get("Price", 0) or 0),
                technicals=str(r.get("Technicals", "")) or None,
                fundamentals=str(r.get("Fundamentals", "")) or None,
                signal=str(r.get("Signal", "HOLD")),
                ai_score=(int(r["AI Score"]) if r.get("AI Score") not in (None, "", "—") else None),
                quant_score=(int(r["Quant Score"]) if r.get("Quant Score") not in (None, "", "—") else None),
            ))
        except Exception:
            continue
    return out


# ─── Agent activity log (recent live_triggers) ───────────────────────────────

class AgentLogRow(BaseModel):
    ts: float
    ticker: str
    direction: str
    score_before: Optional[int]
    score_after: Optional[int]
    price: Optional[float]
    action: str
    notes: Optional[str]


@app.get("/api/agent/log", response_model=list[AgentLogRow])
def agent_log(limit: int = 100):
    try:
        with sqlite3.connect("nuroq.db") as conn:
            rows = conn.execute(
                "SELECT ts, ticker, direction, score_before, score_after, "
                "price, action, notes FROM live_triggers "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            AgentLogRow(
                ts=r[0], ticker=r[1], direction=r[2],
                score_before=r[3], score_after=r[4], price=r[5],
                action=r[6], notes=r[7],
            )
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(500, f"Agent log query failed: {e}")


# ─── Activity log (tail of activity.log) ─────────────────────────────────────

@app.get("/api/logs")
def activity_log(lines: int = 200):
    path = ROOT / "activity.log"
    if not path.is_file():
        return {"lines": [], "path": str(path)}
    try:
        with open(path, "rb") as f:
            # Read last N lines efficiently — seek from end
            f.seek(0, 2)
            size = f.tell()
            blocksize = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read = min(blocksize, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
        tail = text.splitlines()[-lines:]
        return {"lines": tail, "path": str(path)}
    except Exception as e:
        raise HTTPException(500, f"Log read failed: {e}")


# ─── System health (parsed health snapshot) ──────────────────────────────────

class HealthComponent(BaseModel):
    state: str           # "ok" | "warn" | "err" | "off"
    name: str
    status: str


@app.get("/api/system/health", response_model=list[HealthComponent])
def system_health():
    """
    Parse the markdown table from render_health_snapshot() back into structured
    rows. Quick and dirty — preserves the single source of truth in dashboard.py.
    """
    md = dash.render_health_snapshot()
    out: list[HealthComponent] = []
    state_map = {"🟢": "ok", "🟡": "warn", "🔴": "err", "⚪": "off"}
    for line in md.split("\n"):
        line = line.strip()
        if not line.startswith("|") or "---" in line or line.startswith("| |"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        emoji = cells[0]
        state = state_map.get(emoji, "off")
        if state == "off" and emoji not in state_map:
            continue  # header row
        # Strip markdown bold (**…**) and backtick code spans from the parsed cells.
        def _clean(s: str) -> str:
            return s.replace("**", "").replace("`", "").strip()
        out.append(HealthComponent(state=state, name=_clean(cells[1]), status=_clean(cells[2])))
    return out


# ─── Static SPA mount (production) ───────────────────────────────────────────
#
# In production, build the React frontend with `cd frontend && npm run build`,
# then this serves the bundle at /. In dev you don't need this — Vite handles
# it on :5173 and proxies API calls back here.

_DIST = ROOT / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
