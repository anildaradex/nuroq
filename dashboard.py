import gradio as gr
from typing import Optional
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import requests
import time
import os
import pandas as pd
import sys
import argparse
import yfinance as yf
import json
import re
import sqlite3
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import date, timedelta, datetime
from mlx_lm import load, generate
from mlx_lm.sample_utils import make_sampler
import asyncio
import threading
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from memory_module import AgentMemory
from event_stream import MarketStreamer

# --- New modular imports ---
from data_fetcher import (
    PolygonRateLimiter, AppCache,
    rate_limiter, news_cache, funds_cache,
    fundamentals_cache, ai_score_cache, watchlist_today, live_triggers,
    history_cache,
    get_polygon_news, get_full_history,
    get_fundamentals, get_fundamentals_batch_async,
    get_history_batch_async,
    get_earnings_risk, get_sentiment,
)
from scoring import (
    calculate_technicals, get_weekly_confluence,
    calculate_sizing, calculate_quant_score,
)

# Global Agent Memory Instance
agent_memory = AgentMemory()

# --- New execution import ---
from alpaca_executor import LiveAlpacaExecutor

# Load environment variables
load_dotenv()

# PolygonRateLimiter, AppCache, caches now imported from data_fetcher

# --- 1. CONFIGURATION & ARGUMENTS ---
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

class AppLogger:
    """Thread-safe logger for tracking application activities."""
    def __init__(self, log_file="activity.log"):
        self.log_file = log_file
        self.activities = []
        self._lock = threading.Lock()
        # Initialize log file
        with open(self.log_file, "a") as f:
            f.write(f"\n--- SESSION STARTED AT {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [{level}] {message}"
        with self._lock:
            self.activities.append(entry)
            # Keep only last 500 logs in memory
            if len(self.activities) > 500:
                self.activities.pop(0)
        
        # Write to persistent file
        with open(self.log_file, "a") as f:
            f.write(entry + "\n")
        print(entry)

    def get_logs(self):
        with self._lock:
            return "\n".join(self.activities)

    def clear(self):
        with self._lock:
            self.activities = []

# Global Logger Instance
logger = AppLogger()

# Model Definitions — all paths point to local HuggingFace cache snapshots
# to skip network checks on every startup.
MODELS_CFG = {
    "gemma": {
        "path": os.path.expanduser(
            "~/.cache/huggingface/hub/models--mlx-community--gemma-3-4b-it-4bit"
            "/snapshots/93724907d4ed1745d2fe50baadf3b0b01a65abf2"
        ),
        "adapter": None
    }
}


class EnsembleAnalyst:
    def __init__(self, mode="single"):
        self.mode = mode
        self.models = {}
        self.tokenizers = {}

    def load_all(self):
        logger.log("🚀 Loading Gemma model...")
        cfg = MODELS_CFG["gemma"]
        m, t = load(cfg["path"], adapter_path=cfg["adapter"])
        self.models["gemma"] = m
        self.tokenizers["gemma"] = t

    def analyze(self, prompt, model_key="gemma"):
        m = self.models["gemma"]
        t = self.tokenizers["gemma"]
        
        # Reduced max_tokens and added stop sequences to prevent generation loops
        sampler = make_sampler(temp=0.0)
        response = generate(
            m, t,
            prompt=prompt,
            sampler=sampler,
            max_tokens=500
        )
        return response

    def get_consensus(self, ticker, prompt):
        """No consensus check needed. Returns Gemma analysis directly."""
        logger.log(f"[{ticker}] ▶ Step 5a: Running Gemma inference...")
        res = self.analyze(prompt, "gemma")
        score = self.extract_score(res)
        logger.log(f"[{ticker}]    Gemma result → score={score}")

        rating = self.get_structured_data(res).get("rating", "HOLD")
        
        # Sanity Check: Ensure Rating matches the Score intensity
        old_rating = rating
        if score < 70 and rating == "BUY":
            rating = "HOLD"
        elif score > 80 and rating == "HOLD":
            rating = "BUY"
            
        if old_rating != rating:
            logger.log(f"[{ticker}] 🛡️ Sanity Guard: Overriding {old_rating} to {rating} (Score {score} too low/high)")

        combined_reasoning = f"--- GEMMA ({score}) ---\n{res}"
        
        return {
            "is_consensus": True,
            "score": score,
            "rating": rating,
            "response": combined_reasoning
        }

    def extract_score(self, text):
        structured = self.get_structured_data(text)
        return structured.get("score", 50)

    def get_structured_data(self, text):
        """Robustly parses LLM output into structured data. Uses raw_decode to handle trailing text."""
        # 0. Strip DeepSeek R1 thinking tags
        clean_text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        # Also strip markdown code fences
        clean_text = re.sub(r'```json\s*', '', clean_text)
        clean_text = re.sub(r'```\s*', '', clean_text)

        # 1. Find first '{' and use raw_decode — this IGNORES trailing text after the JSON
        brace_pos = clean_text.find('{')
        if brace_pos != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(clean_text, brace_pos)
                # Ensure required keys exist with defaults
                return {
                    "reasoning":      obj.get("reasoning", clean_text[:500]),
                    "considerations": obj.get("considerations", []),
                    "weights":        obj.get("weights", {}),
                    "metrics":        obj.get("metrics", {}),
                    "rating":         str(obj.get("rating", "HOLD")).upper(),
                    "score":          int(obj.get("score", 50))
                }
            except json.JSONDecodeError as e:
                logger.log(f"JSON raw_decode failed ({e}), falling back to regex", level="WARNING")

        # 2. Regex fallback — extract fields individually
        reasoning_match   = re.search(r'["\']reasoning["\']:\s*["\'](.*?)["\']', clean_text, re.DOTALL)
        rating_match      = re.search(r'["\']rating["\']:\s*["\'](\w+)["\']', clean_text)
        if not rating_match:
            rating_match  = re.search(r'(?:FINAL\s+)?RATING(?:\s+is|:)\s*[\'"]*(\w+)[\'"]*', clean_text, re.IGNORECASE)
        score_match       = re.search(r'["\']score["\']:\s*(\d+)', clean_text)
        if not score_match:
            score_match   = re.search(r'(?:FINAL\s+)?SCORE(?:\s+is|:)\s*(\d+)', clean_text, re.IGNORECASE)
        cons_match        = re.search(r'["\']considerations["\']:\s*\[(.*?)\]', clean_text, re.DOTALL)
        considerations    = [c.strip().strip('"').strip("'") for c in cons_match.group(1).split(",")] if cons_match else []

        reasoning = reasoning_match.group(1) if reasoning_match else (clean_text[:1500] if len(clean_text) > 10 else "No structured reasoning found.")

        # 3. De-duplicate repetitive lines (loop breaker)
        seen, unique = set(), []
        for line in reasoning.split("\n"):
            s = line.strip()
            if s and (s not in seen or len(s) <= 20):
                unique.append(line)
            seen.add(s)
        reasoning = "\n".join(unique)

        return {
            "reasoning":      reasoning,
            "considerations": considerations,
            "weights":        {},
            "metrics":        {},
            "rating":         rating_match.group(1).upper() if rating_match else "HOLD",
            "score":          int(score_match.group(1)) if score_match else 50
        }



# Global Analyst Instance
parser = argparse.ArgumentParser(description="Run the Stock AI Dashboard")
parser.add_argument("--mode", type=str, default="single", choices=["single", "ensemble"],
                    help="Choose 'single' (one model) or 'ensemble' (consensus mode)")
parser.add_argument("--model", type=str, default="gemma", choices=["gemma"],
                    help="Choose the model to use in single mode")
args, unknown = parser.parse_known_args()

analyst = EnsembleAnalyst(mode=args.mode)
analyst.load_all()

SELECTED_MODEL_KEY = "gemma"
MODEL_PATH = MODELS_CFG["gemma"]["path"]
ADAPTER_PATH = None

model = analyst.models["gemma"]
tokenizer = analyst.tokenizers["gemma"]


class TradeGatekeeper:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.approval_event = asyncio.Event()
        self.user_choice = None
        self.app = None
        self.loop = None

    async def start(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        self.user_choice = query.data
        await query.edit_message_text(text=f"🔘 Choice Received: {self.user_choice}. Processing...")
        self.approval_event.set()

    async def request_approval(self, ticker, price, score, reasoning):
        self.approval_event.clear()
        self.user_choice = None
        
        message = (
            f"🚀 **AI TRADE SIGNAL: {ticker}**\n"
            f"Price: ${price} | AI Score: {score}/100\n\n"
            f"🧠 **Analysis:**\n{reasoning[:500]}...\n\n"
            f"Do you want to execute this trade?"
        )
        
        keyboard = [[
            InlineKeyboardButton("✅ EXECUTE", callback_data='EXECUTE'),
            InlineKeyboardButton("❌ CANCEL", callback_data='CANCEL'),
        ]]
        
        await self.app.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
        try:
            await asyncio.wait_for(self.approval_event.wait(), timeout=300)
            return self.user_choice
        except asyncio.TimeoutError:
            return "TIMEOUT"

    def send_notification(self, message: str) -> None:
        """Fire-and-forget Telegram alert. Safe to call from any thread."""
        if not self.app or not self.loop:
            logger.log("⚠️ Gatekeeper not started; dropping notification.", level="WARNING")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(chat_id=self.chat_id, text=message),
                self.loop,
            )
        except Exception as e:
            logger.log(f"⚠️ Notification dispatch failed: {e}", level="WARNING")

class ShadowExecutor:
    def __init__(self, db_path="nuroq.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS shadow_trades (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT,
                            ticker TEXT,
                            action TEXT,
                            price REAL,
                            shares REAL,
                            total REAL
                        )''')
            
            # Migrate existing CSV if table is empty
            cursor = conn.execute("SELECT COUNT(*) FROM shadow_trades")
            if cursor.fetchone()[0] == 0 and os.path.exists("shadow_trades.csv"):
                try:
                    df = pd.read_csv("shadow_trades.csv")
                    df.to_sql("shadow_trades", conn, if_exists="append", index=False)
                except Exception as e:
                    logger.log(f"⚠️ Failed to migrate shadow_trades.csv: {e}")

    def execute_trade(self, ticker, price, amount=200):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        shares = round(amount / price, 4)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO shadow_trades (timestamp, ticker, action, price, shares, total) VALUES (?, ?, ?, ?, ?, ?)",
                         (timestamp, ticker, "BUY", price, shares, amount))
            
        logger.log(f"💾 Trade logged: BUY {shares} {ticker} @ ${price}")
        return f"✅ [SHADOW MODE] Simulated BUY of {shares} shares of {ticker} at ${price}."

class PortfolioManager:
    def __init__(self, db_path="nuroq.db"):
        self.db_path = db_path
        self.cols = ["Ticker", "Shares", "Avg Price", "Current Price", "Total Value", "PnL %", "Stop Loss", "Take Profit", "AI Score", "AI Rating", "Entry Date"]
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS portfolio (
                            ticker TEXT PRIMARY KEY,
                            shares REAL,
                            avg_price REAL,
                            current_price REAL,
                            total_value REAL,
                            pnl_pct REAL,
                            stop_loss REAL,
                            take_profit REAL,
                            ai_score REAL,
                            ai_rating TEXT,
                            entry_date TEXT
                        )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS all_signals (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT,
                            ticker TEXT,
                            name TEXT,
                            industry TEXT,
                            price REAL,
                            technicals TEXT,
                            fundamentals TEXT,
                            signal TEXT,
                            ai_score REAL,
                            quant_score REAL
                        )''')
            
            # Migrate CSVs if tables are empty
            cursor = conn.execute("SELECT COUNT(*) FROM portfolio")
            if cursor.fetchone()[0] == 0 and os.path.exists("portfolio.csv"):
                try:
                    df = pd.read_csv("portfolio.csv")
                    if not df.empty:
                        # Rename columns to match sqlite schema for migration
                        df.columns = ["ticker", "shares", "avg_price", "current_price", "total_value", "pnl_pct", "stop_loss", "take_profit", "ai_score", "ai_rating", "entry_date"]
                        df.to_sql("portfolio", conn, if_exists="append", index=False)
                except Exception as e:
                    logger.log(f"⚠️ Failed to migrate portfolio.csv: {e}")
                    
            cursor = conn.execute("SELECT COUNT(*) FROM all_signals")
            if cursor.fetchone()[0] == 0 and os.path.exists("all_signals.csv"):
                try:
                    df = pd.read_csv("all_signals.csv")
                    if not df.empty:
                        # Rename columns to match sqlite schema
                        df.columns = ["timestamp", "ticker", "name", "industry", "price", "technicals", "fundamentals", "signal", "ai_score", "quant_score"]
                        df.to_sql("all_signals", conn, if_exists="append", index=False)
                except Exception as e:
                    pass

    def get_portfolio(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql("SELECT ticker as Ticker, shares as Shares, avg_price as 'Avg Price', current_price as 'Current Price', total_value as 'Total Value', pnl_pct as 'PnL %', stop_loss as 'Stop Loss', take_profit as 'Take Profit', ai_score as 'AI Score', ai_rating as 'AI Rating', entry_date as 'Entry Date' FROM portfolio", conn)
            for c in self.cols:
                if c not in df.columns: df[c] = "N/A"
            return df
        except:
            return pd.DataFrame(columns=self.cols)

    def add_position(self, ticker, shares, price, sl=0, tp=0, score=0, rating="HOLD"):
        ticker = ticker.upper()
        now = datetime.now().strftime("%Y-%m-%d")
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT shares, avg_price FROM portfolio WHERE ticker = ?", (ticker,))
            row = cursor.fetchone()
            
            if row:
                old_shares, old_avg = row
                new_shares = old_shares + shares
                new_avg = ((old_shares * old_avg) + (shares * price)) / new_shares
                total_val = new_shares * price
                
                conn.execute('''UPDATE portfolio 
                                SET shares=?, avg_price=?, current_price=?, total_value=?, 
                                    stop_loss=?, take_profit=?, ai_score=?, ai_rating=?
                                WHERE ticker=?''',
                             (new_shares, new_avg, price, total_val, sl, tp, score, rating, ticker))
            else:
                total_val = shares * price
                conn.execute('''INSERT INTO portfolio 
                                (ticker, shares, avg_price, current_price, total_value, pnl_pct, 
                                 stop_loss, take_profit, ai_score, ai_rating, entry_date)
                                VALUES (?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?)''',
                             (ticker, shares, price, price, total_val, sl, tp, score, rating, now))

    def refresh_prices(self):
        df = self.get_portfolio()
        if df.empty: return df
        
        tickers = df['Ticker'].tolist()
        try:
            data = yf.download(tickers, period="1d", group_by='ticker', threads=True, progress=False)
            with sqlite3.connect(self.db_path) as conn:
                for ticker in tickers:
                    try:
                        curr_price = data[ticker]['Close'].iloc[-1] if len(tickers) > 1 else data['Close'].iloc[-1]
                        curr_price = round(float(curr_price), 2)
                        
                        cursor = conn.execute("SELECT avg_price, shares, stop_loss, take_profit FROM portfolio WHERE ticker = ?", (ticker,))
                        row = cursor.fetchone()
                        if not row: continue
                        avg_p, shares, sl, tp = row
                        
                        pnl_pct = round(((curr_price - avg_p) / avg_p) * 100, 2)
                        total_val = round(shares * curr_price, 2)
                        
                        conn.execute('''UPDATE portfolio 
                                        SET current_price=?, pnl_pct=?, total_value=? 
                                        WHERE ticker=?''', (curr_price, pnl_pct, total_val, ticker))
                        
                        # Alert Check
                        if sl > 0 and curr_price <= sl:
                            logger.log(f"🛑 STOP LOSS HIT: {ticker} at ${curr_price} (Target: ${sl})", level="WARNING")
                            gatekeeper.send_notification(f"🛑 ALERT: Stop Loss hit for {ticker} at ${curr_price}. Position is underwater.")
                        elif tp > 0 and curr_price >= tp:
                            logger.log(f"🎯 TAKE PROFIT HIT: {ticker} at ${curr_price} (Target: ${tp})", level="INFO")
                            gatekeeper.send_notification(f"🎯 ALERT: Take Profit hit for {ticker} at ${curr_price}. Time to harvest gains?")
                    except Exception as e:
                        logger.log(f"⚠️ Price refresh skipped for {ticker}: {e}", level="WARNING")
                        continue
        except Exception as e:
            logger.log(f"⚠️ Portfolio Refresh Error: {e}", level="ERROR")
        return self.get_portfolio()

    def remove_position(self, ticker):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker.upper(),))

    def is_in_portfolio(self, ticker):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT 1 FROM portfolio WHERE ticker = ?", (ticker.upper(),))
            return cursor.fetchone() is not None

    def get_buy_signals(self):
        """Returns the full history of all analyzed stocks from all_signals DB table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql('''SELECT timestamp as Timestamp, ticker as Ticker, name as Name, 
                                           industry as Industry, price as Price, technicals as Technicals, 
                                           fundamentals as Fundamentals, signal as Signal, 
                                           ai_score as "AI Score", quant_score as "Quant Score" 
                                    FROM all_signals ORDER BY timestamp DESC LIMIT 100''', conn)
                return df
        except:
            return pd.DataFrame(columns=["Timestamp", "Ticker", "Name", "Industry", "Price", "Technicals", "Fundamentals", "Signal", "AI Score", "Quant Score"])

    def log_analysis_result(self, ticker, name, industry, price, techs, funds, signal, ai_score, quant_score):
        """Records every single analysis into a persistent DB log."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        tech_str = f"{techs['trend']} Trend, RSI {techs['rsi']} ({techs['semantic_rsi']})"
        fund_str = f"P/E: {funds['pe']}, Growth: {funds['growth']}"
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT INTO all_signals 
                            (timestamp, ticker, name, industry, price, technicals, fundamentals, signal, ai_score, quant_score)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                         (now, ticker.upper(), name, industry, price, tech_str, fund_str, signal, ai_score, quant_score))

# Initialize Modules
portfolio_mgr = PortfolioManager()
gatekeeper = TradeGatekeeper(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
executor = ShadowExecutor()
alpaca_api = LiveAlpacaExecutor()

def _live_equity(fallback: float = 10_000.0) -> float:
    """Returns the live Alpaca account equity, or the fallback if disconnected."""
    try:
        acct = alpaca_api.get_account_summary()
        if acct.get("connected") and acct.get("equity", 0) > 0:
            return float(acct["equity"])
    except Exception as e:
        logger.log(f"⚠️ _live_equity fell back to ${fallback}: {e}", level="WARNING")
    return fallback


# ─── Ad-hoc Research Cycle (Phase 2 preview, button-triggered) ───────────────
# Single-run guard so multiple button clicks don't spawn overlapping cycles.
_research_lock = threading.Lock()
_research_in_progress = {"active": False, "started_at": None, "progress": 0, "total": 0}


def _research_status_text() -> str:
    """
    Renders the research-cycle status line for the top-right panel.
    Live state if a cycle is mid-run; otherwise the last-completion timestamp
    from watchlist_today (which is the canonical 'cycle finished' marker).
    """
    if _research_in_progress["active"]:
        pct = (_research_in_progress["progress"] / _research_in_progress["total"] * 100
               if _research_in_progress["total"] else 0)
        elapsed_s = int((datetime.now() - _research_in_progress['started_at']).total_seconds())
        return (f"🔄 **Running** — {_research_in_progress['progress']}/"
                f"{_research_in_progress['total']} ({pct:.0f}%) · {elapsed_s // 60}m elapsed")

    meta = watchlist_today.get_metadata()
    if not meta["generated_at"]:
        return "_No research cycle yet — click to run, or wait for overnight cron._"
    finished = datetime.fromtimestamp(meta["generated_at"]).strftime("%Y-%m-%d %H:%M")
    age_h = (time.time() - meta["generated_at"]) / 3600
    freshness = "🟢" if age_h < 12 else ("🟡" if age_h < 24 else "🔴")
    return (f"{freshness} Last cycle: **{finished}** "
            f"({age_h:.1f}h ago) · {meta['count']} candidates")


def _build_watchlist_rows(analyses: list, batch_funds: dict) -> list:
    """
    Turns the per-ticker analysis dicts into ranked watchlist rows for
    watchlist_today. Ranking: highest quant_score first, ties broken by
    AI score then by today's change %.
    """
    enriched = []
    for a in analyses:
        if not a:
            continue
        ticker = a.get("Ticker")
        funds = batch_funds.get(ticker, {}) if batch_funds else {}
        tech_summary = (
            f"{a.get('Trend', 'N/A')} | RSI {a.get('Analysis','')[:0]}"
            f"Price ${a.get('Price', 0)} | Change {a.get('Change %', 0)}% "
            f"| 20D {a.get('20D Gain %', 0)}%"
        )
        fund_summary = f"P/E: {funds.get('pe', 'N/A')} | Growth: {funds.get('growth', 'N/A')}"
        # The AI score isn't returned directly by analyze_single_ticker_data; pull from cache.
        cached_ai = ai_score_cache.get(ticker) or {}
        enriched.append({
            "ticker":         ticker,
            "quant_score":    a.get("Score", 0),
            "ai_score":       cached_ai.get("score"),
            "recommendation": a.get("Rating", "HOLD"),
            "price":          a.get("Price", 0),
            "change_pct":     a.get("Change %", 0),
            "technicals_summary":   tech_summary,
            "fundamentals_summary": fund_summary,
        })

    # Defensive coercion: any field from analyze_single_ticker_data or the AI
    # cache could in principle be a string (e.g., "N/A" or a stringified number
    # from a non-conforming LLM output). Force everything to float for the sort
    # key with try/except so one bad row can't tank a 25-min cycle.
    def _num(v) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    enriched.sort(
        key=lambda r: (
            -_num(r.get("quant_score")),
            -_num(r.get("ai_score")),
            -_num(r.get("change_pct")),
        )
    )
    for idx, row in enumerate(enriched, start=1):
        row["rank"] = idx
    return enriched


def run_research_cycle(top_n: int = 150) -> str:
    """
    Ad-hoc Tier-1 cache population. Pulls Polygon snapshot, filters by liquidity
    and momentum, runs analyze_single_ticker_data on top N. Both
    fundamentals_cache and ai_score_cache get written through.

    Designed to be called from a background thread (e.g. the Refresh Cache
    button). Notifies via Telegram at 25%/50%/75%/100% and on completion.
    Returns a short status string for the immediate caller.
    """
    with _research_lock:
        if _research_in_progress["active"]:
            return "⚠️ Research cycle already running — wait for it to finish or restart the app."
        _research_in_progress.update(
            active=True, started_at=datetime.now(), progress=0, total=top_n
        )

    try:
        logger.log(f"🔬 [Research Cycle] Starting ad-hoc Tier-1 refresh (top {top_n})...")
        target_date = get_last_trading_day()
        date_20d_ago = get_trading_day_n_ago(20)

        rate_limiter.wait()
        url_curr = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
                    f"{target_date}?adjusted=true&apiKey={POLYGON_API_KEY}")
        rate_limiter.wait()
        url_hist = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
                    f"{date_20d_ago}?adjusted=true&apiKey={POLYGON_API_KEY}")

        resp_c = requests.get(url_curr, timeout=20).json()
        resp_h = requests.get(url_hist, timeout=20).json()
        if "results" not in resp_c:
            raise RuntimeError("Polygon snapshot returned no results")

        hist_prices = {item['T']: item['c'] for item in resp_h.get("results", [])}

        # Liquidity + momentum filter (looser than agent loop to populate broader cache).
        candidates = []
        for item in resp_c["results"]:
            price = item.get('c', 0)
            vol = item.get('v', 0)
            if price < 2 or vol < 500_000:
                continue
            p20 = hist_prices.get(item['T'])
            if not p20 or price <= p20:
                continue
            candidates.append(item)

        candidates.sort(key=lambda x: x.get('v', 0), reverse=True)
        top_picks = candidates[:top_n]
        _research_in_progress["total"] = len(top_picks)
        logger.log(f"🔬 [Research Cycle] {len(top_picks)} tickers selected for cache refresh.")

        gatekeeper.send_notification(
            f"🔬 Research cycle started — {len(top_picks)} tickers in scope. "
            f"ETA ~{len(top_picks) * 8 // 60} min."
        )

        # Pre-fetch fundamentals + history in batches (already cached helpers).
        tickers = [p['T'] for p in top_picks]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        batch_funds = loop.run_until_complete(get_fundamentals_batch_async(tickers, logger))
        batch_history = loop.run_until_complete(get_history_batch_async(tickers, logger, skip_stale=True))

        # Collect each analysis result so we can write a ranked watchlist at the end.
        analysis_results = []

        # Analyze each (writes through fundamentals_cache + ai_score_cache via
        # analyze_single_ticker_data's cache hooks).
        progress_milestones = {int(len(top_picks) * f): int(f * 100)
                               for f in (0.25, 0.50, 0.75)}
        for i, item in enumerate(top_picks, start=1):
            ticker = item['T']
            history = batch_history.get(ticker, [])
            # Inject today's bar so technicals reflect the current session.
            # Defensive: Polygon raw bars have `t` as int ms; normalize before comparing
            # to target_date (string). Should not be needed after get_full_history fix,
            # but guards against any other caller leaking raw Polygon shapes.
            if history:
                last_t = history[-1].get("t", "")
                if isinstance(last_t, (int, float)):
                    last_t = date.fromtimestamp(last_t / 1000).strftime("%Y-%m-%d")
                if last_t < target_date:
                    history = history + [{
                        "o": item.get("o"), "h": item.get("h"),
                        "l": item.get("l"), "c": item.get("c"),
                        "v": item.get("v"), "t": target_date,
                    }]
            try:
                analysis = analyze_single_ticker_data(
                    ticker,
                    pre_fetched_data=item,
                    pre_fetched_funds=batch_funds.get(ticker),
                    pre_fetched_history=history,
                )
                if analysis:
                    analysis_results.append(analysis)
            except Exception as e:
                logger.log(f"⚠️ [Research Cycle] {ticker} failed: {e}", level="WARNING")
            _research_in_progress["progress"] = i

            if i in progress_milestones:
                pct = progress_milestones[i]
                gatekeeper.send_notification(f"🔬 Research cycle: {pct}% ({i}/{len(top_picks)})")

        # Write today's ranked watchlist for the live reactive agent (Phase 3) to consume.
        watchlist_rows = _build_watchlist_rows(analysis_results, batch_funds)
        n_written = watchlist_today.replace_all(watchlist_rows)

        elapsed_min = (datetime.now() - _research_in_progress['started_at']).seconds // 60
        n_buys = sum(1 for r in watchlist_rows if r["recommendation"] == "BUY")
        msg = (f"✅ Research cycle complete: {len(top_picks)} tickers analyzed in {elapsed_min} min. "
               f"Watchlist: {n_written} ranked candidates ({n_buys} BUY). "
               f"Fundamentals + AI scores cached.")
        logger.log(msg)
        gatekeeper.send_notification(msg)
        return msg
    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc()
        err = f"❌ Research cycle error: {e}"
        logger.log(err, level="ERROR")
        logger.log(f"   Traceback:\n{tb_str}", level="ERROR")
        try:
            gatekeeper.send_notification(f"{err}\n(see activity.log for traceback)")
        except Exception:
            pass
        return err
    finally:
        _research_in_progress.update(active=False, started_at=None, progress=0, total=0)


def trigger_research_cycle_async() -> str:
    """Button handler: dispatch research cycle in a daemon thread and return immediately."""
    if _research_in_progress["active"]:
        return "⚠️ Research cycle is already running."
    threading.Thread(
        target=run_research_cycle,
        name="research-cycle",
        daemon=True,
    ).start()
    return ("🔬 Research cycle started in background. "
            "ETA ~15-25 min. Telegram progress at 25/50/75/100%.")


# ─── Watchlist Today UI helpers ──────────────────────────────────────────────

WATCHLIST_COLS = ["Rank", "Ticker", "Rating", "Quant", "AI", "Price", "Chg%",
                  "Technicals", "Fundamentals"]


def _watchlist_rows_to_df(rows: list) -> pd.DataFrame:
    """Convert watchlist_today rows to a Gradio-friendly DataFrame."""
    if not rows:
        return pd.DataFrame(columns=WATCHLIST_COLS)
    return pd.DataFrame([
        {
            "Rank":         r["rank"],
            "Ticker":       r["ticker"],
            "Rating":       r["recommendation"],
            "Quant":        r["quant_score"],
            "AI":           r["ai_score"] if r["ai_score"] is not None else "—",
            "Price":        f"${r['price']:.2f}",
            "Chg%":         f"{r['change_pct']:+.2f}%",
            "Technicals":   r["technicals_summary"],
            "Fundamentals": r["fundamentals_summary"],
        }
        for r in rows
    ])


def render_watchlist_header() -> str:
    """Markdown caption above the BUY watchlist section."""
    meta = watchlist_today.get_metadata()
    if not meta["generated_at"]:
        return (
            "### 🎯 Today's Watchlist\n"
            "<div class='nuroq-empty-state'>"
            "<h3>📭 No watchlist yet</h3>"
            "<p>The watchlist is generated by the overnight research cycle "
            "(~9 min). It seeds the LiveAgent with today's ranked candidates "
            "and the BUY/HOLD crossings the agent watches for during the session.</p>"
            "<p><b>To populate:</b> tick the consent box and click "
            "<b>🔬 Run Research Cycle</b> in the top-right of the dashboard. "
            "Or wait for the overnight cron (see <code>SCHEDULING.md</code>).</p>"
            "</div>"
        )
    finished = datetime.fromtimestamp(meta["generated_at"]).strftime("%Y-%m-%d %H:%M")
    age_h = (time.time() - meta["generated_at"]) / 3600
    age_label = f"{age_h:.1f}h ago" if age_h < 48 else f"{age_h/24:.1f}d ago"
    freshness = "🟢" if age_h < 12 else ("🟡" if age_h < 24 else "🔴")
    all_rows = watchlist_today.get_all()
    n_buy = sum(1 for r in all_rows if r["recommendation"] == "BUY")
    n_hold = sum(1 for r in all_rows if r["recommendation"] == "HOLD")
    n_sell = sum(1 for r in all_rows if r["recommendation"] == "SELL")
    breakdown = []
    if n_buy:  breakdown.append(f"**{n_buy} BUY**")
    if n_hold: breakdown.append(f"{n_hold} HOLD")
    if n_sell: breakdown.append(f"{n_sell} SELL")
    return (f"### 🎯 Today's Watchlist — {' · '.join(breakdown) or 'empty'}\n"
            f"{freshness} Generated **{finished}** ({age_label}) by overnight research cycle "
            f"(or the manual 🔬 button). LiveAgent subscribes to ALL of these for crossing detection.")


def render_watchlist_buys() -> pd.DataFrame:
    """BUY-only slice for the primary table."""
    rows = [r for r in watchlist_today.get_all() if r["recommendation"] == "BUY"]
    return _watchlist_rows_to_df(rows)


def render_watchlist_holds() -> pd.DataFrame:
    """HOLD slice (and any SELLs in the watchlist) for the collapsed section."""
    rows = [r for r in watchlist_today.get_all() if r["recommendation"] != "BUY"]
    return _watchlist_rows_to_df(rows)


def refresh_watchlist_view():
    """Click handler for the Watchlist tab's refresh button. Returns header + 2 tables."""
    return render_watchlist_header(), render_watchlist_buys(), render_watchlist_holds()


# ─── Phase 5: Health observability ────────────────────────────────────────────

def _age_traffic_light(age_h: float) -> str:
    if age_h < 12:   return "🟢"
    if age_h < 24:   return "🟡"
    return "🔴"


def render_health_snapshot() -> str:
    """Markdown table of system health for the Health tab."""
    rows = []
    now = time.time()

    # ─── Research cycle / watchlist freshness ───
    meta = watchlist_today.get_metadata()
    if meta["generated_at"]:
        age_h = (now - meta["generated_at"]) / 3600
        ts = datetime.fromtimestamp(meta["generated_at"]).strftime("%Y-%m-%d %H:%M")
        rows.append((_age_traffic_light(age_h),
                     "Research cycle",
                     f"Last run: **{ts}** ({age_h:.1f}h ago) · {meta['count']} candidates"))
    else:
        rows.append(("🔴", "Research cycle",
                     "Never run. Click 🔬 Run Research Cycle or wait for cron."))

    # ─── LiveAgent ───
    try:
        agent_status = agent.status()
        if agent_status["running"]:
            started = (datetime.fromisoformat(agent_status["started_at"]).strftime("%H:%M:%S")
                       if agent_status.get("started_at") else "?")
            last_bar = (datetime.fromtimestamp(agent_status["latest_bar_ts"]).strftime("%H:%M:%S")
                        if agent_status.get("latest_bar_ts") else "—")
            rows.append(("🟢", "LiveAgent",
                         f"Running since {started} · {agent_status['subscribed_tickers']} tickers · "
                         f"{agent_status['bars_processed']:,} bars · last bar {last_bar} · "
                         f"BUYs {agent_status['buys_fired_today']}/{agent_status['buys_cap']}"))
        else:
            rows.append(("⚪", "LiveAgent", "Stopped — click ▶️ START AGENT in the Agent tab."))
    except Exception as e:
        rows.append(("🔴", "LiveAgent", f"Status error: {e}"))

    # ─── NewsPoller ───
    if news_poller is not None:
        ns = news_poller.status()
        last = ns.get("last_cycle_at")
        if last:
            age_min = (now - last) / 60
            last_str = datetime.fromtimestamp(last).strftime("%H:%M:%S")
            light = "🟢" if age_min < 35 else "🟡"
            rows.append((light, "NewsPoller",
                         f"Running · last cycle {last_str} ({age_min:.0f} min ago) · "
                         f"{ns['cycles_completed']} cycles · "
                         f"{ns['headlines_ingested_total']} headlines total"))
        else:
            rows.append(("🟡", "NewsPoller",
                         f"Running · no cycles completed yet (interval {ns['interval_seconds']}s)"))
    else:
        rows.append(("⚪", "NewsPoller",
                     "Disabled (NUROQ_BACKGROUND_SERVICES=0 or dashboard not booted with it)."))

    # ─── LLM rescore queue (Phase 4b) ───
    if llm_rescore_queue is not None:
        qs = llm_rescore_queue.status()
        rows.append(("🟢" if qs["running"] else "⚪",
                     "LLM Rescore Queue",
                     f"depth={qs['queue_depth']} · processed={qs['total_processed']} · "
                     f"dedups={qs['total_dedup_drops']} · errors={qs['total_errors']}"))
    else:
        rows.append(("⚪", "LLM Rescore Queue", "Disabled."))

    # ─── Alpaca connection ───
    try:
        acct = alpaca_api.get_account_summary()
        if acct.get("connected"):
            rows.append(("🟢", "Alpaca",
                         f"{acct['status']} · equity ${acct['equity']:,.2f} · "
                         f"buying power ${acct['buying_power']:,.2f}"))
        else:
            rows.append(("🔴", "Alpaca", "Not connected. Check ALPACA_API_KEY / SECRET in .env."))
    except Exception as e:
        rows.append(("🔴", "Alpaca", f"Error: {e}"))

    # ─── SQLite cache sizes ───
    with sqlite3.connect("nuroq.db") as conn:
        def _count(table):
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                return int(row[0]) if row else 0
            except Exception:
                return 0
        ph = _count("price_history")
        fc = _count("fundamentals_cache")
        ac = _count("ai_scores_cache")
        nc = _count("news_cache")
        lt = _count("live_triggers")
        sg = _count("all_signals")

    rows.append(("🟢", "SQLite caches",
                 f"price_history: {ph:,} bars · fundamentals_cache: {fc:,} · "
                 f"ai_scores_cache: {ac:,} · news_cache: {nc:,} · live_triggers: {lt:,} · "
                 f"all_signals: {sg:,}"))

    # Build markdown table
    out = ["### 🩺 System Health\n", "| | Component | Status |", "|:---:|:---|:---|"]
    for emoji, name, status in rows:
        # Sanitize pipes in status text since we're embedding into a markdown table
        safe = status.replace("|", "/")
        out.append(f"| {emoji} | **{name}** | {safe} |")
    out.append(f"\n_Refreshed at {datetime.now().strftime('%H:%M:%S')}_")
    return "\n".join(out)


# ─── Phase A UI redesign: status pills + Today landing tab ───────────────────
#
# Compact "system status at a glance" pill row that lives in the top bar so the
# user never has to dig into the Health tab to know whether the agent / stream
# / Telegram bot / Alpaca connection are alive. Plus a Today landing tab that
# answers "what's happening right now" without forcing tab-hopping.

def _pill(state: str, label: str) -> str:
    """state ∈ {'ok','warn','err','off'} → colored dot + label."""
    dot = {"ok": "🟢", "warn": "🟡", "err": "🔴", "off": "⚪"}.get(state, "⚪")
    return f"{dot} **{label}**"


def render_status_pills() -> str:
    """One-line system-status strip for the persistent top bar."""
    pills = []

    # Agent
    try:
        a = agent.status()
        pills.append(_pill("ok" if a.get("running") else "off", "Agent"))
    except Exception:
        pills.append(_pill("err", "Agent"))

    # Stream (WebSocket)
    try:
        streamer_running = bool(getattr(agent, "streamer", None)
                                and agent.streamer.is_running)
        last_bar = (agent.status().get("latest_bar_ts") if streamer_running else None)
        if not streamer_running:
            pills.append(_pill("off", "Stream"))
        elif last_bar and (time.time() - last_bar) < 300:
            pills.append(_pill("ok", "Stream"))
        else:
            # Streamer subscribed but no recent bars — normal outside market hours.
            pills.append(_pill("warn", "Stream"))
    except Exception:
        pills.append(_pill("err", "Stream"))

    # Telegram bot
    try:
        bot_alive = bool(gatekeeper.app is not None and gatekeeper.loop is not None)
        pills.append(_pill("ok" if bot_alive else "off", "Telegram"))
    except Exception:
        pills.append(_pill("err", "Telegram"))

    # Alpaca
    try:
        acct = alpaca_api.get_account_summary()
        pills.append(_pill("ok" if acct.get("connected") else "err", "Alpaca"))
    except Exception:
        pills.append(_pill("err", "Alpaca"))

    return "&nbsp;&nbsp;·&nbsp;&nbsp;".join(pills) + \
        f"  &nbsp;&nbsp;&nbsp;<sub>{datetime.now().strftime('%H:%M:%S')}</sub>"


def render_today_hero() -> str:
    """Big P&L number + equity + 30-day return for the Today landing tab."""
    try:
        acct = alpaca_api.get_account_summary()
    except Exception as e:
        return f"### 📈 Today\n_Account data unavailable: {e}_"
    if not acct.get("connected"):
        return ("### 📈 Today\n_Alpaca not connected — set "
                "`ALPACA_API_KEY` / `ALPACA_SECRET_KEY` in `.env`._")

    pl = acct["todays_pl"]
    pl_pct = acct["todays_pl_pct"]
    pl_sign = "+" if pl >= 0 else ""
    pl_color = "#10b981" if pl >= 0 else "#ef4444"

    hist = alpaca_api.get_portfolio_history(period_days=30)
    if hist.get("connected") and hist["equity_series"]:
        r = hist["return_pct"]
        r_sign = "+" if r >= 0 else ""
        r_color = "#10b981" if r >= 0 else "#ef4444"
        thirty = f"<span style='color:{r_color}'>30-day <b>{r_sign}{r:.2f}%</b></span>"
    else:
        thirty = "<span style='opacity:0.5'>30-day —</span>"

    return (
        f"<div class='nuroq-hero-pl' style='color: {pl_color};'>"
        f"{pl_sign}${pl:,.2f} "
        f"<span style='font-size: 0.45em; opacity: 0.7; font-weight: 500;'>"
        f"({pl_sign}{pl_pct:.2f}% today)</span>"
        f"</div>"
        f"<div style='font-size: 12px; opacity: 0.85;'>"
        f"<b>Equity</b> ${acct['equity']:,.2f} &nbsp;·&nbsp; "
        f"<b>Cash</b> ${acct['cash']:,.2f} &nbsp;·&nbsp; "
        f"<b>BP</b> ${acct['buying_power']:,.2f} &nbsp;·&nbsp; "
        f"{thirty} &nbsp;·&nbsp; "
        f"<span style='opacity:0.6'>{acct['status']}</span>"
        f"</div>"
    )


def render_today_cards() -> str:
    """Three side-by-side summary cards: Watchlist, Agent, News."""
    # Watchlist breakdown
    all_wl = watchlist_today.get_all()
    n_buy = sum(1 for r in all_wl if r["recommendation"] == "BUY")
    n_hold = sum(1 for r in all_wl if r["recommendation"] == "HOLD")
    meta = watchlist_today.get_metadata()
    if meta["generated_at"]:
        age_h = (time.time() - meta["generated_at"]) / 3600
        wl_age = f"{age_h:.1f}h ago" if age_h < 24 else f"{age_h/24:.1f}d ago"
    else:
        wl_age = "never run"

    # Agent
    try:
        a = agent.status()
        if a.get("running"):
            agent_line = (f"🟢 **Running** · {a.get('subscribed_tickers',0)} tickers · "
                          f"BUYs {a.get('buys_fired_today',0)}/{a.get('buys_cap',0)} · "
                          f"SELLs {a.get('sells_fired_today',0)}")
        else:
            agent_line = "⚪ **Stopped** — open the Agent tab to start"
    except Exception as e:
        agent_line = f"🔴 Status error: {e}"

    # News shocks today
    try:
        with sqlite3.connect("nuroq.db") as conn:
            day_ago = time.time() - 86400
            rows = conn.execute(
                "SELECT classification, COUNT(*) FROM news_cache "
                "WHERE ingested_at > ? AND classification != 'NEUTRAL' "
                "GROUP BY classification", (day_ago,)
            ).fetchall()
        if rows:
            news_line = " · ".join(f"{c.replace('_',' ')}: **{n}**" for c, n in rows)
        else:
            news_line = "_No non-neutral classifications in last 24h_"
    except Exception:
        news_line = "_News cache unavailable_"

    return (
        "| 🎯 Watchlist | 🤖 Agent | 📰 News (24h) |\n"
        "|:---|:---|:---|\n"
        f"| **{n_buy}** BUYs · **{n_hold}** HOLDs<br/>"
        f"<sub>generated {wl_age}</sub> | "
        f"{agent_line} | "
        f"{news_line} |"
    )


def render_channel_bar() -> str:
    """
    Chronological feed of recent agent decisions + news shocks (last 24h, cap 50).
    Trade Ideas calls this a Channel Bar — the single most useful 'what just
    happened' surface for an event-driven AI quant tool.
    """
    events = []
    day_ago = time.time() - 86400
    try:
        with sqlite3.connect("nuroq.db") as conn:
            # Live triggers
            for ts, ticker, direction, sb, sa, price, action, notes in conn.execute(
                "SELECT ts, ticker, direction, score_before, score_after, price, action, notes "
                "FROM live_triggers WHERE ts > ? ORDER BY ts DESC LIMIT 30", (day_ago,)
            ).fetchall():
                emoji = {
                    "FIRED_BUY": "🎯", "FIRED_SELL": "📉",
                    "SUPPRESSED_CAP": "🛑", "SUPPRESSED_HELD": "⏭️",
                    "SUPPRESSED_NEWS": "🚫",
                }.get(action, "•")
                desc = f"{direction} crossing — {action.replace('_',' ').lower()}"
                if sb is not None and sa is not None:
                    desc += f" ({sb}→{sa})"
                if price:
                    desc += f" @ ${price:.2f}"
                events.append((ts, emoji, ticker, desc))

            # News shocks
            for ticker, headline, classification, ingested_at in conn.execute(
                "SELECT ticker, headline, classification, ingested_at FROM news_cache "
                "WHERE ingested_at > ? AND classification != 'NEUTRAL' "
                "ORDER BY ingested_at DESC LIMIT 20", (day_ago,)
            ).fetchall():
                emoji = {
                    "NEGATIVE_BLOCK": "🛑", "NEGATIVE_WARNING": "⚠️",
                    "POSITIVE_BOOST": "📈",
                }.get(classification, "📰")
                short = headline[:90] + ("…" if len(headline) > 90 else "")
                events.append((ingested_at, emoji, ticker,
                               f"{classification.replace('_',' ')}: {short}"))
    except Exception as e:
        return f"### 🛰️ Live Feed (24h)\n_Feed unavailable: {e}_"

    if not events:
        return ("### 🛰️ Live Feed (24h)\n"
                "_No agent decisions or news shocks in the last 24 hours. "
                "The feed will populate during market hours as the live agent "
                "fires crossings and the news poller ingests headlines._")

    events.sort(key=lambda e: e[0], reverse=True)
    events = events[:50]

    lines = ["### 🛰️ Live Feed (24h)", ""]
    for ts, emoji, ticker, desc in events:
        when = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"- `{when}` {emoji} **{ticker}** — {desc}")
    return "\n".join(lines)


def refresh_today_view():
    """Single-click refresh for all five Today surfaces + status pills."""
    return (render_status_pills(), render_today_hero(),
            render_today_cards(), render_next_actions(), render_channel_bar())


def render_next_actions() -> str:
    """
    Smart 'what to do next' card on the Today tab. Analyzes current system
    state and surfaces 0–N actionable suggestions. The goal is to convert
    'here's a dashboard, figure it out' into 'here's what you should do now'.
    """
    suggestions = []
    now = time.time()

    # Research cycle freshness
    meta = watchlist_today.get_metadata()
    if not meta["generated_at"]:
        suggestions.append(
            ("🔴", "**No watchlist yet.** Tick the consent box at the top and "
             "click **🔬 Run Research Cycle** to generate today's ranked candidates "
             "(~9 min, GPU-bound). The live agent needs this to know what to watch.")
        )
    else:
        age_h = (now - meta["generated_at"]) / 3600
        if age_h >= 24:
            suggestions.append(
                ("🟡", f"**Research cycle is {age_h/24:.1f} days old.** Re-run the "
                 "research cycle to refresh today's watchlist and AI scores.")
            )

    # Watchlist BUYs but agent stopped
    try:
        all_wl = watchlist_today.get_all()
        n_buy = sum(1 for r in all_wl if r["recommendation"] == "BUY")
        agent_running = bool(agent.status().get("running"))
        if n_buy > 0 and not agent_running:
            suggestions.append(
                ("🟡", f"**{n_buy} BUY signals on the watchlist but the agent is stopped.** "
                 "Open the **🤖 Agent** tab and click **▶️ START AGENT** so it can "
                 "fire approval prompts when intraday crossings occur.")
            )
    except Exception:
        pass

    # Recent NEGATIVE_BLOCK news on a held position
    try:
        held = []
        df = portfolio_mgr.get_portfolio()
        if not df.empty:
            held = [str(t).upper() for t in df["Ticker"].tolist()]
        if held:
            with sqlite3.connect("nuroq.db") as conn:
                day_ago = now - 86400
                placeholders = ",".join("?" * len(held))
                rows = conn.execute(
                    f"SELECT ticker, headline FROM news_cache "
                    f"WHERE ingested_at > ? AND classification = 'NEGATIVE_BLOCK' "
                    f"AND ticker IN ({placeholders}) ORDER BY ingested_at DESC LIMIT 3",
                    [day_ago, *held],
                ).fetchall()
            for tk, hl in rows:
                short = hl[:120] + ("…" if len(hl) > 120 else "")
                suggestions.append(
                    ("🔴", f"**Block-level negative news on held position {tk}.** "
                     f"_{short}_ — review the position; consider closing.")
                )
    except Exception:
        pass

    # Alpaca disconnected
    try:
        acct = alpaca_api.get_account_summary()
        if not acct.get("connected"):
            suggestions.append(
                ("🔴", "**Alpaca not connected.** Check `ALPACA_API_KEY` and "
                 "`ALPACA_SECRET_KEY` in `.env` — without it, no orders can route.")
            )
    except Exception as e:
        suggestions.append(("🔴", f"**Alpaca status check failed:** {e}"))

    # Pending Alpaca orders awaiting market open
    try:
        orders = alpaca_api.get_open_orders(limit=25)
        if orders:
            suggestions.append(
                ("🟢", f"**{len(orders)} pending order(s)** queued to route at market open. "
                 "View under the Alpaca account panel above.")
            )
    except Exception:
        pass

    # All clear
    if not suggestions:
        return ("### 💡 Next Actions\n"
                "<div class='nuroq-empty-state' style='padding: 20px;'>"
                "<h3>✅ All systems healthy</h3>"
                "<p>No outstanding actions. Watch the Live Feed below for incoming "
                "agent decisions and news shocks.</p>"
                "</div>")

    lines = ["### 💡 Next Actions", ""]
    for emoji, text in suggestions:
        lines.append(f"- {emoji} {text}")
    return "\n".join(lines)


# ─── Phase A/B UI redesign: drill-down + recent symbols + QT prefill ─────────

def _ticker_from_row(df, evt) -> Optional[str]:
    """
    Extract the ticker string from a Gradio DataFrame select event.
    Tables across the dashboard use different column orderings — this finds
    'Ticker' or 'Symbol' by name and returns None on any failure rather than
    crashing the whole drill-down flow.
    """
    try:
        if df is None or len(df) == 0:
            return None
        row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
        row = df.iloc[int(row_idx)]
        for key in ("Ticker", "Symbol", "ticker", "symbol"):
            if key in row.index:
                t = str(row[key]).strip().upper()
                if t and t not in ("NAN", "NONE", ""):
                    return t
        # Fallback: first string-looking cell
        for v in row.values:
            s = str(v).strip().upper()
            if 1 <= len(s) <= 6 and s.isalpha():
                return s
    except Exception:
        return None
    return None


def drill_to_analyze(df, evt: gr.SelectData):
    """
    Generic table-row → Analyze tab handler. Returns (ticker, tabs_update).
    Used by Watchlist (BUY + HOLD), Portfolio, Signals, Scanner, Agent log.
    The `evt: gr.SelectData` annotation tells Gradio to auto-inject the
    SelectData object — it's not declared in inputs=.
    """
    ticker = _ticker_from_row(df, evt)
    if not ticker:
        return gr.update(), gr.update()
    return ticker, gr.Tabs(selected="analyze")


def push_recent_symbol(ticker: str, recent: list) -> list:
    """Insert the analyzed ticker at the head of the recent list, cap at 8."""
    if not ticker:
        return recent or []
    t = ticker.strip().upper()
    if not t:
        return recent or []
    out = [x for x in (recent or []) if x != t]
    out.insert(0, t)
    return out[:8]


def analyze_and_remember(ticker: str, recent: list):
    """Wrap analyze_stock to also push the ticker into the recent-symbols list."""
    result = analyze_stock(ticker)  # (out_md, cot_md, chart, setup_md)
    new_recent = push_recent_symbol(ticker, recent)
    return (*result, new_recent, gr.update(choices=new_recent, value=None))


def recent_selected(choice: str):
    """When user picks from the recent-symbols dropdown, fill the ticker input."""
    return choice or ""


def send_to_quick_trade(ticker: str):
    """
    Pre-fill the Quick Trade bar from a freshly-priced ATR-based sizing for the
    given ticker. Returns (ticker, shares, sl, tp). Pulls a quick price from
    history_cache (no API call); falls back to no-op on missing data.
    """
    if not ticker:
        return gr.update(), gr.update(), gr.update(), gr.update()
    t = ticker.strip().upper()
    try:
        bars = history_cache.get(t, allow_stale=True) or []
        if not bars:
            return t, gr.update(), gr.update(), gr.update()
        price = float(bars[-1].get("c") or 0)
        if price <= 0:
            return t, gr.update(), gr.update(), gr.update()
        techs = calculate_technicals(bars) or {}
        atr = float(techs.get("atr") or max(price * 0.02, 0.5))
        sizing = calculate_sizing(price, atr=atr, account=_live_equity())
        return (
            t,
            int(sizing["shares"]),
            round(float(sizing["sl"]), 2),
            round(float(sizing["tp"]), 2),
        )
    except Exception as e:
        logger.log(f"⚠️ send_to_quick_trade({t}) failed: {e}", level="WARNING")
        return t, gr.update(), gr.update(), gr.update()


def run_scanner(mode: str):
    """Unified dispatcher for the merged Scanner tab (Top-20 vs Global Deep Scan)."""
    if mode == "Global Deep Scan":
        return deep_market_scan()
    return scan_market()


def confirm_research_cycle(consent: bool):
    """
    Two-gate research-cycle trigger. The checkbox is the consent gate; this
    handler runs the long (≈9 min) cycle only when consent is True, then
    auto-unchecks for next time so a stray click can't re-trigger it.
    """
    if not consent:
        return ("⚠️ Check 'I understand' first — the research cycle takes ~9 min "
                "and consumes GPU.", gr.update(value=False))
    msg = trigger_research_cycle_async()
    return msg, gr.update(value=False)


# ─── Reasoning-drift detector (anti-Gemma-hallucination guard) ───────────────
#
# Defends against the failure mode where Gemma 4B, given a generic prompt,
# generates analysis text about a more-prominent company (typically GameStop)
# while the actual ticker was something less famous (e.g., SONY). Front-loading
# the company name in the prompt mitigates this, but a runtime guard catches
# the cases that still slip through and prevents them from polluting the cache.

# Marker → likely originator-ticker. If a marker word appears in the reasoning
# but the analyzed ticker is something different AND the target ticker/company
# isn't co-mentioned, we flag as drift.
_DRIFT_MARKERS = {
    "Ryan Cohen":      "GME",
    "GameStop":        "GME",
    "iPhone":          "AAPL",
    "Tim Cook":        "AAPL",
    "Elon Musk":       "TSLA",
    "Cybertruck":      "TSLA",
    "Jensen Huang":    "NVDA",
    "Mark Zuckerberg": "META",
    "Satya Nadella":   "MSFT",
    "Sundar Pichai":   "GOOGL",
    "Andy Jassy":      "AMZN",
}


def detect_reasoning_drift(text: str, ticker: str, company_name: str = "") -> Optional[str]:
    """
    Returns the offending marker string if `text` appears to talk about a
    different company than `ticker`. Returns None when reasoning looks aligned.

    Two-stage heuristic:
      Stage 1 (TOPIC SENTENCE) — scan the first 120 chars. If a foreign-company
      marker dominates the opening and the target ticker/company is NOT in
      that opening, flag as drift regardless of later co-mentions. This catches
      the SONY → GameStop bug where the body starts "GameStop is currently
      trading..." but mentions Sony only as a passing reference later.

      Stage 2 (BODY) — for the rest of the text, co-mention is treated as a
      legitimate comparison and does not trigger.
    """
    if not text:
        return None
    tkr_upper = (ticker or "").upper()
    text_upper = text.upper()
    head = text[:120]
    head_upper = head.upper()

    # First word of company name (e.g., "Sony" from "Sony Group Corporation").
    # Skips generic words that would always match in a corpus.
    company_first = ""
    if company_name:
        for tok in company_name.split():
            if tok and tok.lower() not in ("the", "inc", "inc.", "corp", "corp.",
                                            "corporation", "company", "co", "co.",
                                            "ltd", "ltd.", "group", "plc"):
                company_first = tok
                break
    company_first_upper = company_first.upper() if company_first else ""

    def _target_in(haystack_upper: str) -> bool:
        if tkr_upper and tkr_upper in haystack_upper:
            return True
        if company_first_upper and company_first_upper in haystack_upper:
            return True
        return False

    # ── Stage 1: topic-sentence check ─────────────────────────────────────────
    for marker, origin_ticker in _DRIFT_MARKERS.items():
        if origin_ticker == tkr_upper:
            continue
        if marker not in head and marker.upper() not in head_upper:
            continue
        if not _target_in(head_upper):
            return marker  # foreign marker dominates the opening — drift

    # ── Stage 2: body check (co-mention treated as legitimate comparison) ─────
    for marker, origin_ticker in _DRIFT_MARKERS.items():
        if origin_ticker == tkr_upper:
            continue
        if marker not in text and marker.upper() not in text_upper:
            continue
        if _target_in(text_upper):
            continue
        return marker

    return None


def render_alpaca_panel() -> str:
    """
    One-line dense Alpaca strip — shown above the tabs on every page so the
    user always knows account state. Detailed pending-orders table only renders
    when there are pending orders (no empty-state clutter). Equity/P&L is also
    shown more prominently on the Today hero; this strip is the always-visible
    cross-tab anchor.
    """
    try:
        acct = alpaca_api.get_account_summary()
    except Exception as e:
        return f"<div style='font-size:12px;opacity:0.75;'>💰 Alpaca: ⚠️ {e}</div>"

    if not acct.get("connected"):
        return ("<div style='font-size:12px;opacity:0.75;'>"
                "💰 Alpaca: <b>not connected</b> — check <code>ALPACA_API_KEY</code> / "
                "<code>ALPACA_SECRET_KEY</code> in <code>.env</code>.</div>")

    pl = acct["todays_pl"]
    pl_sign = "+" if pl >= 0 else ""
    pl_color = "#10b981" if pl >= 0 else "#ef4444"

    hist = alpaca_api.get_portfolio_history(period_days=30)
    if hist.get("connected") and hist["equity_series"]:
        r = hist["return_pct"]
        r_color = "#10b981" if r >= 0 else "#ef4444"
        thirty = (f"<span style='color:{r_color}'>30d {'+' if r>=0 else ''}{r:.2f}%</span>")
    else:
        thirty = "<span style='opacity:0.5'>30d —</span>"

    orders = alpaca_api.get_open_orders(limit=25)
    pending_badge = (f" · <b>📋 {len(orders)} pending</b>"
                     if orders else "")

    strip = (
        f"<div style='font-size:12px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;"
        f"padding:4px 8px;border:1px solid var(--border-color-primary);border-radius:4px;"
        f"background:var(--background-fill-secondary);'>"
        f"<b>💰 {acct['status']}</b>"
        f" · Equity <b>${acct['equity']:,.0f}</b>"
        f" · Cash <b>${acct['cash']:,.0f}</b>"
        f" · BP <b>${acct['buying_power']:,.0f}</b>"
        f" · <span style='color:{pl_color}'>Today {pl_sign}${pl:,.2f} "
        f"({pl_sign}{acct['todays_pl_pct']:.2f}%)</span>"
        f" · {thirty}"
        f"{pending_badge}"
        f"</div>"
    )

    # Pending orders only when present — collapsible, default open
    if orders:
        order_rows = []
        for o in orders:
            side_emoji = "🟢" if o["side"] == "BUY" else "🔴"
            kind = "Bracket" if o["is_bracket"] else o["order_type"].title()
            qty = int(o["qty"]) if o["qty"] == int(o["qty"]) else o["qty"]
            px = ""
            if o["limit_price"]:
                px += f"@ ${o['limit_price']:.2f}"
            if o["stop_price"]:
                px += (" " if px else "") + f"stop ${o['stop_price']:.2f}"
            submitted = (o["submitted_at"] or "")[:16].replace("T", " ")
            order_rows.append(
                f"<tr><td>{side_emoji} <b>{o['symbol']}</b></td><td>{o['side']}</td>"
                f"<td style='text-align:right'>{qty}</td><td>{kind}</td>"
                f"<td>{px or '—'}</td><td><code>{o['status']}</code></td>"
                f"<td>{submitted}</td></tr>"
            )
        strip += (
            f"<details style='margin-top:4px;font-size:12px;' open>"
            f"<summary style='cursor:pointer;font-weight:500;'>"
            f"📋 {len(orders)} pending order(s) — queued for market open</summary>"
            f"<table style='width:100%;margin-top:4px;font-size:11px;border-collapse:collapse;'>"
            f"<thead><tr><th></th><th>Side</th><th style='text-align:right'>Qty</th>"
            f"<th>Type</th><th>Price</th><th>Status</th><th>Submitted</th></tr></thead>"
            f"<tbody>{''.join(order_rows)}</tbody>"
            f"</table></details>"
        )
    return strip


def handle_quick_trade(ticker, shares, action, order_type, tif, limit_price, stop_price,
                       sl_price=None, tp_price=None):
    if not ticker or shares is None or shares <= 0:
        return "⚠️ Please enter a valid ticker and a share amount greater than 0."

    # Validate required prices based on order type
    if order_type in ["Limit", "Stop Limit"] and not limit_price:
        return f"⚠️ {order_type} orders require a Limit Price."
    if order_type in ["Stop", "Stop Limit", "Trailing Stop"] and not stop_price:
        return f"⚠️ {order_type} orders require a Stop Price (or Trailing Value)."

    # Bracket path: Market entry + SL + TP atomically
    if order_type == "Market" and sl_price and tp_price and sl_price > 0 and tp_price > 0:
        return alpaca_api.submit_bracket_order(
            ticker=ticker, action=action, shares=int(shares),
            sl=float(sl_price), tp=float(tp_price), tif=tif,
        )

    # Single-order path (existing behavior)
    return alpaca_api.submit_advanced_order(
        ticker=ticker, action=action, shares=int(shares),
        order_type=order_type, tif=tif, limit_price=limit_price, stop_price=stop_price
    )

# A sample "Universe" of stocks to scan for the "Top Picks" feature
TOP_TICKERS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "INTC",
    "JPM", "V", "WMT", "PG", "XOM", "JNJ", "HD", "BAC", "KO", "PEP"
]

# get_polygon_news, get_fundamentals, get_full_history, get_earnings_risk,
# get_sentiment, calculate_technicals, get_weekly_confluence,
# calculate_sizing, calculate_quant_score — all imported from data_fetcher / scoring

def analyze_single_ticker_data(ticker, pre_fetched_data=None, pre_fetched_funds=None, pre_fetched_history=None):
    """
    Deep analysis for one ticker using Ensemble (Consensus) or Single mode.
    Accepts pre_fetched_history to skip the Polygon API call entirely.
    """
    # 1. Gather Context
    if pre_fetched_data:
        price      = pre_fetched_data.get('c')
        open_price = pre_fetched_data.get('o')
        change     = round(((price - open_price) / open_price) * 100, 2) if open_price else 0

        # Use pre-fetched history if available (skips Polygon call)
        history = pre_fetched_history if pre_fetched_history else get_full_history(ticker)
        if history:
            techs = calculate_technicals(history)
            techs['price']  = price
            techs['change'] = change
        else:
            techs = {"price": price, "change": change, "volatility": 0,
                     "trend": "UP" if price > open_price else "DOWN",
                     "gain_20d": 0, "semantic_rsi": "NEUTRAL",
                     "semantic_bb": "NEUTRAL", "atr": 0, "rsi": 50,
                     "percent_b": 0.5, "rel_vol": 1.0, "sma_20": price}

        funds = pre_fetched_funds if pre_fetched_funds else (
            get_fundamentals(ticker) or {"pe": "N/A", "growth": "N/A", "news": "N/A"}
        )
    else:
        history = pre_fetched_history if pre_fetched_history else get_full_history(ticker)
        if not history: return None
        techs = calculate_technicals(history)
        funds = pre_fetched_funds if pre_fetched_funds else (
            get_fundamentals(ticker) or {"pe": "N/A", "growth": "N/A", "news": "N/A"}
        )

    # 2. Build Prompt — front-load ticker + company name so Gemma 4B can't
    # drift to a more-prominent ticker (was previously hallucinating GME content
    # for SONY because the symbol only appeared in the final response anchor).
    mem_ctx = agent_memory.get_past_context(ticker)
    company_name = funds.get('name') or ticker.upper()
    industry = funds.get('industry', 'N/A')
    prompt = f"""### Instruction: Act as a Hedge Fund Analyst.

    ANALYSIS TARGET: {ticker.upper()} — {company_name} ({industry})

    Analyze {ticker.upper()} ({company_name}) using Technicals and Fundamentals.
    All reasoning MUST reference {ticker.upper()} or {company_name} explicitly.
    Do not analyze any other company.

    1. Technicals for {ticker.upper()}: {techs['trend']} Trend, RSI Flag: {techs.get('semantic_rsi', 'NEUTRAL')}, Bollinger Flag: {techs.get('semantic_bb', 'NEUTRAL')}
    2. Fundamentals for {ticker.upper()}: PE Ratio {funds['pe']}, Rev Growth {funds['growth']}.
    3. Context for {ticker.upper()}: {funds['news'][:500]}
    4. Memory (past decisions on {ticker.upper()}): {mem_ctx}

    Provide your analysis as a CONCISE JSON object with the following fields:
    - "reasoning": "A concise explanation of {ticker.upper()}'s setup"
    - "considerations": ["Factor 1 specific to {ticker.upper()}", "Factor 2", "Factor 3"]
    - "rating": "BUY/SELL/HOLD"
    - "score": 0-100 (Conviction score)

    Ensure the response is valid JSON only. Do not repeat the output."""
    prompt += f" ### Input: Ticker: {ticker.upper()} ({company_name}), Close: ${techs['price']} ### Response:"
    # 3. Analyze with Consensus/Single
    if analyst.mode == "ensemble":
        is_consensus, score, response = analyst.get_consensus(ticker, prompt)
        if not is_consensus:
            response = "{\"reasoning\": \"⚠️ ENSEMBLE WARNING: NO CONSENSUS FOUND.\", \"considerations\": [], \"metrics\": {}, \"rating\": \"HOLD\", \"score\": 50}"
    else:
        sampler = make_sampler(temp=0.0)
        response = generate(model, tokenizer, prompt=prompt, sampler=sampler, max_tokens=500, verbose=False)
    
    # 4. Extract and Log
    structured_data = analyst.get_structured_data(response)
    score = structured_data.get("score", 50)

    # Write through to AI score cache so research cycle + live agent can reuse it.
    try:
        ai_score_cache.store(ticker.upper(), {
            "score":          score,
            "rating":         structured_data.get("rating", "HOLD"),
            "reasoning":      structured_data.get("reasoning", ""),
            "bull_case":      structured_data.get("bull_case", ""),
            "bear_case":      structured_data.get("bear_case", ""),
            "key_risk":       structured_data.get("key_risk", ""),
            "considerations": structured_data.get("considerations", []),
        })
    except Exception as e:
        logger.log(f"⚠️ AI score cache write failed [{ticker}]: {e}", level="WARNING")

    # Calculate additional metrics for Hybrid Quant Score
    # We use fast/local versions to keep the scan snappy
    w_trend = get_weekly_confluence(history) if history else "UNKNOWN"
    e_risk = get_earnings_risk(ticker) if not pre_fetched_data else {"risk": False, "days": 30} # Estimate if pre-fetched
    st_sent = get_sentiment(ticker) if not pre_fetched_data else "Neutral"
    
    final_score = calculate_quant_score(techs, funds, w_trend, e_risk, st_sent, score)
    
    # Consistency Check: Rating follows Quant Score
    if final_score >= 60: rating = "BUY"
    elif final_score <= 30: rating = "SELL"
    else: rating = "HOLD"

    # Record to persistent Signal History
    portfolio_mgr.log_analysis_result(
        ticker, funds.get('name', ticker), funds.get('industry', 'N/A'), 
        techs['price'], techs, funds, rating, score, final_score
    )

    return {
        "Ticker": ticker,
        "Price": techs['price'],
        "Change %": techs['change'],
        "Score": final_score,
        "Rating": rating,
        "Trend": techs['trend'],
        "PE Ratio": funds['pe'],
        "20D Gain %": techs.get('gain_20d', 0),
        "Analysis": structured_data.get("reasoning", "No analysis")
    }

def get_last_trading_day():
    """Returns the last weekday (Mon-Fri) in YYYY-MM-DD format."""
    d = date.today()
    while True:
        d -= timedelta(days=1)
        if d.weekday() < 5: # 0-4 are Mon-Fri
            return d.strftime("%Y-%m-%d")

def get_live_stats(ticker):
    """Fetches live/latest stats for a single ticker via Polygon."""
    history = get_full_history(ticker)
    if not history:
        return None, 0, 0
    
    last = history[-1]
    price = last.get('c')
    
    prev = history[-2]['c'] if len(history) > 1 else price
    change = round(((price - prev)/prev)*100, 2) if prev else 0
    
    vol = round(((last['h'] - last['l'])/price)*100, 2) if price else 0
    return price, change, vol

def get_trading_day_n_ago(n):
    """Returns the date N trading days ago in YYYY-MM-DD format."""
    d = date.today()
    count = 0
    while count < n:
        d -= timedelta(days=1)
        if d.weekday() < 5: # Monday-Friday
            count += 1
    return d.strftime("%Y-%m-%d")

import platform
import subprocess
from functools import wraps

def prevent_sleep(func):
    """Decorator to prevent macOS from going to sleep while a function runs."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        process = None
        if platform.system() == 'Darwin':
            try:
                # -i prevents idle sleep, -s prevents system sleep
                process = subprocess.Popen(['caffeinate', '-i', '-s'])
                logger.log(f"☕ System sleep disabled for {func.__name__}.")
            except Exception as e:
                logger.log(f"⚠️ Could not acquire wake lock: {e}", level="WARNING")
        try:
            return func(*args, **kwargs)
        finally:
            if process:
                process.terminate()
                process.wait()
                logger.log(f"🛌 System sleep re-enabled after {func.__name__}.")
    return wrapper

@prevent_sleep
def scan_market(progress=gr.Progress()):
    """Scans the TOP_TICKERS list efficiently with backtesting."""
    results = []
    
    progress(0, desc="Fetching Market Data...")
    
    # 1. Fetch CURRENT data and HISTORICAL data (20 days ago)
    target_date = get_last_trading_day()
    date_20d_ago = get_trading_day_n_ago(20)
    
    rate_limiter.wait() # First call
    url_curr = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{target_date}?adjusted=true&apiKey={POLYGON_API_KEY}"
    
    rate_limiter.wait() # Second call
    url_hist = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_20d_ago}?adjusted=true&apiKey={POLYGON_API_KEY}"
    
    market_curr = {}
    market_hist = {}
    
    try:
        logger.log(f"📡 Fetching bulk data: Current ({target_date}) & History ({date_20d_ago})...")
        resp_c = requests.get(url_curr, timeout=20).json()
        resp_h = requests.get(url_hist, timeout=20).json()
        
        if "results" in resp_c:
            for item in resp_c["results"]: market_curr[item["T"]] = item
        if "results" in resp_h:
            for item in resp_h["results"]: market_hist[item["T"]] = item
            
    except Exception as e:
        logger.log(f"⚠️ Batch Fetch Error: {e}", level="ERROR")
        return pd.DataFrame(), "Error fetching market data."

    # 2. Pre-fetch fundamentals AND history in parallel
    progress(0.2, desc="Batch fetching fundamentals + price history...")
    tickers = TOP_TICKERS
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    batch_funds   = loop.run_until_complete(get_fundamentals_batch_async(tickers, logger))
    # skip_stale=True: stale tickers return SQLite cache instantly; we inject today's
    # bar from the bulk snapshot (already fetched above) so no per-ticker Polygon call.
    batch_history = loop.run_until_complete(get_history_batch_async(tickers, logger, skip_stale=True))
    logger.log(f"📦 History batch complete for {len(batch_history)} tickers")

    # 3. Sequential AI Inference (MLX/Metal is NOT thread-safe — 1 worker prevents GPU hangs)
    # Speed gain comes from pre-fetched data above, not concurrent inference.
    import concurrent.futures as _cf
    _results_lock = threading.Lock()
    _counter      = [0]

    def _analyze_one(ticker):
        if ticker not in market_curr:
            return
        try:
            funds_data   = batch_funds.get(ticker)
            history_data = batch_history.get(ticker, [])

            # Inject today's bar from the bulk snapshot into the cached history.
            # This keeps technical indicators current without an extra Polygon call.
            if history_data and ticker in market_curr:
                snap = market_curr[ticker]
                snap_date = target_date  # already a YYYY-MM-DD string
                last_cached_date = history_data[-1].get("t", "")
                if last_cached_date < snap_date:
                    today_bar = {
                        "o": snap.get("o"), "h": snap.get("h"),
                        "l": snap.get("l"), "c": snap.get("c"),
                        "v": snap.get("v"), "t": snap_date,
                    }
                    history_data = history_data + [today_bar]

            data = analyze_single_ticker_data(
                ticker,
                pre_fetched_data=market_curr[ticker],
                pre_fetched_funds=funds_data,
                pre_fetched_history=history_data,
            )
            if data:
                if ticker in market_hist:
                    price_now  = market_curr[ticker]['c']
                    price_then = market_hist[ticker]['c']
                    data["Backtest (20D %)"] = round(((price_now - price_then) / price_then) * 100, 2)
                else:
                    data["Backtest (20D %)"] = 0.0
                with _results_lock:
                    results.append(data)
                    _counter[0] += 1
                    progress(0.3 + 0.7 * (_counter[0] / len(TOP_TICKERS)),
                             desc=f"Analyzed {_counter[0]}/{len(TOP_TICKERS)}: {ticker}")
        except Exception as e:
            logger.log(f"⚠️ Skipping {ticker} in scan: {e}", level="WARNING")

    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
        pool.map(_analyze_one, TOP_TICKERS)

    # Sort by Score descending
    results.sort(key=lambda x: x["Score"], reverse=True)
    top_10 = results[:10]
    
    if not top_10:
        logger.log("⚠️ Scan completed: No data available.", level="WARNING")
        return pd.DataFrame(), "No data available."

    # Calculate Overall Portfolio Performance
    avg_perf = sum(item["Backtest (20D %)"] for item in top_10) / len(top_10)
    perf_color = "green" if avg_perf >= 0 else "red"
    summary_md = f"### 📊 Top 10 Portfolio Backtest (Last 20 Days): <span style='color:{perf_color}'>{avg_perf:.2f}%</span>"
    
    logger.log(f"✅ Scan Complete. Top 10 Portfolio Backtest: {avg_perf:.2f}%")
    
    df = pd.DataFrame(top_10)
    # Ensure columns are in order
    return df[["Ticker", "Price", "Change %", "Backtest (20D %)", "Score", "Rating", "Analysis"]], summary_md

def get_portfolio_status(ticker):
    """Checks if we own the ticker and returns basic position info."""
    df = portfolio_mgr.get_portfolio()
    pos = df[df['Ticker'] == ticker.upper()]
    if not pos.empty:
        return f"OWNED: {pos.iloc[0]['Shares']} shares @ ${float(pos.iloc[0]['Avg Price']):.2f} (PnL: {pos.iloc[0]['PnL %']}%)"
    return "NOT OWNED"

from rag_engine import RAGEngine

# Initialize RAG Engine
rag = RAGEngine()

def analyze_stock(ticker, is_auto=False):
    total_steps = 6
    tag = f"[{ticker.upper()}]"
    status_msg = f"🔍 Analyzing {ticker.upper()}..."
    logger.log(f"{tag} ====== ANALYSIS START (auto={is_auto}) =====")
    
    # Check if stock is in portfolio for SELL logic
    in_portfolio = portfolio_mgr.is_in_portfolio(ticker)

    # STEP 1: Price History
    logger.log(f"{tag} Step 1/{total_steps}: Fetching 60-day OHLCV price history...")
    history = get_full_history(ticker.upper())
    if not history:
        logger.log(f"{tag} ❌ Step 1 FAILED — no price data returned", level="ERROR")
        return "❌ Error: Could not fetch data.", None, None
    logger.log(f"{tag}   └ Got {len(history)} trading days")

    # STEP 2: Technicals
    logger.log(f"{tag} Step 2/{total_steps}: Computing technical indicators (RSI, Bollinger, SMA20)...")
    techs = calculate_technicals(history)
    if not techs:
        logger.log(f"{tag} ❌ Step 2 FAILED — insufficient history", level="WARNING")
        return "❌ Error: Insufficient history.", None, None
    logger.log(f"{tag}   └ Price=${techs['price']}, RSI={techs['rsi']}, Trend={techs['trend']}, %B={techs['percent_b']}, RelVol={techs['rel_vol']}x")

    # STEP 3: RAG Context
    status_msg += "\n📡 Step 3: Retrieving SEC filings and news..."
    logger.log(f"{tag} Step 3/{total_steps}: Fetching RAG context (SEC 10-K/Q + Polygon news)...")
    rag_context = rag.get_grounded_context(ticker.upper())
    logger.log(f"{tag}   └ RAG context: {len(rag_context)} chars retrieved")

    # STEP 4: Fundamentals & Confluence
    status_msg += "\n🧠 Step 4: Fetching fundamental data & confluence..."
    logger.log(f"{tag} Step 4/{total_steps}: Fetching fundamental data and sentiment...")
    funds = get_fundamentals(ticker.upper()) or {"pe": "N/A", "f_pe": "N/A", "growth": "N/A", "news": "N/A"}
    w_trend = get_weekly_confluence(history) # Now uses history, no API call
    e_risk = get_earnings_risk(ticker.upper())
    st_sent = get_sentiment(ticker.upper())
    logger.log(f"{tag}   └ P/E={funds['pe']}, Weekly={w_trend}, EarningsRisk={e_risk['risk']}, Sentiment={st_sent}")

    # STEP 5: Build Prompt & Run Ensemble
    status_msg += "\n⚖️ Step 5: Running dual-model consensus..."
    logger.log(f"{tag} Step 5/{total_steps}: Building prompt and running Ensemble inference...")
    mem_ctx = agent_memory.get_past_context(ticker)
    # Anti-drift: small LLMs (Gemma 4B) confuse tickers when the symbol only
    # appears at the very end of the prompt — they default to whatever is
    # most prominent in training (e.g., GameStop). Front-load the company
    # identity and demand explicit reference to it in the reasoning.
    company_name = funds.get('name') or ticker.upper()
    industry = funds.get('industry', 'N/A')
    prompt = f"""### Instruction: You are a Senior Hedge Fund Quant Analyst.

    ANALYSIS TARGET: {ticker.upper()} — {company_name} ({industry})

    Produce a structured analysis of {ticker.upper()} ({company_name}). All reasoning,
    bull case, bear case, and key risk MUST reference {ticker.upper()} and/or
    {company_name} explicitly. Do not analyze, mention, or reason about any
    other company.

    GROUNDING CONTEXT (SEC Filings & News for {ticker.upper()}):
    {rag_context}

    MARKET DATA for {ticker.upper()} ({company_name}):
    - Price: ${techs['price']} ({'+' if techs['change']>=0 else ''}{techs['change']}% today)
    - Technical Signal (Daily): {techs['trend']} Trend | RSI={techs['rsi']} ({techs.get('semantic_rsi','NEUTRAL')}) | %B={techs['percent_b']} ({techs.get('semantic_bb','NEUTRAL')})
    - Technical Signal (Weekly): {w_trend} Trend
    - Momentum: RelVol={techs['rel_vol']}x | Volatility={techs['volatility']}% | ATR={techs['atr']}
    - Fundamentals: Trailing P/E={funds['pe']} | Forward P/E={funds['f_pe']} | Rev Growth={funds['growth']}
    - Macro Context: Social Sentiment={st_sent} | Earnings Risk={'HIGH — within 5 days' if e_risk['risk'] else 'LOW'}
    - Recent Headlines: {funds['news'][:400]}
    - Agent Memory (Past Decisions for {ticker.upper()}): {mem_ctx}

    Respond ONLY with a single valid JSON object using this exact schema. Every
    string field MUST mention {ticker.upper()} or {company_name} by name:
    {{"reasoning": "2-3 sentence synthesis of {ticker.upper()}'s setup covering technical, fundamental, and key risk",
      "bull_case": "strongest argument FOR entering {ticker.upper()}",
      "bear_case": "strongest argument AGAINST entering {ticker.upper()}",
      "key_risk": "single biggest risk to the {ticker.upper()} thesis",
      "considerations": ["factor1 specific to {ticker.upper()}", "factor2", "factor3"],
      "rating": "BUY|SELL|HOLD",
      "score": <0-100>}}"""
    prompt += f" ### Input: {ticker.upper()} ({company_name}) @ ${techs['price']} ### Response:"

    is_consensus = True
    if analyst.mode == "ensemble":
        consensus_data = analyst.get_consensus(ticker.upper(), prompt)
        is_consensus = consensus_data["is_consensus"]
        score = consensus_data["score"]
        recommendation = consensus_data["rating"]
        response = consensus_data["response"]
        
        if not is_consensus:
            logger.log(f"{tag} ⚠️ No consensus — using weighted average ({score})", level="WARNING")
        
        # Parse the combined response just for the reasoning text
        analysis_data = analyst.get_structured_data(response)
        reasoning = analysis_data.get("reasoning", response[:1000])
    else:
        sampler = make_sampler(temp=0.0)
        response = generate(model, tokenizer, prompt=prompt, sampler=sampler, max_tokens=500, verbose=False)
        analysis_data = analyst.get_structured_data(response)
        reasoning = analysis_data.get("reasoning", "")
        score = int(analysis_data.get("score", 50))
        recommendation = analysis_data.get('rating', 'HOLD')
        
        # Single Model Sanity Guard
        if score < 70 and recommendation == "BUY":
            logger.log(f"{tag} 🛡️ Sanity Guard: Overriding BUY to HOLD (Score {score} too low)")
            recommendation = "HOLD"
        elif score > 80 and recommendation == "HOLD":
            logger.log(f"{tag} 🛡️ Sanity Guard: Overriding HOLD to BUY (Score {score} high enough)")
            recommendation = "BUY"

    # Reasoning-drift guard: detect when Gemma's output talks about a different
    # company than the requested ticker (the SONY → GameStop bug, 2026-05-24).
    drift_text = " ".join([
        reasoning or "",
        " ".join(analysis_data.get("considerations") or []),
        analysis_data.get("bull_case", "") or "",
        analysis_data.get("bear_case", "") or "",
        analysis_data.get("key_risk", "") or "",
    ])
    drift_marker = detect_reasoning_drift(drift_text, ticker, funds.get('name', ''))
    if drift_marker:
        logger.log(
            f"{tag} 🚨 REASONING DRIFT: response mentions '{drift_marker}' (foreign ticker) "
            f"but analyzing {ticker.upper()}. Skipping ai_score_cache write to prevent "
            f"contamination, flagging in UI.",
            level="WARNING",
        )
        reasoning = (
            f"⚠️ DRIFT DETECTED — Gemma's response mentioned '{drift_marker}' but the "
            f"target ticker is {ticker.upper()} ({funds.get('name', '?')}). "
            f"Output discarded as potentially hallucinated. Re-run for a fresh response. "
            f"Original (truncated): {reasoning[:300]}"
        )
        analysis_data = dict(analysis_data, reasoning=reasoning)
    else:
        # Write through to persistent AI score cache so overnight cycles and the
        # live reactive agent (Phase 3) can reuse it without re-running Gemma.
        try:
            ai_score_cache.store(ticker.upper(), {
                "score":          score,
                "rating":         recommendation,
                "reasoning":      reasoning,
                "bull_case":      analysis_data.get("bull_case", ""),
                "bear_case":      analysis_data.get("bear_case", ""),
                "key_risk":       analysis_data.get("key_risk", ""),
                "considerations": analysis_data.get("considerations", []),
            })
        except Exception as e:
            logger.log(f"{tag} ⚠️ AI score cache write failed: {e}", level="WARNING")

    # STEP 6: Quant Scoring & Final Recommendation
    status_msg += "\n✅ Step 6: Calculating Hybrid Quant Score..."
    logger.log(f"{tag} Step 6/{total_steps}: Merging AI analysis with objective Quant metrics...")

    # Calculate the new objective score
    final_score = calculate_quant_score(techs, funds, w_trend, e_risk, st_sent, score)
    
    # Consistency Check: Rating must follow the Quant Score
    if final_score >= 60: recommendation = "BUY"
    elif final_score <= 30: recommendation = "SELL"
    else: recommendation = "HOLD"
    
    # --- DETAILED AUDIT LOG ---
    audit_log = f"📊 [AUDIT] {ticker.upper()} | AI:{score} | Quant:{final_score} | Rating:{recommendation}\n"
    audit_log += f"   └ Techs: {techs['trend']} Trend, RSI:{techs['rsi']}, RelVol:{techs['rel_vol']}x\n"
    audit_log += f"   └ Funds: P/E:{funds.get('pe','N/A')}, Growth:{funds.get('growth','N/A')}, Weekly:{w_trend}"
    logger.log(audit_log)
    
    # Record to persistent Signal History
    portfolio_mgr.log_analysis_result(
        ticker, funds.get('name', ticker), funds.get('industry', 'N/A'), 
        techs['price'], techs, funds, recommendation, score, final_score
    )
    
    if abs(final_score - score) > 15:
        logger.log(f"{tag} ⚖️ Quant Overwrite: AI suggested {score}, Quant corrected to {final_score}")

    logger.log(f"{tag} ====== RESULT: FinalScore={final_score}/100 | Rating={recommendation} | Consensus={is_consensus} ======")
    status_msg += "\n✅ Analysis Complete."

    rating_color = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(recommendation, "⚪")
    score_bar = "█" * (final_score // 10) + "░" * (10 - final_score // 10)
    consensus_badge = "✅ Consensus" if is_consensus else "⚠️ No Consensus"

    # ── BOX 1: Signal Card (top-left) ─────────────────────────────────────
    output_rec  = f"## {ticker.upper()} — {funds.get('name', ticker)}\n"
    output_rec += f"`{funds.get('industry', 'N/A')}`\n\n"
    output_rec += f"### {rating_color} **{recommendation}** &nbsp; `{consensus_badge}` &nbsp; Score: **{final_score}/100**\n\n"
    output_rec += f"`{score_bar}` {final_score}%\n\n---\n"
    output_rec += f"| Metric | Value |\n|:---|:---|\n"
    output_rec += f"| 💵 Price | **${techs['price']}** ({'+' if techs['change']>=0 else ''}{techs['change']}%) |\n"
    output_rec += f"| 📊 RSI | {techs['rsi']} — *{techs['semantic_rsi']}* |\n"
    output_rec += f"| 📉 Bollinger %B | {techs['percent_b']} — *{techs['semantic_bb']}* |\n"
    output_rec += f"| 📈 Trend Daily/Weekly | {techs['trend']} / {w_trend} |\n"
    output_rec += f"| 🔊 Relative Volume | {techs['rel_vol']}x |\n"
    output_rec += f"| 💹 Trailing P/E | {funds.get('pe', 'N/A')} |\n"
    output_rec += f"| ⚡ Forward P/E | {funds.get('f_pe', 'N/A')} |\n"
    output_rec += f"| 🚀 Rev Growth | {funds.get('growth', 'N/A')} |\n"
    output_rec += f"| 💬 Sentiment | {st_sent} |\n"
    output_rec += f"| ⚠️ Earnings Risk | {'🔥 HIGH (within 5 days)' if e_risk['risk'] else '✅ LOW'} |\n"
    output_rec += f"| 🤖 AI Raw Score | {score}/100 |\n"

    # ── BOX 3: AI Chain-of-Thought (bottom-left) ──────────────────────────
    bull  = analysis_data.get('bull_case', '')
    bear  = analysis_data.get('bear_case', '')
    risk  = analysis_data.get('key_risk', '')
    cons  = analysis_data.get('considerations', [])

    cot_md  = f"### 🧠 AI Chain-of-Thought Analysis\n\n"
    cot_md += f"**Summary:** {reasoning}\n\n"
    if bull: cot_md += f"**🟢 Bull Case:** {bull}\n\n"
    if bear: cot_md += f"**🔴 Bear Case:** {bear}\n\n"
    if risk: cot_md += f"**⚡ Key Risk:** {risk}\n\n"
    if cons:
        cot_md += "**Key Considerations:**\n"
        for c in cons: cot_md += f"- {c}\n"

    # ── BOX 4: Trade Setup (bottom-right) ─────────────────────────────────
    sizing = calculate_sizing(techs['price'], techs['atr'], account=_live_equity())
    gate_threshold = 75 if e_risk['risk'] else 65
    gate_status = "🔒 GATE LOCKED" if final_score < gate_threshold else "🔓 GATE OPEN — awaiting Telegram approval"

    setup_md  = f"### 📐 Trade Setup\n\n"
    setup_md += f"| Parameter | Value |\n|:---|:---|\n"
    setup_md += f"| 🎯 Suggested Shares | {sizing['shares']} |\n"
    setup_md += f"| 💰 Position Size | ${sizing['amount']:,.2f} |\n"
    setup_md += f"| 🛑 Stop Loss | ${sizing['sl']} (2× ATR) |\n"
    setup_md += f"| 🎯 Take Profit | ${sizing['tp']} (4× ATR) |\n"
    setup_md += f"| 📏 ATR | ${techs['atr']} |\n"
    setup_md += f"| 📅 20D Performance | {techs.get('gain_20d', 'N/A')}% |\n"
    setup_md += f"| ⏳ Days to Earnings | {e_risk['days']} days |\n"
    setup_md += f"\n---\n**Execution Gate ({gate_threshold}pt threshold):** {gate_status}\n"

    # --- PORTFOLIO EXECUTION ---
    # Gating Logic: 
    # BUY: Final Quant Score >= threshold
    gate_threshold = 75 if e_risk['risk'] else 65
    
    is_buy = recommendation == "BUY"
    is_sell = recommendation == "SELL"

    # SELL gate: a single dipped score on a held position shouldn't auto-exit.
    # Require corroborating evidence: overbought RSI, extended %B, or earnings imminent.
    sell_distress = (
        techs.get("rsi", 50) > 75 or
        techs.get("percent_b", 0.5) > 0.95 or
        e_risk.get("risk", False)
    )
    should_trigger_buy = is_buy and final_score >= gate_threshold and is_consensus
    should_trigger_sell = is_sell and in_portfolio and sell_distress

    if should_trigger_sell:
        # Automate SELL signals: close at broker first, then update local state.
        logger.log(f"📉 Auto-Executing SELL for {ticker} (Score: {final_score}, "
                   f"RSI: {techs.get('rsi')}, %B: {techs.get('percent_b')}, earnings: {e_risk.get('risk')}).")
        close_result = alpaca_api.close_position(ticker.upper())
        gatekeeper.send_notification(
            f"📉 [PORTFOLIO EXIT] {ticker.upper()}\n"
            f"Score: {final_score} | RSI: {techs.get('rsi')} | %B: {techs.get('percent_b')}\n"
            f"Price: ${techs['price']}\n{close_result}\n"
            f"Reason: {reasoning[:200]}"
        )
        portfolio_mgr.remove_position(ticker.upper())
        output_rec += f"\n\n{close_result}"
        agent_memory.log_decision(ticker.upper(), recommendation, final_score, reasoning)
    elif is_sell and in_portfolio:
        logger.log(f"🛑 SELL classified for {ticker} but distress gate not met "
                   f"(RSI={techs.get('rsi')}, %B={techs.get('percent_b')}, earnings={e_risk.get('risk')}). Holding.")
        output_rec += (f"\n\n🛑 SELL classified (score {final_score}) but distress gate not met — holding. "
                       f"RSI={techs.get('rsi')}, %B={techs.get('percent_b')}.")
        
    elif should_trigger_buy:
        # BUY signals require Telegram approval, dispatched ASYNC so the UI
        # (or agent cycle) doesn't block for up to 5 minutes waiting for the tap.
        logger.log(f"🎯 Gating Passed for {ticker} (Threshold: {gate_threshold}). "
                   f"Dispatching Telegram approval request asynchronously...")
        output_rec += (
            f"\n\n📡 **Approval requested via Telegram** "
            f"(Earnings Risk: {'HIGH' if e_risk['risk'] else 'LOW'})\n"
            f"_The UI won't wait — respond on Telegram. "
            f"Execution + confirmation will happen in the background._"
        )

        # Capture values to avoid race with outer scope mutations.
        _ctx = {
            'ticker':         ticker.upper(),
            'price':          techs['price'],
            'atr':            techs['atr'],
            'final_score':    final_score,
            'recommendation': recommendation,
            'reasoning':      reasoning,
        }

        def _await_approval_and_execute(ctx):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    gatekeeper.request_approval(
                        ctx['ticker'], ctx['price'], ctx['final_score'], ctx['reasoning']
                    ),
                    gatekeeper.loop,
                )
                decision = future.result(timeout=305)
                logger.log(f"📱 Telegram Decision for {ctx['ticker']}: {decision}")

                if decision != "EXECUTE":
                    logger.log(f"🛑 {ctx['ticker']} approval result: {decision} — no trade placed.")
                    return

                sizing = calculate_sizing(ctx['price'], ctx['atr'], account=_live_equity())
                shares_int = int(sizing['shares'])
                if shares_int < 1:
                    msg = (f"⚠️ {ctx['ticker']} position size rounds to 0 shares "
                           f"(price=${ctx['price']}, atr={ctx['atr']}, raw={sizing['shares']}). Skipped.")
                    logger.log(msg, level="WARNING")
                    gatekeeper.send_notification(msg)
                    return

                exec_result = alpaca_api.submit_bracket_order(
                    ctx['ticker'], 'buy', shares_int,
                    sl=sizing['sl'], tp=sizing['tp'],
                )
                portfolio_mgr.add_position(
                    ctx['ticker'], shares_int, ctx['price'],
                    sl=sizing['sl'], tp=sizing['tp'],
                    score=ctx['final_score'], rating=ctx['recommendation'],
                )
                logger.log(f"✅ Executed bracket BUY for {ctx['ticker']}: {shares_int} shares — {exec_result}")
                agent_memory.log_decision(
                    ctx['ticker'], ctx['recommendation'], ctx['final_score'], ctx['reasoning']
                )
                # Loop back to the user via Telegram since the UI has already moved on.
                gatekeeper.send_notification(
                    f"✅ Trade executed: BUY {shares_int} {ctx['ticker']} @ ~${ctx['price']}\n"
                    f"SL: ${sizing['sl']} | TP: ${sizing['tp']} | Score: {ctx['final_score']}"
                )
            except Exception as e:
                logger.log(f"⚠️ Async approval/execute error for {ctx['ticker']}: {e}", level="ERROR")
                try:
                    gatekeeper.send_notification(f"⚠️ {ctx['ticker']} approval/execute error: {e}")
                except Exception:
                    pass

        threading.Thread(
            target=_await_approval_and_execute,
            args=(_ctx,),
            name=f"approval-{ticker.upper()}",
            daemon=True,
        ).start()

    # --- Build rich interactive Plotly chart ---
    chart_fig = None
    try:
        df_h = pd.DataFrame(history)
        df_h['Date'] = pd.to_datetime(df_h['t'], unit='ms')
        # Removed Matplotlib refs

        # SMA20 & Bollinger Bands
        win = min(20, len(df_h))
        df_h['SMA20'] = df_h['c'].rolling(window=win).mean()
        df_h['STD'] = df_h['c'].rolling(window=win).std()
        df_h['Upper'] = df_h['SMA20'] + 2*df_h['STD']
        df_h['Lower'] = df_h['SMA20'] - 2*df_h['STD']

        # Create Figure with subplots (Price/Indicators + Volume)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                           vertical_spacing=0.03, subplot_titles=(f'{ticker.upper()} Price Action', 'Volume'),
                           row_width=[0.3, 0.7])

        # 1. Candlestick Chart
        fig.add_trace(go.Candlestick(x=df_h['Date'],
                        open=df_h['o'], high=df_h['h'],
                        low=df_h['l'], close=df_h['c'],
                        name='OHLC'), row=1, col=1)

        # 2. SMA 20
        fig.add_trace(go.Scatter(x=df_h['Date'], y=df_h['SMA20'],
                        line=dict(color='#f59e0b', width=1.5),
                        name='SMA20'), row=1, col=1)

        # 3. Bollinger Bands
        fig.add_trace(go.Scatter(x=df_h['Date'], y=df_h['Upper'],
                        line=dict(color='#6366f1', width=0.5, dash='dot'),
                        name='Upper BB', showlegend=False), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_h['Date'], y=df_h['Lower'],
                        line=dict(color='#6366f1', width=0.5, dash='dot'),
                        name='Lower BB', fill='tonexty', 
                        fillcolor='rgba(99, 102, 241, 0.05)', showlegend=False), row=1, col=1)

        # 4. Volume Bars
        colors = ['#065f46' if row['c'] >= row['o'] else '#ef4444' for _, row in df_h.iterrows()]
        fig.add_trace(go.Bar(x=df_h['Date'], y=df_h['v'],
                        marker_color=colors, name='Volume'), row=2, col=1)

        # Layout styling
        fig.update_layout(
            template='plotly_dark',
            paper_bgcolor='#0f1117',
            plot_bgcolor='#0f1117',
            showlegend=True,
            xaxis_rangeslider_visible=False,
            height=600,
            margin=dict(l=50, r=50, t=50, b=50),
            title_text=f"{ticker.upper()} Analysis | Score: {final_score} | {recommendation}"
        )
        
        # Add annotation for rating
        r_color = '#065f46' if recommendation == 'BUY' else ('#ef4444' if recommendation == 'SELL' else '#f59e0b')
        fig.add_annotation(text=f"<b>{recommendation}</b>",
                          xref="paper", yref="paper",
                          x=0.98, y=0.98, showarrow=False,
                          font=dict(size=16, color="white"),
                          bgcolor=r_color, borderpad=4, bordercolor=r_color)

        chart_fig = fig
    except Exception as e:
        logger.log(f"⚠️ Plotly Chart render error: {e}", level="WARNING")

    return output_rec, cot_md, chart_fig, setup_md

import subprocess
import os
import signal

def kill_port(port):
    """Finds and kills the process using the specified port."""
    try:
        result = subprocess.run(["lsof", "-t", f"-i:{port}"], capture_output=True, text=True)
        if result.stdout:
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    logger.log(f"✅ Killed process {pid} on port {port}")
                except ProcessLookupError:
                    logger.log(f"Process {pid} already terminated.")
        time.sleep(2)
    except Exception as e:
        logger.log(f"⚠️ Could not clear port {port}: {e}", level="WARNING")

def run_bot_background(gk):
    """Runs the Telegram bot in a dedicated event loop."""
    loop = asyncio.new_event_loop()
    gk.loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(gk.start())
    loop.run_forever()

# Background services (Telegram bot + position monitor) only start when
# NUROQ_BACKGROUND_SERVICES != "0". The cron script sets this to 0 so it can
# import dashboard for run_research_cycle without spinning up a competing
# Telegram poller (which would conflict with the running dashboard's bot token).
START_BACKGROUND_SERVICES = os.getenv("NUROQ_BACKGROUND_SERVICES", "1") != "0"


def _detect_duplicate_dashboard() -> list:
    """
    Returns PIDs of any *other* live `python dashboard.py` processes. Used to
    refuse Telegram polling startup when another dashboard is already running —
    otherwise both would call getUpdates against the same bot token and trigger
    telegram.error.Conflict ("terminated by other getUpdates request") in a
    loop. Returns [] if we're the only one.
    """
    try:
        my_pid = os.getpid()
        result = subprocess.run(
            ["pgrep", "-f", "python.*dashboard\\.py"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return []
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        return [p for p in pids if p != my_pid]
    except Exception:
        return []  # if pgrep is missing or fails, don't block startup


if START_BACKGROUND_SERVICES:
    _other_dashboards = _detect_duplicate_dashboard()
    if _other_dashboards:
        logger.log(
            f"⚠️ Another dashboard.py is already running (PIDs: {_other_dashboards}). "
            f"Skipping Telegram bot startup to avoid getUpdates conflict. "
            f"Kill the old process(es) and restart if this instance should own Telegram.",
            level="WARNING",
        )
    else:
        threading.Thread(
            target=run_bot_background, args=(gatekeeper,), daemon=True,
        ).start()
else:
    logger.log("ℹ️ NUROQ_BACKGROUND_SERVICES=0 — skipping Telegram bot polling.", level="INFO")

# ─── Live agent callback adapters ─────────────────────────────────────────────
# These bridge the LiveAgent (which knows nothing about Gradio/Telegram/Alpaca
# specifics) to the existing async approval + close-position machinery.

def _live_fire_buy(ticker: str, price: float, score: int, reasoning: str) -> None:
    """LiveAgent → Telegram approval → Alpaca bracket (all async)."""
    sizing = calculate_sizing(price, atr=max(price * 0.02, 0.5), account=_live_equity())
    shares_int = int(sizing['shares'])
    if shares_int < 1:
        logger.log(f"⚠️ LiveAgent: {ticker} sizing rounds to 0 shares — skipping.",
                   level="WARNING")
        return

    ctx = {
        'ticker':         ticker,
        'price':          price,
        'final_score':    score,
        'recommendation': "BUY",
        'reasoning':      reasoning,
        'sl':             sizing['sl'],
        'tp':             sizing['tp'],
        'shares':         shares_int,
    }

    def _await_and_execute(c):
        try:
            future = asyncio.run_coroutine_threadsafe(
                gatekeeper.request_approval(c['ticker'], c['price'], c['final_score'], c['reasoning']),
                gatekeeper.loop,
            )
            decision = future.result(timeout=305)
            logger.log(f"📱 LiveAgent Telegram decision for {c['ticker']}: {decision}")
            if decision != "EXECUTE":
                return
            exec_result = alpaca_api.submit_bracket_order(
                c['ticker'], 'buy', c['shares'], sl=c['sl'], tp=c['tp'],
            )
            portfolio_mgr.add_position(
                c['ticker'], c['shares'], c['price'],
                sl=c['sl'], tp=c['tp'],
                score=c['final_score'], rating=c['recommendation'],
            )
            agent_memory.log_decision(c['ticker'], c['recommendation'], c['final_score'], c['reasoning'])
            gatekeeper.send_notification(
                f"✅ LiveAgent trade: BUY {c['shares']} {c['ticker']} @ ~${c['price']:.2f}\n"
                f"SL: ${c['sl']} | TP: ${c['tp']} | Score: {c['final_score']}"
            )
        except Exception as e:
            logger.log(f"⚠️ LiveAgent approve/execute for {c['ticker']}: {e}", level="ERROR")

    threading.Thread(
        target=_await_and_execute, args=(ctx,),
        name=f"live-approval-{ticker}", daemon=True,
    ).start()


def _live_fire_sell(ticker: str, price: float, score: int, reasoning: str) -> None:
    """LiveAgent → Alpaca close_position → portfolio remove → Telegram notify."""
    try:
        close_result = alpaca_api.close_position(ticker)
        gatekeeper.send_notification(
            f"📉 LiveAgent EXIT: {ticker}\nScore: {score} | Price ${price:.2f}\n"
            f"{close_result}\nReason: {reasoning[:200]}"
        )
        portfolio_mgr.remove_position(ticker)
        agent_memory.log_decision(ticker, "SELL", score, reasoning)
    except Exception as e:
        logger.log(f"⚠️ LiveAgent sell-close for {ticker}: {e}", level="ERROR")


def _live_get_held_tickers() -> list:
    """List currently-held ticker symbols from the portfolio table."""
    try:
        df = portfolio_mgr.get_portfolio()
        return df['Ticker'].tolist() if not df.empty else []
    except Exception:
        return []


# ─── AgentLoop (Phase 3: live reactive, replaces the 4-hour scan cycle) ──────

from live_agent import LiveAgent, is_market_hours


class AgentLoop:
    """
    Phase 3 thin wrapper. Public surface unchanged (start/stop/is_running)
    so existing Gradio buttons keep working. Under the hood: spins up a
    LiveAgent that subscribes to today's watchlist via MarketStreamer and
    reacts to live bars with cached-state evaluation.

    The old 4-hour heavy-scan cycle is retired — the overnight research
    cycle (cron-scheduled research_cycle.py) replaces it.
    """

    def __init__(self):
        self.is_running = False
        self.started_at = None
        # last_run / next_run kept for backward compat with existing UI bindings.
        self.last_run = None
        self.next_run = None

        # Streamer is shared between the existing `trigger_callback` path
        # (notable-event LLM analysis, debounced) and the new `bar_callback`
        # path (every-bar deterministic re-score for LiveAgent).
        def stream_trigger(ticker):
            logger.log(f"⚡ [Event Stream] Notable event for {ticker}, triggering deep analysis.")
            analyze_stock(ticker, is_auto=True)

        self.streamer = MarketStreamer(trigger_callback=stream_trigger)
        self.live_agent = LiveAgent(
            streamer=self.streamer,
            logger=logger,
            fire_buy_callback=_live_fire_buy,
            fire_sell_callback=_live_fire_sell,
            get_held_tickers=_live_get_held_tickers,
        )

    def start(self):
        if self.is_running:
            return "Agent is already running."

        # Market-hours gate disabled during dev — user wants to be able to start
        # the agent any time (weekends/after-hours) while iterating on the
        # system. To re-enable once the pipeline is finalized: revert this to
        #   force = os.getenv("NUROQ_FORCE_LIVE", "0") == "1"
        # which will refuse to start outside 09:30-16:00 ET on weekdays unless
        # NUROQ_FORCE_LIVE=1 is set.
        force = True
        start_msg = self.live_agent.start(force=force)
        # If the live agent refused (market closed + no force flag), surface that.
        if not self.live_agent.is_running:
            return start_msg

        self.is_running = True
        self.started_at = datetime.now()
        self.last_run = self.started_at
        # next_run is semantically obsolete for live mode; show "continuous" via UI.
        self.next_run = None
        logger.log(f"🤖 AgentLoop started in LIVE reactive mode. {start_msg}")
        return f"Agent Started (Live Reactive). {start_msg}"

    def stop(self):
        stop_msg = self.live_agent.stop()
        self.streamer.stop()
        self.is_running = False
        logger.log(f"🛑 AgentLoop stopping. {stop_msg}")
        return f"Agent Stopped. {stop_msg}"

    def status(self) -> dict:
        """Live status snapshot for the UI panel."""
        return self.live_agent.status()


# Global Agent Instance
agent = AgentLoop()

@prevent_sleep
def deep_market_scan(progress=gr.Progress()):
    """Scans ~12,000 stocks with prioritized Quant filters for top 100 movers."""
    results = []
    progress(0, desc="Fetching Market Snapshots...")
    
    target_date = get_last_trading_day()
    date_20d_ago = get_trading_day_n_ago(20)
    
    url_curr = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{target_date}?adjusted=true&apiKey={POLYGON_API_KEY}"
    url_hist = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_20d_ago}?adjusted=true&apiKey={POLYGON_API_KEY}"
    
    try:
        resp_c = requests.get(url_curr, timeout=20).json()
        resp_h = requests.get(url_hist, timeout=20).json()
        
        if "results" not in resp_c:
            return pd.DataFrame(), "⚠️ API error: No results for today."
            
        hist_prices = {item['T']: item['c'] for item in resp_h.get("results", [])}
        
        candidates = []
        for item in resp_c["results"]:
            ticker = item['T']
            price = item.get('c', 0)
            vol = item.get('v', 0)
            
            # Adjusted Filter: More inclusive but still liquid
            if price < 2 or vol < 500000: continue
            
            # Simple Trend: Price > 20D Ago
            price_20d = hist_prices.get(ticker)
            if not price_20d or price <= price_20d: continue
            
            # Momentum: Must be up at least 2% today
            change_pct = (price - item.get('o', price)) / item.get('o', 1)
            if change_pct < 0.02: continue 
            
            candidates.append(item)
        
        # PRIORITIZE: Sort by Volume and take Top 100
        candidates.sort(key=lambda x: x.get('v', 0), reverse=True)
        top_picks = candidates[:100]
        logger.log(f"🔬 Prioritizing {len(top_picks)} movers for AI analysis.")
        
    except Exception as e:
        logger.log(f"⚠️ Scan Error: {e}", level="ERROR")
        return pd.DataFrame(), f"⚠️ Scan Error: {e}"

    tickers = [item['T'] for item in top_picks]
    progress(0.1, desc="Batch fetching fundamentals + price history...")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    batch_funds   = loop.run_until_complete(get_fundamentals_batch_async(tickers, logger))
    # skip_stale=True: return stale SQLite cache instantly; inject today's bar from bulk snapshot below.
    batch_history = loop.run_until_complete(get_history_batch_async(tickers, logger, skip_stale=True))
    logger.log(f"📦 History batch complete for {len(batch_history)} tickers")

    # Sequential AI Inference (MLX/Metal is NOT thread-safe — 1 worker prevents GPU hangs)
    # Speed gain comes from pre-fetched data above, not concurrent inference.
    import concurrent.futures as _cf
    _results_lock = threading.Lock()
    _counter      = [0]

    def _deep_analyze_one(item):
        ticker = item['T']
        try:
            funds_data   = batch_funds.get(ticker)
            history_data = batch_history.get(ticker, [])

            # Inject today's bar from the bulk snapshot so indicators stay current.
            if history_data:
                snap_date = target_date
                last_cached_date = history_data[-1].get("t", "")
                if last_cached_date < snap_date:
                    today_bar = {
                        "o": item.get("o"), "h": item.get("h"),
                        "l": item.get("l"), "c": item.get("c"),
                        "v": item.get("v"), "t": snap_date,
                    }
                    history_data = history_data + [today_bar]

            data = analyze_single_ticker_data(
                ticker,
                pre_fetched_data=item,
                pre_fetched_funds=funds_data,
                pre_fetched_history=history_data,
            )
            if data and data["Score"] >= 60:
                with _results_lock:
                    results.append(data)
                    _counter[0] += 1
                    progress(0.2 + 0.8 * (_counter[0] / len(top_picks)),
                             desc=f"Analyzed {_counter[0]}/{len(top_picks)}: {ticker}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=1) as pool:
        pool.map(_deep_analyze_one, top_picks)

    if not results:
        return pd.DataFrame(), "No stocks met the conviction threshold."
        
    df = pd.DataFrame(results)
    df.sort_values(by="Score", ascending=False, inplace=True)
    return df[["Ticker", "Price", "Change %", "Score", "Rating", "Analysis"]][:30], f"✅ Deep Scan Complete. Found {len(df)} setups."

# --- 2. POSITION MONITOR ---
def run_position_monitor():
    """Background thread that refreshes portfolio prices and checks SL/TP every 60s."""
    logger.log("📡 Position Monitor thread started.")
    while True:
        try:
            portfolio_mgr.refresh_prices()
        except Exception as e:
            logger.log(f"⚠️ Position Monitor Error: {e}", level="ERROR")
        time.sleep(60)

# Start Monitor (gated by the same NUROQ_BACKGROUND_SERVICES flag as the Telegram bot)
if START_BACKGROUND_SERVICES:
    monitor_thread = threading.Thread(target=run_position_monitor, daemon=True)
    monitor_thread.start()
else:
    logger.log("ℹ️ NUROQ_BACKGROUND_SERVICES=0 — skipping position monitor.", level="INFO")

# Phase 4: News poller + Phase 4b: LLM rescore queue.
# Gated by the same flag so cron jobs don't double-poll.
news_poller = None
llm_rescore_queue = None
if START_BACKGROUND_SERVICES:
    from news_engine import NewsPoller
    from llm_queue import LLMRescoreQueue

    def _news_poller_tickers():
        wl = list(watchlist_today.get_tickers() or [])
        held = []
        try:
            df = portfolio_mgr.get_portfolio()
            held = df['Ticker'].tolist() if not df.empty else []
        except Exception:
            pass
        return wl + [t for t in held if t not in wl]

    # Phase 4b: re-score worker. Pulls fresh history from cache + runs
    # analyze_single_ticker_data which writes new AI score through to cache.
    def _rescore_ticker(ticker: str) -> None:
        history = history_cache.get(ticker, allow_stale=True) or []
        if not history:
            return
        analyze_single_ticker_data(ticker, pre_fetched_history=history)

    llm_rescore_queue = LLMRescoreQueue(run_analysis=_rescore_ticker, logger=logger)
    llm_rescore_queue.start()

    # Phase 4b: shock-detected callback. Invalidate cached score + enqueue re-run.
    # Only WARNING and BLOCK shocks invalidate; POSITIVE_BOOST decorates approvals
    # but doesn't require a re-score (the cached score is probably already optimistic).
    def _on_news_shock(ticker: str, verdict) -> None:
        if verdict.classification in ("NEGATIVE_BLOCK", "NEGATIVE_WARNING"):
            ai_score_cache.invalidate(ticker)
            llm_rescore_queue.enqueue(
                ticker, reason=f"news shock: {verdict.classification}"
            )

    news_poller = NewsPoller(
        get_tickers_fn=_news_poller_tickers,
        logger=logger,
        polygon_api_key=POLYGON_API_KEY,
        interval_seconds=int(os.getenv("NUROQ_NEWS_INTERVAL_SECONDS", "1800")),
        max_tickers_per_cycle=int(os.getenv("NUROQ_NEWS_MAX_TICKERS", "35")),
        on_shock_callback=_on_news_shock,
    )
    news_poller.start()
else:
    logger.log("ℹ️ NUROQ_BACKGROUND_SERVICES=0 — skipping news poller + rescore queue.", level="INFO")

def update_agent_status():
    """Returns status snapshot for the Agent tab (3 outputs: status_md, last_run, next_run)."""
    s = agent.status()
    if s["running"]:
        status = (
            f"### 🟢 LiveAgent Running\n"
            f"Subscribed: **{s['subscribed_tickers']}** tickers "
            f"({s['held_in_watchlist']} held positions) · "
            f"Bars processed: **{s['bars_processed']:,}**\n"
            f"BUYs fired today: **{s['buys_fired_today']}/{s['buys_cap']}** · "
            f"SELLs fired: **{s['sells_fired_today']}** · "
            f"Suppressed (cap): {s['buys_suppressed_cap']}"
        )
        started = (datetime.fromisoformat(s["started_at"]).strftime("%H:%M:%S")
                   if s.get("started_at") else "N/A")
        if s.get("latest_bar_ts"):
            last_bar = datetime.fromtimestamp(s["latest_bar_ts"]).strftime("%H:%M:%S")
        else:
            last_bar = "no bars yet"
    else:
        status = "### 🔴 LiveAgent Stopped"
        started = "N/A"
        last_bar = "N/A"

    return status, started, last_bar

def refresh_activity_log():
    return logger.get_logs()

def clear_activity_log():
    logger.clear()
    return ""

# --- 3. BUILD GRADIO UI ---
custom_theme = gr.themes.Soft(
    primary_hue="green",
    secondary_hue="teal",
    neutral_hue="slate"
).set(
    # Dark mode Midnight Neon overrides
    body_background_fill_dark="#0d0f1a",
    background_fill_primary_dark="#151828", 
    background_fill_secondary_dark="#1e2136",
    border_color_primary_dark="#2a2d45",
    button_primary_background_fill="#065f46",
    button_primary_background_fill_hover="#064e3b",
    button_primary_text_color="#ffffff",
    button_primary_background_fill_dark="#065f46",
    button_primary_background_fill_hover_dark="#064e3b",
    button_primary_text_color_dark="#ffffff",
    # Light mode Modern Green overrides
    body_background_fill="#f8fafc",
    background_fill_primary="#ffffff",
    background_fill_secondary="#f1f5f9",
)

theme_manager_js = """
function() {
    // ─── Theme bootstrap ───
    const savedTheme = localStorage.getItem('theme') || 'light';
    if (savedTheme === 'dark') {
        document.body.classList.add('dark');
    } else {
        document.body.classList.remove('dark');
        localStorage.setItem('theme', 'light');
    }

    // ─── ⌘K / Ctrl+K command palette ───
    // Lightweight palette: prompt() based, no custom modal. Recognizes:
    //   - Bare ticker (e.g. NVDA, AAPL): switches to Analyze tab and runs analysis
    //   - "start agent" / "stop agent": clicks the corresponding button
    //   - "run scan" / "scan": triggers Market Scanner
    //   - "research": triggers Research Cycle button (still gated by consent checkbox)
    //   - "refresh": clicks Refresh Today
    //   - "today" / "watchlist" / "portfolio" / "agent" / "system" / "logs": switch tabs
    function nuroqClickByText(matchers) {
        const btns = document.querySelectorAll('button');
        for (const btn of btns) {
            const t = (btn.textContent || '').trim();
            for (const m of matchers) {
                if (t.toUpperCase().includes(m.toUpperCase())) {
                    btn.click();
                    return true;
                }
            }
        }
        return false;
    }
    function nuroqSwitchTab(label) {
        const tabs = document.querySelectorAll('[role="tab"], button.tab-nav-button');
        for (const tab of tabs) {
            if ((tab.textContent || '').toUpperCase().includes(label.toUpperCase())) {
                tab.click();
                return true;
            }
        }
        return false;
    }
    // ─── Tab persistence across reloads ───
    // Remember which tab the user was on, restore on next page load. Uses
    // localStorage so it survives both refreshes and dashboard restarts.
    function nuroqWireTabPersistence() {
        const tabs = document.querySelectorAll('[role="tab"], button.tab-nav-button');
        if (tabs.length === 0) return false;
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                localStorage.setItem('nuroq.lastTab', (tab.textContent || '').trim());
            });
        });
        const saved = localStorage.getItem('nuroq.lastTab');
        if (saved) {
            for (const tab of tabs) {
                if ((tab.textContent || '').trim() === saved) {
                    tab.click();
                    break;
                }
            }
        }
        return true;
    }
    // Tabs may not be in the DOM at first script execution — retry a few times.
    let _tabRetries = 0;
    const _tabRetry = setInterval(() => {
        if (nuroqWireTabPersistence() || ++_tabRetries > 10) clearInterval(_tabRetry);
    }, 300);

    document.addEventListener('keydown', (e) => {
        if (!((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K'))) return;
        e.preventDefault();
        const input = prompt(
            'NuroQ ⌘K — type a ticker (e.g. NVDA) or command:\\n' +
            '  start agent / stop agent / run scan / research / refresh\\n' +
            '  today / analyze / watchlist / scanner / portfolio / agent / signals / system / logs'
        );
        if (!input) return;
        const cmd = input.trim().toLowerCase();
        if (!cmd) return;

        // Command routing
        if (cmd === 'start agent')   return nuroqClickByText(['START AGENT']);
        if (cmd === 'stop agent')    return nuroqClickByText(['STOP AGENT']);
        if (cmd === 'run scan' || cmd === 'scan') return nuroqClickByText(['RUN SCAN']);
        if (cmd === 'research')      return nuroqClickByText(['Run Research Cycle']);
        if (cmd === 'refresh')       return nuroqClickByText(['Refresh Today']);

        // Tab navigation shortcuts
        const tabMap = {
            'today': '📈 Today', 'analyze': '📊 Analyze',
            'watchlist': '🎯 Watchlist', 'scanner': '🔍 Scanner',
            'portfolio': '💼 Portfolio', 'agent': '🤖 Agent',
            'signals': '📜 Signal', 'system': '🩺 System', 'logs': '📝 Logs',
            'health': '🩺 System',
        };
        if (cmd in tabMap) return nuroqSwitchTab(tabMap[cmd]);

        // Otherwise: treat as ticker. Switch to Analyze, fill input, click RUN ANALYSIS.
        const looksLikeTicker = /^[A-Za-z\\.\\-]{1,6}$/.test(cmd);
        if (looksLikeTicker) {
            nuroqSwitchTab('📊 Analyze');
            setTimeout(() => {
                const inputs = document.querySelectorAll('textarea, input[type="text"]');
                for (const inp of inputs) {
                    const ph = (inp.placeholder || '').toLowerCase();
                    if (ph.includes('ticker') || ph.includes('nvda')) {
                        const native = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value') ||
                                       Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
                        native.set.call(inp, cmd.toUpperCase());
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        break;
                    }
                }
                setTimeout(() => nuroqClickByText(['RUN ANALYSIS']), 200);
            }, 250);
            return;
        }
        alert('NuroQ ⌘K — unrecognized command: ' + input);
    });
}
"""

# Light CSS polish: tighter section spacing, sticky top bar feel, hover affordance
# on table rows (we wire row clicks to drill-down later), nicer empty-state cards.
NUROQ_CSS = """
/* ════════════════════════════════════════════════════════════════════════
   NuroQ density pass — Bloomberg-style information density. Gradio defaults
   target casual data-science demos (huge padding, big fonts, lots of air).
   For a trading tool we want every pixel earning its keep.
   ════════════════════════════════════════════════════════════════════════ */

/* ── Container & base typography ──────────────────────────────────────── */
.gradio-container {
    max-width: 1700px !important;
    padding: 8px 16px !important;
    font-size: 13px !important;
}
.gradio-container * { font-size: inherit; }

/* Headings: trim default 1.5em+ margins down to financial-app sizing */
.gradio-container h1 { font-size: 1.4em !important; margin: 4px 0 !important; line-height: 1.2; }
.gradio-container h2 { font-size: 1.2em !important; margin: 6px 0 4px !important; line-height: 1.2; }
.gradio-container h3 { font-size: 1.05em !important; margin: 8px 0 4px !important; line-height: 1.2; }
.gradio-container h4 { font-size: 0.95em !important; margin: 4px 0 2px !important; }
.gradio-container p  { margin: 4px 0 !important; line-height: 1.45; }
.gradio-container ul, .gradio-container ol { margin: 4px 0 !important; padding-left: 20px; }
.gradio-container li { margin: 2px 0 !important; }

/* ── Block/row/column spacing — the biggest whitespace win ───────────── */
.block, .form { padding: 4px !important; gap: 4px !important; }
.gap { gap: 6px !important; }
.gap-sm { gap: 4px !important; }
div[class*="row"] > div { padding: 2px !important; }
.column { gap: 4px !important; }

/* Remove the giant default margin Gradio puts around every block */
.block.padded { padding: 4px !important; }
.svelte-vt1mxs { margin: 0 !important; padding: 4px !important; }

/* ── Inputs & buttons: tighter, more keyboard-app feel ────────────────── */
input, textarea, select {
    font-size: 13px !important;
    padding: 4px 8px !important;
    min-height: 30px !important;
}
.gradio-container button {
    padding: 4px 12px !important;
    min-height: 30px !important;
    font-size: 13px !important;
    font-weight: 500;
}
.gradio-container button.lg, .gradio-container button[size="lg"] { min-height: 36px !important; }
.gradio-container button.sm, .gradio-container button[size="sm"] {
    min-height: 26px !important; padding: 2px 10px !important; font-size: 12px !important;
}

/* Labels — Gradio's defaults are oversized */
label, .label-wrap {
    font-size: 12px !important;
    font-weight: 500 !important;
    margin-bottom: 2px !important;
}

/* ── Tabs — slim strip, dense ─────────────────────────────────────────── */
.tab-nav { padding: 0 4px !important; gap: 0 !important; }
.tab-nav button {
    padding: 6px 12px !important;
    font-size: 13px !important;
    min-height: 32px !important;
    border-radius: 4px 4px 0 0 !important;
}

/* Sticky tab strip (already present, refined) */
.tab-nav {
    position: sticky !important;
    top: 0 !important;
    z-index: 50 !important;
    background: var(--background-fill-primary) !important;
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border-color-primary);
}
.dark .tab-nav { background: rgba(21, 24, 40, 0.92) !important; }

/* ── Data tables — Bloomberg-style dense ──────────────────────────────── */
.gradio-container table {
    font-size: 12px !important;
    border-collapse: collapse !important;
}
.gradio-container th {
    padding: 4px 8px !important;
    font-weight: 600 !important;
    font-size: 11px !important;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    background: var(--background-fill-secondary);
}
.gradio-container td {
    padding: 3px 8px !important;
    line-height: 1.3 !important;
}
table tbody tr { cursor: pointer; }
table tbody tr:hover { background: rgba(16, 185, 129, 0.08) !important; }
table tbody tr:nth-child(even) { background: rgba(148, 163, 184, 0.04); }
.dark table tbody tr:nth-child(even) { background: rgba(148, 163, 184, 0.06); }

/* ── Accordion: less padding ──────────────────────────────────────────── */
.gradio-container .accordion {
    padding: 4px !important;
    margin: 4px 0 !important;
}
.gradio-container details > summary {
    padding: 6px 10px !important;
    font-size: 13px !important;
    font-weight: 500;
}

/* ── Forms (Markdown bodies) ──────────────────────────────────────────── */
.prose, .markdown { padding: 0 !important; }
.gradio-container .prose p { margin: 4px 0 !important; }

/* ── Plot container — kill the giant default margin ──────────────────── */
.gradio-container .plot-container { padding: 4px !important; }
.gradio-container .js-plotly-plot { margin: 0 !important; }

/* ── Image (logo) — keep small ────────────────────────────────────────── */
.gradio-container .image-container img { max-height: 64px !important; }

/* ── Checkbox / radio — inline-friendly ──────────────────────────────── */
.gradio-container .checkbox-wrap, .gradio-container .radio-wrap {
    padding: 4px 6px !important;
    min-height: 28px !important;
}

/* ── kbd hint pills ───────────────────────────────────────────────────── */
kbd {
    background: #e2e8f0; color: #1e293b; padding: 1px 5px; border-radius: 4px;
    font-size: 0.85em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border: 1px solid #cbd5e1;
}
.dark kbd { background: #334155; color: #e2e8f0; border-color: #475569; }

/* ── Empty state card — tighter ───────────────────────────────────────── */
.nuroq-empty-state {
    text-align: center; padding: 24px 16px; opacity: 0.75;
    border: 1px dashed #cbd5e1; border-radius: 6px; margin: 8px 0;
}
.dark .nuroq-empty-state { border-color: #475569; }
.nuroq-empty-state h3 { margin: 0 0 6px !important; }

/* ── Live-pulse dot ───────────────────────────────────────────────────── */
@keyframes nuroq-pulse {
    0%, 100% { opacity: 0.9; }
    50% { opacity: 0.3; }
}
.nuroq-live-dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #10b981; margin-right: 5px;
    animation: nuroq-pulse 2s ease-in-out infinite;
}

/* ── NuroQ header — compact one-line brand row ────────────────────────── */
.nuroq-brand {
    display: flex; align-items: center; gap: 12px;
    padding: 4px 0;
}
.nuroq-brand h2 {
    margin: 0 !important;
    font-size: 1.1em !important;
    font-weight: 600;
}
.nuroq-brand .tagline {
    font-size: 11px;
    opacity: 0.7;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.nuroq-brand .hint {
    margin-left: auto;
    font-size: 11px;
    opacity: 0.6;
}

/* ── Hero P&L number — restore prominence after font shrink ───────────── */
.nuroq-hero-pl {
    font-size: 2.4em !important;
    font-weight: 700;
    line-height: 1.05;
    margin: 4px 0 !important;
}

/* ── Section separators — subtle horizontal rules instead of huge margin ── */
.gradio-container hr {
    margin: 6px 0 !important;
    border: none;
    border-top: 1px solid var(--border-color-primary);
    opacity: 0.5;
}
"""

toggle_theme_js = """
function() {
    if (document.body.classList.contains('dark')) {
        document.body.classList.remove('dark');
        localStorage.setItem('theme', 'light');
        return "🌙 Switch to Dark Mode";
    } else {
        document.body.classList.add('dark');
        localStorage.setItem('theme', 'dark');
        return "☀️ Switch to Light Mode";
    }
}
"""

with gr.Blocks() as demo:
    # ── Compact single-row header: brand left, controls right ──────────────
    # The previous header used 3 columns + a stacked status block on the right
    # that ate ~140px of vertical space. New layout: brand + tagline in a slim
    # 32px row, controls inline on the right, consent + status moved inline.
    with gr.Row(equal_height=True):
        with gr.Column(scale=1, min_width=60):
            gr.Image("nuroq_logo.png", show_label=False, container=False,
                     width=48, height=48)
        with gr.Column(scale=5):
            gr.HTML(
                "<div class='nuroq-brand'>"
                "<h2>🧠 NuroQ <span class='tagline'>· Frontier Neural Quant · "
                "Neural · Ensemble · Sovereign Agent</span></h2>"
                "<span class='hint'>Press <kbd>⌘K</kbd> or <kbd>Ctrl+K</kbd> to search</span>"
                "</div>"
            )
        with gr.Column(scale=2, min_width=280):
            with gr.Row(equal_height=True):
                refresh_cache_btn = gr.Button(
                    "🔬 Run Research Cycle", size="sm", variant="primary", scale=3,
                )
                refresh_status_btn = gr.Button("↻", size="sm", scale=0, min_width=40)
                theme_toggle_btn = gr.Button("🌙", size="sm", scale=0, min_width=40)
            with gr.Row(equal_height=True):
                research_consent = gr.Checkbox(
                    label="I understand this takes ~9 min",
                    value=False, container=False, scale=3,
                )
            cache_status_md = gr.Markdown(_research_status_text())

    refresh_cache_btn.click(
        confirm_research_cycle, inputs=[research_consent],
        outputs=[cache_status_md, research_consent],
    )
    refresh_status_btn.click(_research_status_text, outputs=[cache_status_md])

    # ── Persistent system bar: status pills + Alpaca strip + refresh ──────
    # Two tight rows, no padding columns. Single-glance health and account
    # state across every tab.
    with gr.Row(equal_height=True):
        with gr.Column(scale=10):
            status_pills_md = gr.Markdown(render_status_pills())
        with gr.Column(scale=0, min_width=120):
            refresh_alpaca_btn = gr.Button("🔄 Refresh", size="sm")
    with gr.Row():
        alpaca_panel = gr.Markdown(render_alpaca_panel())
    refresh_alpaca_btn.click(
        lambda: (render_alpaca_panel(), render_status_pills()),
        outputs=[alpaca_panel, status_pills_md],
    )

    # ── GLOBAL QUICK TRADE BAR (Alpaca Paper) ──────────────────────────────
    # Two compact rows. Row 1: instrument + sizing + order params + buttons.
    # Row 2: SL/TP (always) + Limit/Stop (visible only for those order types).
    # Status line under both.
    with gr.Accordion("⚡ Quick Trade — collapse to hide", open=True):
        with gr.Row(equal_height=True):
            qt_ticker = gr.Textbox(label="Ticker", placeholder="AAPL", scale=2, min_width=100)
            qt_shares = gr.Number(label="Shares", value=1, scale=1, min_width=80)
            qt_type = gr.Dropdown(
                choices=["Market", "Limit", "Stop", "Stop Limit", "Trailing Stop"],
                value="Market", label="Type", scale=2, min_width=110,
            )
            qt_tif = gr.Dropdown(
                choices=["Day", "GTC", "OPG", "IOC", "FOK"],
                value="GTC", label="TIF", scale=1, min_width=80,
            )
            qt_buy = gr.Button("🟢 BUY", variant="primary", scale=1, min_width=90)
            qt_sell = gr.Button("🔴 SELL", variant="stop", scale=1, min_width=90)

        with gr.Row(equal_height=True):
            qt_sl = gr.Number(label="Stop Loss ($)", visible=True, scale=1, min_width=110)
            qt_tp = gr.Number(label="Take Profit ($)", visible=True, scale=1, min_width=110)
            qt_limit = gr.Number(label="Limit Price ($)", visible=False, scale=1, min_width=110)
            qt_stop = gr.Number(label="Stop / Trail ($)", visible=False, scale=1, min_width=110)

        qt_status = gr.Markdown(
            "<sub>Fill SL + TP on a Market order for a bracket; blank = naked single order.</sub>"
        )

        # Dynamic visibility logic
        def update_price_inputs(order_type):
            show_limit = order_type in ["Limit", "Stop Limit"]
            show_stop = order_type in ["Stop", "Stop Limit", "Trailing Stop"]
            show_bracket = order_type == "Market"  # bracket only valid for Market entries

            buy_text = f"🟢 BUY {order_type.upper()}"
            sell_text = f"🔴 SELL {order_type.upper()}"

            return (
                gr.update(visible=show_limit),
                gr.update(visible=show_stop),
                gr.update(value=buy_text),
                gr.update(value=sell_text),
                gr.update(visible=show_bracket),
                gr.update(visible=show_bracket),
            )

        qt_type.change(
            update_price_inputs,
            inputs=[qt_type],
            outputs=[qt_limit, qt_stop, qt_buy, qt_sell, qt_sl, qt_tp]
        )

    # Recent-symbols memory for the Analyze tab (per-session).
    recent_symbols = gr.State([])

    # Tabs container — each TabItem has an explicit `id=` so drill-down
    # handlers can programmatically switch via `return gr.Tabs(selected="...")`.
    tabs = gr.Tabs()
    with tabs:
        # ── 📈 Today — landing tab, single-glance "what's happening now" ─────
        with gr.TabItem("📈 Today", id="today"):
            with gr.Row():
                refresh_today_btn = gr.Button(
                    "↻ Refresh Today", size="sm", variant="primary", scale=0,
                )
                auto_refresh_chk = gr.Checkbox(
                    label="Auto-refresh every 60s", value=True, container=False, scale=0,
                )
                gr.Markdown(
                    "<sub><span class='nuroq-live-dot'></span>Live updates active</sub>",
                    container=False,
                )
            today_hero_md = gr.Markdown(render_today_hero())
            today_cards_md = gr.Markdown(render_today_cards())
            today_next_md = gr.Markdown(render_next_actions())
            today_feed_md = gr.Markdown(render_channel_bar())

            refresh_today_btn.click(
                refresh_today_view,
                outputs=[status_pills_md, today_hero_md, today_cards_md,
                         today_next_md, today_feed_md],
            )

            # Auto-refresh: gr.Timer ticks every N seconds when active. The
            # checkbox toggles `active` so users can pause refreshes if they're
            # reading the feed and don't want it to repaint under them.
            today_timer = gr.Timer(60, active=True)
            today_timer.tick(
                refresh_today_view,
                outputs=[status_pills_md, today_hero_md, today_cards_md,
                         today_next_md, today_feed_md],
            )
            auto_refresh_chk.change(
                lambda v: gr.Timer(active=bool(v)),
                inputs=[auto_refresh_chk], outputs=[today_timer],
            )

        # ── 📊 Analyze — chart-first 60/40 layout, recent-symbols quick pick ─
        with gr.TabItem("📊 Analyze", id="analyze"):
            with gr.Row(equal_height=True):
                ticker_in = gr.Textbox(
                    label=None, placeholder="Enter ticker (e.g. NVDA, AAPL) — or press ⌘K",
                    scale=4, min_width=120, container=False,
                )
                recent_dd = gr.Dropdown(
                    choices=[], value=None, label=None, container=False,
                    scale=2, min_width=140, allow_custom_value=False,
                    info=None,
                )
                an_btn = gr.Button("🔍 RUN ANALYSIS", variant="primary", scale=1, min_width=140)

            # Chart-first 60/40: chart breathes on the left, supporting context
            # stacks on the right as tabs (Signal / AI / Trade Setup) instead
            # of competing for equal-weight quadrants.
            with gr.Row(equal_height=True):
                with gr.Column(scale=3):
                    cht = gr.Plot(label="📊 Price Chart — SMA20 & Bollinger Bands")
                with gr.Column(scale=2):
                    with gr.Tabs():
                        with gr.TabItem("📊 Signal"):
                            out_md = gr.Markdown("*Run analysis to see the signal card.*")
                        with gr.TabItem("🧠 AI Reasoning"):
                            cot_md_out = gr.Markdown("*AI reasoning will appear here after analysis.*")
                        with gr.TabItem("💰 Trade Setup"):
                            setup_md_out = gr.Markdown("*Trade setup will appear here after analysis.*")
                            send_to_qt_btn = gr.Button(
                                "⚡ Send to Quick Trade", variant="primary", size="sm",
                            )
                            qt_prefill_status = gr.Markdown("*Click to pre-fill the Quick Trade bar above with sized SL/TP.*")

        # ── 🎯 Watchlist — drill-down enabled, HOLDs open by default ─────────
        with gr.TabItem("🎯 Watchlist", id="watchlist"):
            wl_header_md = gr.Markdown(render_watchlist_header())
            with gr.Row():
                refresh_wl_btn = gr.Button("🔄 Refresh Watchlist", variant="primary", size="sm")
            gr.Markdown(
                "<sub>💡 Click any row to drill into the Analyze tab with that ticker preloaded.</sub>"
            )

            gr.Markdown("### 🟢 BUY Signals")
            wl_buy_table = gr.DataFrame(
                value=render_watchlist_buys(),
                headers=WATCHLIST_COLS, wrap=True, interactive=False,
            )

            # HOLDs accordion now open by default — these are the candidates
            # the live agent is watching for HOLD→BUY crossings; hiding them
            # made the agent's whole purpose invisible.
            with gr.Accordion("👀 Watching for crossings — HOLDs near threshold "
                              "(LiveAgent monitors these for HOLD→BUY transitions during the session)",
                              open=True):
                wl_hold_table = gr.DataFrame(
                    value=render_watchlist_holds(),
                    headers=WATCHLIST_COLS, wrap=True, interactive=False,
                )

            refresh_wl_btn.click(
                refresh_watchlist_view,
                outputs=[wl_header_md, wl_buy_table, wl_hold_table],
            )

        # ── 🔍 Scanner — merged: Top-20 + Global Deep Scan via Radio ─────────
        with gr.TabItem("🔍 Scanner", id="scanner"):
            with gr.Row():
                sc_mode = gr.Radio(
                    choices=["Top 20 Universe", "Global Deep Scan"],
                    value="Top 20 Universe",
                    label="Scan Mode", scale=3,
                )
                sc_btn = gr.Button("RUN SCAN", variant="primary", scale=1, min_width=140)
            sc_sum = gr.Markdown(
                "Pick a mode and click RUN SCAN. **Top 20** = curated universe "
                "(~30s). **Global Deep Scan** = full market sweep (~3 min, GPU-bound)."
            )
            sc_tbl = gr.DataFrame(
                headers=["Ticker", "Price", "Change %", "Score", "Rating", "Analysis"],
                wrap=True,
            )

        # ── 💼 Portfolio — drill-down enabled ────────────────────────────────
        with gr.TabItem("💼 Portfolio", id="portfolio"):
            with gr.Row():
                refresh_p_btn = gr.Button("🔄 Refresh Portfolio & Prices", variant="primary", size="sm")
                remove_p_btn = gr.Button("🗑️ Remove Selection (input ticker below)", variant="secondary", size="sm")
            ticker_remove = gr.Textbox(label="Ticker to Remove", placeholder="e.g. AAPL")
            gr.Markdown("<sub>💡 Click any row to drill into Analyze.</sub>")
            p_tbl = gr.DataFrame(headers=portfolio_mgr.cols, interactive=False)

        # ── 🤖 Agent — start/stop, banner, status, log ───────────────────────
        with gr.TabItem("🤖 Agent", id="agent"):
            with gr.Row():
                st_a_btn = gr.Button("▶️ START AGENT", variant="primary")
                sp_a_btn = gr.Button("⏹️ STOP AGENT", variant="stop")
            ag_msg = gr.Markdown("*Press START AGENT to launch the live reactive loop.*")
            ag_st = gr.Markdown("### 🔴 LiveAgent Stopped")
            with gr.Row():
                ag_last = gr.Textbox(label="Started At")
                ag_next = gr.Textbox(label="Last Bar")
            gr.Markdown("<sub>💡 Click any row to drill into Analyze.</sub>")
            ag_log = gr.DataFrame(headers=["Timestamp", "Ticker", "Action", "Price", "Shares", "Total"])

        # ── 📜 Signal History — renamed from "Signals" for clarity ───────────
        with gr.TabItem("📜 Signal History", id="signals"):
            gr.Markdown("### 📜 Universal Signal History\n"
                        "*Every equity the system has analyzed, ranked by recency.*")
            refresh_sig_btn = gr.Button("🔄 Refresh", variant="primary", size="sm")
            gr.Markdown("<sub>💡 Click any row to drill into Analyze.</sub>")
            signals_table = gr.DataFrame(
                headers=["Timestamp", "Ticker", "Name", "Industry", "Price",
                         "Technicals", "Fundamentals", "Signal", "AI Score", "Quant Score"],
                wrap=True,
            )

        # ── 🩺 System (renamed from Health) ──────────────────────────────────
        with gr.TabItem("🩺 System", id="system"):
            health_md = gr.Markdown(render_health_snapshot())
            refresh_health_btn = gr.Button("🔄 Refresh System Snapshot", variant="primary", size="sm")
            refresh_health_btn.click(render_health_snapshot, outputs=[health_md])

        # ── 📝 Logs (renamed from Activity Log) ──────────────────────────────
        with gr.TabItem("📝 Logs", id="logs"):
            with gr.Row():
                ref_l_btn = gr.Button("🔄 Refresh", size="sm")
                clr_l_btn = gr.Button("🗑️ Clear", size="sm")
            act_disp = gr.Textbox(label="Logs", lines=25, autoscroll=True)

    # ── Event Bindings ─────────────────────────────────────────────────────

    def handle_remove(t):
        portfolio_mgr.remove_position(t)
        return portfolio_mgr.get_portfolio()

    # Quick Trade (unchanged contract)
    qt_buy.click(
        handle_quick_trade,
        inputs=[qt_ticker, qt_shares, gr.State("buy"), qt_type, qt_tif,
                qt_limit, qt_stop, qt_sl, qt_tp],
        outputs=[qt_status],
    )
    qt_sell.click(
        handle_quick_trade,
        inputs=[qt_ticker, qt_shares, gr.State("sell"), qt_type, qt_tif,
                qt_limit, qt_stop, qt_sl, qt_tp],
        outputs=[qt_status],
    )

    # Analyze: wraps analyze_stock to also push the ticker into the
    # recent-symbols list and refresh the dropdown choices.
    an_btn.click(
        analyze_and_remember,
        inputs=[ticker_in, recent_symbols],
        outputs=[out_md, cot_md_out, cht, setup_md_out, recent_symbols, recent_dd],
    )
    # Picking a recent symbol fills the ticker input but doesn't auto-run.
    recent_dd.change(recent_selected, inputs=[recent_dd], outputs=[ticker_in])

    # Send-to-Quick-Trade: pre-fill the QT bar from the currently-analyzed ticker.
    send_to_qt_btn.click(
        send_to_quick_trade, inputs=[ticker_in],
        outputs=[qt_ticker, qt_shares, qt_sl, qt_tp],
    ).then(
        lambda t: f"✅ Quick Trade pre-filled for **{t}**. Review SL/TP and click 🟢 BUY above." if t else "*Enter a ticker first.*",
        inputs=[ticker_in], outputs=[qt_prefill_status],
    )

    # Scanner: single dispatcher for both modes via the Radio selector.
    sc_btn.click(run_scanner, inputs=[sc_mode], outputs=[sc_tbl, sc_sum])

    # Refreshers
    refresh_sig_btn.click(portfolio_mgr.get_buy_signals, None, signals_table)
    refresh_p_btn.click(portfolio_mgr.refresh_prices, None, p_tbl)
    remove_p_btn.click(handle_remove, ticker_remove, p_tbl)

    # Agent lifecycle — surface response in banner + refresh status panel + top pills
    st_a_btn.click(agent.start, outputs=[ag_msg]).then(
        update_agent_status, outputs=[ag_st, ag_last, ag_next]
    ).then(render_status_pills, outputs=[status_pills_md])
    sp_a_btn.click(agent.stop, outputs=[ag_msg]).then(
        update_agent_status, outputs=[ag_st, ag_last, ag_next]
    ).then(render_status_pills, outputs=[status_pills_md])

    ref_l_btn.click(refresh_activity_log, outputs=act_disp)
    clr_l_btn.click(clear_activity_log, outputs=act_disp)
    theme_toggle_btn.click(None, None, theme_toggle_btn, js=toggle_theme_js)

    # ── Drill-down wiring: row-click on any data table → Analyze tab ────────
    # Every drill-down handler returns (ticker, gr.Tabs(selected="analyze"))
    # which both populates the Analyze input AND switches the active tab.
    wl_buy_table.select(
        drill_to_analyze, inputs=[wl_buy_table], outputs=[ticker_in, tabs],
    )
    wl_hold_table.select(
        drill_to_analyze, inputs=[wl_hold_table], outputs=[ticker_in, tabs],
    )
    p_tbl.select(
        drill_to_analyze, inputs=[p_tbl], outputs=[ticker_in, tabs],
    )
    signals_table.select(
        drill_to_analyze, inputs=[signals_table], outputs=[ticker_in, tabs],
    )
    sc_tbl.select(
        drill_to_analyze, inputs=[sc_tbl], outputs=[ticker_in, tabs],
    )
    ag_log.select(
        drill_to_analyze, inputs=[ag_log], outputs=[ticker_in, tabs],
    )
    
    gr.Markdown("---")
    gr.Markdown("*Disclaimer: This is an AI tool for educational purposes and pattern recognition, not financial advice.*")

if __name__ == "__main__":
    kill_port(7860)

    # Safe defaults: bind to localhost, no public tunnel.
    # Override via env: GRADIO_SHARE=1, GRADIO_SERVER_NAME=0.0.0.0, GRADIO_USER + GRADIO_PASSWORD.
    share       = os.getenv("GRADIO_SHARE", "0") == "1"
    server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    gr_user     = os.getenv("GRADIO_USER")
    gr_pass     = os.getenv("GRADIO_PASSWORD")
    auth        = (gr_user, gr_pass) if (gr_user and gr_pass) else None

    if share and not auth:
        logger.log("⚠️ GRADIO_SHARE=1 but no GRADIO_USER/GRADIO_PASSWORD set — "
                   "the public tunnel will be unauthenticated.", level="WARNING")

    # theme/js/css moved here per Gradio 6 — Blocks() constructor no longer
    # accepts them.
    demo.launch(
        inbrowser=False,
        server_port=7860,
        server_name=server_name,
        share=share,
        auth=auth,
        theme=custom_theme,
        js=theme_manager_js,
        css=NUROQ_CSS,
    )


