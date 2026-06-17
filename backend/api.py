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

from fastapi import FastAPI, HTTPException, Request
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


# In-process daily scheduler (cloud only). On the Mac these run as launchd crons;
# the single cloud container runs them itself when NUROQ_INPROC_SCHEDULER=1.
_SCHEDULER_ON = os.getenv("NUROQ_INPROC_SCHEDULER", "0") == "1"
_SCHEDULER_JOBS = 0


@app.on_event("startup")
def _start_scheduler() -> None:
    global _SCHEDULER_JOBS
    if not _SCHEDULER_ON:
        return
    try:
        from scheduler import start_inproc_scheduler
        from premarket_scanner import build_dt_universe as _dt_scan_job
        _SCHEDULER_JOBS = start_inproc_scheduler([
            ("research",   3, 30, dash.trigger_research_cycle_async),
            ("proposals",  8,  0, dash.log_sell_proposals),
            # Premarket scanner runs at 08:05 ET (after proposals at 08:00 so
            # any same-morning research-cycle refresh has settled). Builds
            # today's DTW and writes dt_universe.
            ("dt_scan",    8,  5, lambda: _dt_scan_job(logger=dash.logger)),
        ], dash.logger)
    except Exception as e:
        print(f"[scheduler] ⚠️  failed to start: {e}")


app.add_middleware(
    CORSMiddleware,
    # Vite dev (5173), Capacitor iOS (capacitor://localhost), Capacitor Android
    # (https://localhost), and any LAN-IP origin during native testing.
    allow_origin_regex=r"^(http://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+):\d+|capacitor://localhost|https://localhost|ionic://localhost)$",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ─── Session-cookie auth (single-user password login) ────────────────────────
#
# `https://nuroq.nuroquant.com` is publicly reachable, so /api/* needs a gate
# (anything else and anyone could read positions or place trades). Mechanism:
#   • POST /api/auth/login {password} → sets a signed `nuroq_session` cookie
#   • Every other /api/* requires that cookie
#   • Exempt: SPA shell + static assets, /health, /docs, login + status checks
# Password storage + token signing live in backend/auth.py. Seeded with "nuroq"
# on first run — CHANGE IT via the in-app form.
from backend import auth as _auth  # noqa: E402

_AUTH_EXEMPT_PATHS = {
    "/health", "/api/health",
    "/", "/index.html", "/docs", "/openapi.json", "/redoc",
    "/tunnel-url", "/favicon.ico",
    "/api/auth/login", "/api/auth/status",
}
_AUTH_EXEMPT_PREFIXES = ("/assets/",)   # built SPA bundles (JS/CSS)


def _is_authenticated(request) -> bool:
    return _auth.verify_token(request.cookies.get(_auth.COOKIE_NAME))


@app.middleware("http")
async def _session_guard(request, call_next):
    from starlette.responses import JSONResponse
    path = request.url.path
    exempt = (request.method == "OPTIONS"
              or path in _AUTH_EXEMPT_PATHS
              or path.startswith(_AUTH_EXEMPT_PREFIXES))
    if not exempt and not _is_authenticated(request):
        return JSONResponse(status_code=401,
                            content={"detail": "Not authenticated."})
    return await call_next(request)


@app.get("/health")
def health():
    """Unauthenticated liveness probe for GCE/load-balancer health checks."""
    return {
        "ok": True, "service": "nuroq",
        "ai_backend": os.getenv("NUROQ_AI_BACKEND", "gemma"),
        "scheduler": _SCHEDULER_ON, "scheduler_jobs": _SCHEDULER_JOBS,
    }


@app.get("/tunnel-url")
def tunnel_url():
    """Returns the current Cloudflare quick-tunnel https URL (cloudflared writes
    it to /data/cloudflared.log). Unauthenticated — the URL is meant to be
    visited, and the app behind it is still key/cookie gated."""
    import re, pathlib
    log = pathlib.Path("/data/cloudflared.log")
    if not log.exists():
        return {"url": None, "note": "no tunnel log yet (named tunnel, or still starting)"}
    try:
        m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text())
        return {"url": m[-1] if m else None}
    except Exception as e:
        return {"url": None, "note": str(e)}


# ─── Auth: login / logout / status / change-password ────────────────────────

class LoginReq(BaseModel):
    password: str


class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str


class AuthStatusResp(BaseModel):
    authenticated: bool
    must_change_password: bool   # true while still using the seeded "nuroq"
    ai_backend: str              # "gemma" (local MLX) or "gemini" (cloud Vertex)


@app.get("/api/auth/status", response_model=AuthStatusResp)
def auth_status(request: Request):
    """Cheap check the SPA polls at boot to decide login screen vs main UI.

    Also surfaces `ai_backend` so the SPA can show/hide backend-specific UI —
    notably the A/B compare button (only useful from the local Gemma box, where
    the *peer* is the cloud's Gemini). On the cloud box the button is hidden,
    since comparing the cloud against itself is meaningless and the cloud can't
    reach the user's Mac.
    """
    return AuthStatusResp(
        authenticated=_is_authenticated(request),
        # If "nuroq" still works, flag the SPA to nag the user to change it.
        must_change_password=_auth.verify_password(_auth.INITIAL_PASSWORD),
        ai_backend=dash.analyst.backend,
    )


@app.post("/api/auth/login")
def auth_login(req: LoginReq):
    from starlette.responses import JSONResponse
    if not _auth.verify_password(req.password):
        # No timing-safe message — just a generic 401. (verify_password itself
        # uses hmac.compare_digest so the hash compare is constant-time.)
        return JSONResponse(status_code=401,
                            content={"ok": False, "detail": "Wrong password."})
    token = _auth.issue_token()
    resp = JSONResponse({"ok": True})
    # Same-site lax so the cookie survives top-level navigation; httponly so
    # JS can't read it. 30-day life matches the token's exp claim.
    resp.set_cookie(_auth.COOKIE_NAME, token, max_age=_auth.SESSION_TTL,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    from starlette.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_auth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/auth/change-password")
def auth_change_password(req: ChangePasswordReq, request: Request):
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(status_code=401,
                            content={"ok": False, "detail": "Not authenticated."})
    if not _auth.verify_password(req.current_password):
        return JSONResponse(status_code=401,
                            content={"ok": False, "detail": "Current password is wrong."})
    if len(req.new_password) < 6:
        return JSONResponse(status_code=400,
                            content={"ok": False,
                                     "detail": "New password must be at least 6 characters."})
    _auth.change_password(req.new_password)
    # Re-issue a fresh session bound to the new secret stays the same
    # (session_secret is per-box, unchanged by password rotation).
    return JSONResponse({"ok": True})


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


class BenchmarkSeries(BaseModel):
    closes: list[float]      # daily closes, aligned to the equity_series indices
    return_pct: float        # total %-change over the same window


class EquityHistoryResp(BaseModel):
    equity_series: list[float]
    timestamps: list[int]
    return_pct: float
    period_days: int
    benchmarks: dict[str, BenchmarkSeries] = {}   # e.g. {"SPY": …, "VOO": …}


# In-memory cache for benchmark history — yfinance batch fetch takes 1-2s
# and the data only changes once a day per close. Key: (symbols, days).
_bench_cache: dict = {}
_BENCH_TTL_SEC = 60 * 30


def _fetch_benchmarks(symbols: list[str], n_points: int, days: int) -> dict[str, BenchmarkSeries]:
    """Daily closes for a list of benchmark tickers, trimmed to roughly match
    the user's equity-series length so the chart's three lines line up by index.

    Cached 30 min — yfinance latency dominates this endpoint otherwise. On any
    failure returns {} so the chart silently degrades to just the user's line."""
    if not symbols or n_points < 2:
        return {}
    key = (tuple(symbols), days, n_points)
    now = time.time()
    cached = _bench_cache.get(key)
    if cached and now - cached["at"] < _BENCH_TTL_SEC:
        return cached["data"]

    import yfinance as yf
    try:
        # Pull a bit more than `days` (weekends, holidays) so we have enough
        # closes after dropping NaN rows.
        period = f"{max(days + 7, 10)}d"
        data = yf.download(
            symbols, period=period, group_by="ticker",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning(f"benchmark fetch failed: {e}")
        return {}

    out: dict[str, BenchmarkSeries] = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                closes_series = data["Close"]
            else:
                closes_series = data[sym]["Close"]
            closes = [float(c) for c in closes_series.dropna().tolist()]
            if len(closes) < 2:
                continue
            # Trim to n_points trailing days so it aligns with the user's
            # equity series (Alpaca returns N points; benchmark returns ~N too,
            # taking the last N gives the most-recent matching window).
            closes = closes[-n_points:]
            ret = (closes[-1] / closes[0] - 1) * 100 if closes[0] else 0.0
            out[sym] = BenchmarkSeries(closes=closes, return_pct=round(ret, 2))
        except Exception:
            continue

    _bench_cache[key] = {"at": now, "data": out}
    return out


@app.get("/api/alpaca/history", response_model=EquityHistoryResp)
def alpaca_history(days: int = 30, benchmarks: str = "SPY,VOO"):
    """
    Equity-value series for the Today sparkline + the Portfolio comparison chart.

    `benchmarks` (comma-separated) is overlaid on the chart so the user can see
    relative performance vs the broader market. Frontend normalizes each series
    to "% from baseline" so a $96k equity line and a $737 SPY line are visually
    comparable. Pass empty string to skip the benchmark fetch entirely.
    """
    hist = dash.alpaca_api.get_portfolio_history(period_days=days)
    equity_series = hist.get("equity_series", []) or []

    bench_syms = [s.strip().upper() for s in (benchmarks or "").split(",") if s.strip()]
    bench_data = _fetch_benchmarks(bench_syms, len(equity_series), days) if bench_syms else {}

    return EquityHistoryResp(
        equity_series=equity_series,
        timestamps=hist.get("timestamps", []) or [],
        return_pct=float(hist.get("return_pct", 0) or 0),
        period_days=days,
        benchmarks=bench_data,
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
        with sqlite3.connect(dash.DB_PATH) as conn:
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
        with sqlite3.connect(dash.DB_PATH) as conn:
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


class ResearchStatusResp(BaseModel):
    active: bool
    progress: int           # tickers analyzed so far
    total: int              # tickers in scope (0 until the first thread tick)
    percent: float          # 0..100, rounded to one decimal
    elapsed_sec: int        # 0 when idle
    eta_sec: Optional[int]  # extrapolated; None when idle or progress is 0
    started_at: Optional[float]
    last_completed_at: Optional[float]   # unix ts of most recent finished cycle
    last_count: int                       # # candidates in current watchlist


@app.get("/api/research-cycle/status", response_model=ResearchStatusResp)
def research_cycle_status():
    """Live progress of an in-flight research cycle, plus a "last completed"
    marker from `watchlist_today` so the UI can show staleness even when no
    cycle is running. Cheap (no Alpaca calls), safe to poll every few seconds.
    """
    st = dash._research_in_progress
    active = bool(st.get("active"))
    progress = int(st.get("progress") or 0)
    total = int(st.get("total") or 0)
    percent = (progress / total * 100) if total else 0.0
    started = st.get("started_at")
    started_ts: Optional[float] = None
    elapsed = 0
    eta: Optional[int] = None
    if active and started is not None:
        # `started_at` is a datetime in the dashboard's internal state.
        try:
            started_ts = started.timestamp()
        except Exception:
            started_ts = None
        if started_ts is not None:
            elapsed = max(0, int(time.time() - started_ts))
            if progress > 0 and total > 0:
                eta = int(elapsed * (total - progress) / progress)

    meta = dash.watchlist_today.get_metadata() or {}
    return ResearchStatusResp(
        active=active,
        progress=progress,
        total=total,
        percent=round(percent, 1),
        elapsed_sec=elapsed,
        eta_sec=eta,
        started_at=started_ts,
        last_completed_at=(meta.get("generated_at") or None),
        last_count=int(meta.get("count") or 0),
    )


class ScanReq(BaseModel):
    mode: str  # "top20" | "global"


@app.post("/api/scan")
def scan(req: ScanReq):
    """Start a scan in the background and return immediately. The scan can run for
    minutes (20+ tickers × rate-limited fetch + AI), which exceeds Cloudflare's
    100s tunnel timeout — so the UI polls GET /api/scan/status for the result."""
    return dash.start_scan_async(req.mode)


@app.get("/api/scan/status")
def scan_status():
    s = dash.scan_status()
    return {"running": s["active"], "rows": s["rows"], "summary": s["summary"],
            "error": s["error"], "mode": s["mode"], "started_at": s["started_at"]}


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
    Grounds the AI with price action + news + SEC-filing RAG + fundamentals.
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


class PortfolioContribution(BaseModel):
    ticker: str
    intraday_pl: float
    change_pct: float
    market_value: float


class InsightResp(BaseModel):
    """Auto-generated 'why is the account up/down today' insight."""
    summary: str
    pnl_dollars: float
    pnl_pct: float
    equity: float
    top_contributors: list[PortfolioContribution]
    top_detractors: list[PortfolioContribution]
    sources: list
    grounded: bool
    generated_at: float


@app.get("/api/insight/today", response_model=InsightResp)
def insight_today(force: bool = False):
    """AI-generated explanation of why the account is up or down today,
    plus structured top-contributors / top-detractors. Cached for 5 min keyed
    by date + positions + P&L bucket; pass `?force=true` to bypass the cache.
    """
    r = dash.build_portfolio_insight(force=force)
    return InsightResp(**r)


class PortfolioAskReq(BaseModel):
    question: str


class PortfolioAskResp(BaseModel):
    question: str
    answer: str
    sources: list
    grounded: bool


@app.post("/api/ask-portfolio", response_model=PortfolioAskResp)
def ask_portfolio(req: PortfolioAskReq):
    """Free-form Q&A grounded with the user's positions, today's per-position
    P&L, fresh news on held tickers, and recent agent activity. Sibling of
    /api/ask which is ticker-specific."""
    if not req.question.strip():
        raise HTTPException(400, "question required")
    r = dash.ask_portfolio_question(req.question)
    return PortfolioAskResp(**r)


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


# ─── Quant sell proposals (Option B: the quant layer proposing SALES) ────────

class SellProposal(BaseModel):
    ticker: str
    kind: str                       # TAX_LOSS_HARVEST | ROTATE | EXIT_WEAK
    shares: float
    current_price: float
    avg_cost: float
    unrealized_pl: float
    unrealized_pl_pct: float
    score: Optional[int] = None
    rotate_into: Optional[str] = None
    section_475: bool = False
    reason: str


class ProposeSellsResp(BaseModel):
    section_475: bool
    proposals: list[SellProposal]


@app.get("/api/propose-sells", response_model=ProposeSellsResp)
def propose_sells():
    """
    Deliberate quant pass over held positions → ranked SELL proposals
    (tax-loss harvest under §475, rotate into a stronger name, or exit a
    decayed holding). Advisory only — the UI acts via the normal SELL paths.
    """
    proposals = dash.propose_sells()
    return ProposeSellsResp(
        section_475=dash.section_475_active(),
        proposals=[SellProposal(**p) for p in proposals],
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

    Faster than /api/analyze/{ticker} (no AI inference). Uses cached daily
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
    Deep analysis: technicals + fundamentals + AI reasoning + trade setup
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


# ─── Backend A/B comparison: peer (cloud Gemini) second opinion ──────────────
#
# Fetches a peer instance's read of the same ticker for side-by-side comparison.
# Canonical use: local Mac (Gemma MLX) calling https://nuroq.nuroquant.com
# (Gemini via Vertex). The quant rubric is identical on both boxes; only the
# gated ~10pt AI tiebreaker differs.
#
# Auth flow: peer login is password-based now (same as the SPA), so this calls
# the peer's POST /api/auth/login with NUROQ_PEER_PASSWORD, caches the session
# cookie, and reuses it. Re-logs in on a 401. Config:
#   NUROQ_COMPARE_URL      peer base URL (default https://nuroq.nuroquant.com)
#   NUROQ_PEER_PASSWORD    peer password (compare disabled if unset)
# Peer failures degrade gracefully (peer=null + note) — never 500s the view.

import json as _json
import urllib.request as _urlreq
import urllib.error as _urlerr

COMPARE_URL = os.getenv("NUROQ_COMPARE_URL", "https://nuroq.nuroquant.com").rstrip("/")
PEER_PASSWORD = os.getenv("NUROQ_PEER_PASSWORD", "").strip()
_peer_session: dict[str, str] = {}   # {"cookie": "nuroq_session=..."}


def _backend_label(name: Optional[str]) -> str:
    n = (name or "").strip().lower()
    if n == "gemma":
        return "Gemma · local MLX"
    if n in ("gemini", "vertex", "google"):
        return "Gemini · cloud Vertex"
    return name or "unknown"


def _peer_login() -> None:
    """POST password to peer's /api/auth/login; stash the session cookie."""
    body = _json.dumps({"password": PEER_PASSWORD}).encode()
    req = _urlreq.Request(
        COMPARE_URL + "/api/auth/login",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "NuroQ-Compare/2.0"},
    )
    with _urlreq.urlopen(req, timeout=10.0) as resp:  # noqa: S310 (trusted URL)
        # urllib gives us the Set-Cookie header verbatim — grab just the
        # name=value pair (everything before the first `;`).
        sc = resp.headers.get("Set-Cookie", "")
        if not sc:
            raise RuntimeError("peer login returned no Set-Cookie")
        _peer_session["cookie"] = sc.split(";", 1)[0].strip()


def _peer_get(path: str, timeout: float, _retry: bool = True) -> dict:
    """GET {COMPARE_URL}{path} authenticating via cached session cookie.

    A real User-Agent is required: the cloud peer sits behind Cloudflare, which
    403s the default `Python-urllib/x` UA as a bot. On 401 we re-login once.
    """
    if not _peer_session.get("cookie"):
        _peer_login()
    req = _urlreq.Request(
        COMPARE_URL + path,
        headers={"Cookie": _peer_session["cookie"],
                 "User-Agent": "NuroQ-Compare/2.0"},
    )
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return _json.loads(resp.read().decode("utf-8"))
    except _urlerr.HTTPError as e:
        # Session expired → re-login once then retry. After that, give up.
        if e.code == 401 and _retry:
            _peer_session.pop("cookie", None)
            return _peer_get(path, timeout, _retry=False)
        raise


class PeerSide(BaseModel):
    backend: str
    ok: bool
    final_score: Optional[int] = None
    rating: Optional[str] = None
    ai_score: Optional[int] = None
    ai_reasoning: Optional[str] = None
    ai_key_risk: Optional[str] = None
    price: Optional[float] = None
    elapsed_s: Optional[float] = None
    error: Optional[str] = None


class PeerCompareResp(BaseModel):
    ticker: str
    local_backend: str            # friendly label for THIS instance's backend
    peer: Optional[PeerSide] = None
    note: Optional[str] = None


@app.get("/api/analyze/peer/{ticker}", response_model=PeerCompareResp)
def analyze_peer(ticker: str):
    """Fetch a peer instance's AI analysis of `ticker` for side-by-side compare.

    The frontend renders the local column from its existing analysis; this only
    adds the peer (cloud Gemini) opinion. See the module note above for config.
    """
    ticker = ticker.upper().strip()
    if not ticker:
        raise HTTPException(400, "ticker required")

    local_backend = _backend_label(dash.analyst.backend)

    if not PEER_PASSWORD:
        return PeerCompareResp(
            ticker=ticker, local_backend=local_backend, peer=None,
            note="Cloud comparison disabled — set NUROQ_PEER_PASSWORD "
                 "(the cloud box's login password) to enable the Gemini side.",
        )

    # Best-effort: ask the peer what backend it runs so we can label the column.
    peer_backend = "Peer"
    try:
        peer_backend = _backend_label(_peer_get("/health", 8.0).get("ai_backend"))
    except Exception:
        pass

    t0 = time.time()
    try:
        p = _peer_get(f"/api/analyze/{ticker}", 30.0)
    except _urlerr.HTTPError as e:
        return PeerCompareResp(
            ticker=ticker, local_backend=local_backend,
            peer=PeerSide(backend=peer_backend, ok=False, error=f"{e.code} {e.reason}"),
            note=f"Peer {COMPARE_URL} returned {e.code}.",
        )
    except Exception as e:
        return PeerCompareResp(
            ticker=ticker, local_backend=local_backend,
            peer=PeerSide(backend=peer_backend, ok=False, error=e.__class__.__name__),
            note=f"Could not reach {COMPARE_URL} for the Gemini comparison.",
        )

    peer = PeerSide(
        backend=peer_backend, ok=True,
        final_score=p.get("final_score"), rating=p.get("rating"),
        ai_score=p.get("ai_score"), ai_reasoning=p.get("ai_reasoning"),
        ai_key_risk=p.get("ai_key_risk"), price=p.get("price"),
        elapsed_s=round(time.time() - t0, 1),
    )
    return PeerCompareResp(ticker=ticker, local_backend=local_backend, peer=peer)


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


# ─── Auto-trade configuration + control ──────────────────────────────────────
#
# The Configuration tab in the SPA reads/writes this single row. Three control
# endpoints (halt / resume / flatten-all) let the user pull the e-brake from
# anywhere — phone, browser, curl.

import agent_config as _agent_config


class AgentConfigResp(BaseModel):
    budget: float
    max_concurrent: int
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    entry_window_start: str
    entry_window_end: str
    eod_flatten_time: str
    margin_allowed: bool
    auto_trade_enabled: bool
    notify_on_trade: bool
    halted_at: Optional[int]
    halt_reason: Optional[str]
    pending_open_flatten: Optional[int] = None
    # §475(f) state: separates the deploy-set env var from the user's
    # acknowledgement that the actual IRS election has been filed.
    # `section_475_env_active` is read-only (from NUROQ_SECTION_475);
    # `section_475_election_filed` is editable via /api/config.
    # The Configuration UI flags red when these disagree (env ON but the
    # user hasn't confirmed the election is on file with the IRS).
    section_475_env_active: bool = False
    section_475_election_filed: bool = False
    # Day-trader (Session 8): intraday agent, separate from swing crossings.
    # Defaults: disabled mode, tighter risk/concurrency, 14:30 ET cutoff.
    dt_mode: str = "disabled"
    dt_max_concurrent: int = 3
    dt_risk_per_trade_pct: float = 0.5
    dt_entry_window_end: str = "14:30"
    dt_volume_multiplier: float = 2.0
    dt_require_vwap: bool = True
    dt_time_stop_bars: int = 30
    dt_target_r_multiple: float = 2.0
    dt_universe: str = ""
    updated_at: int


class AgentConfigUpdateReq(BaseModel):
    budget: Optional[float] = None
    max_concurrent: Optional[int] = None
    risk_per_trade_pct: Optional[float] = None
    daily_loss_limit_pct: Optional[float] = None
    entry_window_start: Optional[str] = None
    entry_window_end: Optional[str] = None
    eod_flatten_time: Optional[str] = None
    margin_allowed: Optional[bool] = None
    auto_trade_enabled: Optional[bool] = None
    notify_on_trade: Optional[bool] = None
    section_475_election_filed: Optional[bool] = None
    # Day-trader knobs (Session 8)
    dt_mode: Optional[str] = None
    dt_max_concurrent: Optional[int] = None
    dt_risk_per_trade_pct: Optional[float] = None
    dt_entry_window_end: Optional[str] = None
    dt_volume_multiplier: Optional[float] = None
    dt_require_vwap: Optional[bool] = None
    dt_time_stop_bars: Optional[int] = None
    dt_target_r_multiple: Optional[float] = None
    dt_universe: Optional[str] = None


def _config_with_env(cfg: dict) -> dict:
    """Layer the read-only NUROQ_SECTION_475 env-var state on top of the
    DB-backed config dict. Done here (not in agent_config.py) because the
    env var is a deployment concern, not a config concern."""
    cfg = dict(cfg)
    cfg["section_475_env_active"] = os.getenv("NUROQ_SECTION_475", "0") == "1"
    return cfg


@app.get("/api/config", response_model=AgentConfigResp)
def get_agent_config():
    return AgentConfigResp(**_config_with_env(_agent_config.get()))


@app.post("/api/config", response_model=AgentConfigResp)
def update_agent_config(req: AgentConfigUpdateReq):
    """Update one or more config fields. Unknown keys silently dropped by the
    module's whitelist. Returns the post-update config."""
    upd = {k: v for k, v in req.model_dump().items() if v is not None}
    cfg = _agent_config.update(**upd)
    return AgentConfigResp(**_config_with_env(cfg))


class HaltReq(BaseModel):
    reason: str = "manual"


@app.post("/api/auto-trade/halt", response_model=AgentConfigResp)
def auto_trade_halt(req: HaltReq):
    """Hard-stop the autonomous trader. Persists across restarts. Does NOT
    flatten positions by itself — call /api/flatten-all if that's intended."""
    cfg = _agent_config.halt(req.reason or "manual")
    return AgentConfigResp(**_config_with_env(cfg))


@app.post("/api/auto-trade/resume", response_model=AgentConfigResp)
def auto_trade_resume():
    """Clear a halt. Does NOT re-enable auto_trade — that's a separate POST
    /api/config so a stale halt → resume doesn't surprise-start trading."""
    cfg = _agent_config.clear_halt()
    return AgentConfigResp(**_config_with_env(cfg))


@app.post("/api/auto-trade/clear-pending-flatten", response_model=AgentConfigResp)
def auto_trade_clear_pending_flatten():
    """Clear a stale `pending_open_flatten` flag manually. Normally the EOD
    daemon clears this itself after a successful flatten (including the
    0-positions case as of Session 8). This endpoint is escape-hatch for
    when an older container's logic is stuck looping the retry."""
    _agent_config.clear_open_flatten()
    cfg = _agent_config.get()
    return AgentConfigResp(**_config_with_env(cfg))


class DayTraderStatusResp(BaseModel):
    """Snapshot of the live day-trader engine. Safe to poll at 5-10s; engine
    state is in-process, no DB hit."""
    mode: str                          # disabled | shadow | approve | auto
    session_date: str
    fires_today: int
    open_positions: list[str]
    open_position_count: int
    universe_size: int


@app.get("/api/day-trader/status", response_model=DayTraderStatusResp)
def day_trader_status():
    """Day-trader engine snapshot. Returns mode + zeros if the engine isn't
    initialized in this process (e.g. live agent never started)."""
    engine = getattr(getattr(getattr(dash, "agent", None), "live_agent", None),
                     "day_trader", None)
    if engine is None:
        return DayTraderStatusResp(
            mode=_agent_config.get().get("dt_mode", "disabled"),
            session_date="", fires_today=0, open_positions=[],
            open_position_count=0, universe_size=0,
        )
    s = engine.status()
    return DayTraderStatusResp(
        mode=s.get("mode", "disabled"),
        session_date=s.get("session_date", ""),
        fires_today=int(s.get("fires_today", 0)),
        open_positions=list(s.get("open_positions", [])),
        open_position_count=int(s.get("open_position_count", 0)),
        universe_size=int(s.get("universe_size", 0)),
    )


class DayTraderModeReq(BaseModel):
    mode: str   # disabled | shadow | approve | auto


@app.post("/api/day-trader/mode", response_model=AgentConfigResp)
def day_trader_set_mode(req: DayTraderModeReq):
    """Promote/demote the day-trader mode in one click. Refuses unknown modes."""
    valid = {"disabled", "shadow", "approve", "auto"}
    if (req.mode or "").lower() not in valid:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {sorted(valid)}")
    cfg = _agent_config.update(dt_mode=req.mode.lower())
    return AgentConfigResp(**_config_with_env(cfg))


class DayTraderScanRow(BaseModel):
    ticker: str
    prev_close: float
    last_premkt_price: float
    gap_pct: float
    premkt_volume: int
    catalyst: str
    catalyst_weight: float
    gms: float
    headline: str = ""


class DayTraderScanResp(BaseModel):
    session_date: str
    scanned: int
    kept: int
    rows: list[DayTraderScanRow]
    universe: str
    filters: dict


@app.post("/api/day-trader/scan", response_model=DayTraderScanResp)
def day_trader_scan():
    """Run the premarket scanner NOW and write dt_universe. Same code path
    the scheduler runs at 08:05 ET — exposed for manual trigger from the UI.
    Safe to call any time of day; bars are cached after first fetch."""
    try:
        from premarket_scanner import build_dt_universe
        result = build_dt_universe(logger=dash.logger)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scanner failed: {e}")
    return DayTraderScanResp(
        session_date=result.get("session_date", ""),
        scanned=int(result.get("scanned", 0)),
        kept=int(result.get("kept", 0)),
        rows=[DayTraderScanRow(**r) for r in result.get("rows", [])],
        universe=str(result.get("universe", "")),
        filters=dict(result.get("filters", {})),
    )


class FlattenResp(BaseModel):
    closed_count: int
    cancelled_orders: int
    queued_for_open: int = 0   # MOO orders queued when market is closed
    errors: list[str]


@app.post("/api/flatten-all", response_model=FlattenResp)
def flatten_all():
    """Sell every open Alpaca position immediately. If the market is closed,
    Alpaca queues the SELLs for next regular session open. Also cancels open
    SL/TP bracket orders so they don't fight the flatten."""
    res = dash.alpaca_api.flatten_all_positions()
    return FlattenResp(**res)


class AutoTradeStatusResp(BaseModel):
    enabled: bool
    halted: bool
    halt_reason: Optional[str]
    today_buys: int
    today_sells: int
    open_positions: int
    todays_pl: float
    todays_pl_pct: float
    equity: float
    cash: float
    on_margin: bool


@app.get("/api/auto-trade/status", response_model=AutoTradeStatusResp)
def auto_trade_status():
    cfg = _agent_config.get()
    acct = dash.alpaca_api.get_account_summary() or {}
    positions = dash.alpaca_api.list_positions() or []
    try:
        with sqlite3.connect(dash.DB_PATH) as conn:
            today_start = int(time.time()) - 24 * 3600
            buys = conn.execute(
                "SELECT COUNT(*) FROM live_triggers WHERE ts > ? "
                "AND action IN ('FIRED_BUY','AUTO_EXECUTED')",
                (today_start,),
            ).fetchone()[0]
            sells = conn.execute(
                "SELECT COUNT(*) FROM live_triggers WHERE ts > ? "
                "AND action='FIRED_SELL'",
                (today_start,),
            ).fetchone()[0]
    except Exception:
        buys = sells = 0
    cash = float(acct.get("cash") or 0)
    return AutoTradeStatusResp(
        enabled=bool(cfg.get("auto_trade_enabled")),
        halted=bool(cfg.get("halted_at")),
        halt_reason=cfg.get("halt_reason"),
        today_buys=buys,
        today_sells=sells,
        open_positions=len(positions),
        todays_pl=float(acct.get("todays_pl") or 0),
        todays_pl_pct=float(acct.get("todays_pl_pct") or 0),
        equity=float(acct.get("equity") or 0),
        cash=cash,
        on_margin=cash < 0,
    )


@app.get("/api/agent/log", response_model=list[AgentLogRow])
def agent_log(limit: int = 100):
    try:
        with sqlite3.connect(dash.DB_PATH) as conn:
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
