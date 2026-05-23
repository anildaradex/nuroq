import gradio as gr
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
                    except: continue
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

def handle_quick_trade(ticker, shares, action, order_type, tif, limit_price, stop_price):
    if not ticker or shares is None or shares <= 0:
        return "⚠️ Please enter a valid ticker and a share amount greater than 0."
        
    # Validate required prices based on order type
    if order_type in ["Limit", "Stop Limit"] and not limit_price:
        return f"⚠️ {order_type} orders require a Limit Price."
    if order_type in ["Stop", "Stop Limit", "Trailing Stop"] and not stop_price:
        return f"⚠️ {order_type} orders require a Stop Price (or Trailing Value)."
        
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

    # 2. Build Prompt
    mem_ctx = agent_memory.get_past_context(ticker)
    prompt = f"""### Instruction: Act as a Hedge Fund Analyst. Analyze using Technicals and Fundamentals.
    1. Technicals: {techs['trend']} Trend, RSI Flag: {techs.get('semantic_rsi', 'NEUTRAL')}, Bollinger Flag: {techs.get('semantic_bb', 'NEUTRAL')}
    2. Fundamentals: PE Ratio {funds['pe']}, Rev Growth {funds['growth']}.
    3. Context: {funds['news'][:500]}
    4. Memory: {mem_ctx}

    Provide your analysis as a CONCISE JSON object with the following fields:
    - "reasoning": "A concise explanation of your findings"
    - "considerations": ["Factor 1", "Factor 2", "Factor 3"]
    - "rating": "BUY/SELL/HOLD"
    - "score": 0-100 (Conviction score)

    Ensure the response is valid JSON only. Do not repeat the output."""
    prompt += f" ### Input: Ticker: {ticker}, Close: ${techs['price']} ### Response:"
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
    
    # Calculate additional metrics for Hybrid Quant Score
    # We use fast/local versions to keep the scan snappy
    w_trend = get_weekly_confluence(history) if history else "UNKNOWN"
    e_risk = get_earnings_risk(ticker) if not pre_fetched_data else {"risk": False, "days": 30} # Estimate if pre-fetched
    st_sent = get_sentiment(ticker) if not pre_fetched_data else "Neutral"
    
    final_score = calculate_quant_score(techs, funds, w_trend, e_risk, st_sent, score)
    
    # Consistency Check: Rating follows Quant Score
    if final_score >= 75: rating = "BUY"
    elif final_score <= 40: rating = "SELL"
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
    prompt = f"""### Instruction: You are a Senior Hedge Fund Quant Analyst. Produce a structured, institutional-quality stock analysis.

    GROUNDING CONTEXT (SEC Filings & News):
    {rag_context}

    MARKET DATA:
    - Price: ${techs['price']} ({'+' if techs['change']>=0 else ''}{techs['change']}% today)
    - Technical Signal (Daily): {techs['trend']} Trend | RSI={techs['rsi']} ({techs.get('semantic_rsi','NEUTRAL')}) | %B={techs['percent_b']} ({techs.get('semantic_bb','NEUTRAL')})
    - Technical Signal (Weekly): {w_trend} Trend
    - Momentum: RelVol={techs['rel_vol']}x | Volatility={techs['volatility']}% | ATR={techs['atr']}
    - Fundamentals: Trailing P/E={funds['pe']} | Forward P/E={funds['f_pe']} | Rev Growth={funds['growth']}
    - Macro Context: Social Sentiment={st_sent} | Earnings Risk={'HIGH — within 5 days' if e_risk['risk'] else 'LOW'}
    - Recent Headlines: {funds['news'][:400]}
    - Agent Memory (Past Decisions): {mem_ctx}

    Respond ONLY with a single valid JSON object using this exact schema:
    {{"reasoning": "2-3 sentence synthesis covering technical setup, fundamental quality, and key risk",
      "bull_case": "strongest argument FOR entering",
      "bear_case": "strongest argument AGAINST entering",
      "key_risk": "single biggest risk to the thesis",
      "considerations": ["factor1", "factor2", "factor3"],
      "rating": "BUY|SELL|HOLD",
      "score": <0-100>}}"""
    prompt += f" ### Input: {ticker.upper()} @ ${techs['price']} ### Response:"

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

    # STEP 6: Quant Scoring & Final Recommendation
    status_msg += "\n✅ Step 6: Calculating Hybrid Quant Score..."
    logger.log(f"{tag} Step 6/{total_steps}: Merging AI analysis with objective Quant metrics...")
    
    # Calculate the new objective score
    final_score = calculate_quant_score(techs, funds, w_trend, e_risk, st_sent, score)
    
    # Consistency Check: Rating must follow the Quant Score
    if final_score >= 75: recommendation = "BUY"
    elif final_score <= 40: recommendation = "SELL"
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
    sizing = calculate_sizing(techs['price'], techs['atr'])
    gate_threshold = 90 if e_risk['risk'] else 80
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
    gate_threshold = 90 if e_risk['risk'] else 80
    
    is_buy = recommendation == "BUY"
    is_sell = recommendation == "SELL"
    
    should_trigger_buy = is_buy and final_score >= gate_threshold and is_consensus
    should_trigger_sell = is_sell and in_portfolio
    
    if should_trigger_sell:
        # Automate SELL signals: Notify and Execute immediately
        logger.log(f"📉 Auto-Executing SELL for {ticker} (Score: {final_score}).")
        gatekeeper.send_notification(f"📉 [PORTFOLIO EXIT] {ticker.upper()}\nScore: {final_score}\nPrice: ${techs['price']}\nReason: {reasoning[:200]}")
        
        portfolio_mgr.remove_position(ticker.upper())
        output_rec += f"\n\n✅ [PORTFOLIO EXIT] Simulated SELL of {ticker.upper()} at ${techs['price']}."
        agent_memory.log_decision(ticker.upper(), recommendation, final_score, reasoning)
        
    elif should_trigger_buy:
        # BUY signals still require approval
        logger.log(f"🎯 Gating Passed for {ticker} (Threshold: {gate_threshold}). Requesting Telegram approval...")
        output_rec += f"\n\n📡 **Action Required! (Earnings Risk: {'HIGH' if e_risk['risk'] else 'LOW'})**"
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                gatekeeper.request_approval(ticker.upper(), techs['price'], final_score, reasoning),
                gatekeeper.loop
            )
            decision = future.result(timeout=305)
            logger.log(f"📱 Telegram Decision for {ticker}: {decision}")
            
            if decision == "EXECUTE":
                sizing = calculate_sizing(techs['price'], techs['atr'])
                exec_result = executor.execute_trade(ticker.upper(), techs['price'], amount=sizing['amount'])
                portfolio_mgr.add_position(ticker.upper(), sizing['shares'], techs['price'], 
                                               sl=sizing['sl'], tp=sizing['tp'], score=final_score, rating=recommendation)
                output_rec += f"\n\n{exec_result}\nStop Loss: ${sizing['sl']} | Take Profit: ${sizing['tp']}"
                logger.log(f"✅ Executed BUY for {ticker}: {sizing['shares']} shares")
                agent_memory.log_decision(ticker.upper(), recommendation, final_score, reasoning)
            else:
                output_rec += f"\n\n🛑 Action {decision}."
        except Exception as e:
            logger.log(f"⚠️ Approval Error for {ticker}: {e}", level="ERROR")
            output_rec += f"\n\n⚠️ Approval Error: {e}"

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

# Start Telegram Bot in Background
threading.Thread(target=run_bot_background, args=(gatekeeper,), daemon=True).start()

class AgentLoop:
    def __init__(self, interval_hours=4):
        self.interval_hours = interval_hours
        self.is_running = False
        self.last_run = None
        self.next_run = None
        self.thread = None
        self._stop_event = threading.Event()
        
        # Initialize the Event Streamer
        def stream_trigger(ticker):
            logger.log(f"⚡ [Event Stream] Triggering analysis for {ticker}")
            analyze_stock(ticker, is_auto=True)
            
        self.streamer = MarketStreamer(trigger_callback=stream_trigger)

    def start(self):
        if self.is_running:
            return "Agent is already running."
        self.is_running = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._main_loop, daemon=True)
        self.thread.start()
        self.streamer.start()
        logger.log("🤖 Autonomous Agent & Event Stream Started.")
        return "Agent Started."

    def stop(self):
        self.is_running = False
        self._stop_event.set()
        self.streamer.stop()
        logger.log("🛑 Autonomous Agent Stopping...")
        return "Agent Stopped."

    def _main_loop(self):
        while not self._stop_event.is_set():
            self.last_run = datetime.now()
            self.next_run = self.last_run + timedelta(hours=self.interval_hours)
            
            try:
                self._run_cycle()
            except Exception as e:
                logger.log(f"❌ Agent Cycle Error: {e}", level="ERROR")
            
            logger.log(f"💤 Agent sleeping. Next run at {self.next_run.strftime('%H:%M:%S')}")
            # Sleep in small increments to allow for stopping
            wait_time = self.interval_hours * 3600
            for _ in range(int(wait_time)):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _run_cycle(self):
        logger.log("🚀 Starting Autonomous Scan Cycle...")
        # 1. Reuse deep_market_scan logic (internal version)
        target_date = get_last_trading_day()
        date_20d_ago = get_trading_day_n_ago(20)
        
        rate_limiter.wait()
        url_curr = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{target_date}?adjusted=true&apiKey={POLYGON_API_KEY}"
        rate_limiter.wait()
        url_hist = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_20d_ago}?adjusted=true&apiKey={POLYGON_API_KEY}"
        
        resp_c = requests.get(url_curr, timeout=20).json()
        resp_h = requests.get(url_hist, timeout=20).json()
        
        if "results" not in resp_c:
            logger.log("⚠️ No market results for today, skipping cycle.", level="WARNING")
            return
            
        hist_prices = {item['T']: item['c'] for item in resp_h.get("results", [])}
        
        candidates = []
        for item in resp_c["results"]:
            ticker = item['T']
            price = item.get('c', 0)
            vol = item.get('v', 0)
            
            # AppVision Filter: Liquid and Momentum
            if price < 5 or vol < 1000000: continue
            
            price_20d = hist_prices.get(ticker)
            if not price_20d or price <= price_20d: continue
            
            change_pct = (price - item.get('o', price)) / item.get('o', 1)
            if change_pct < 0.02: continue 
            
            candidates.append(item)
        
        candidates.sort(key=lambda x: x.get('v', 0), reverse=True)
        top_picks = candidates[:100]
        logger.log(f"🔬 Filtered {len(top_picks)} movers for AI analysis.")
        
        # Set Streamer watchlist
        tickers_list = [t['T'] for t in top_picks]
        self.streamer.set_watchlist(tickers_list)
        logger.log(f"📡 Event Stream listening to {len(tickers_list)} tickers.")

        for item in top_picks:
            if self._stop_event.is_set():
                logger.log("🛑 Agent stop requested during scan, halting.")
                break

            ticker = item['T']
            logger.log(f"🔍 [Agent] Analyzing {ticker}...")

            try:
                output, reasoning, history_df = analyze_stock(ticker, is_auto=True)
            except Exception as e:
                logger.log(f"⚠️ [Agent] Error analyzing {ticker}: {e}", level="ERROR")

            # Rate limit buffer - 20 seconds is safer for free tier
            time.sleep(20)

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
            if data and data["Score"] >= 75:
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

# Start Monitor
monitor_thread = threading.Thread(target=run_position_monitor, daemon=True)
monitor_thread.start()

def update_agent_status():
    status = "Running" if agent.is_running else "Stopped"
    last = agent.last_run.strftime("%H:%M:%S") if agent.last_run else "N/A"
    nxt = agent.next_run.strftime("%H:%M:%S") if agent.next_run else "N/A"
    return f"### Status: {status}", last, nxt

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
    const savedTheme = localStorage.getItem('theme') || 'light';
    if (savedTheme === 'dark') {
        document.body.classList.add('dark');
    } else {
        document.body.classList.remove('dark');
        localStorage.setItem('theme', 'light');
    }
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

with gr.Blocks(theme=custom_theme, js=theme_manager_js) as demo:
    with gr.Row():
        with gr.Column(scale=1, min_width=100):
            gr.Image("nuroq_logo.png", show_label=False, container=False, width=100)
        with gr.Column(scale=4):
            gr.Markdown("# 🧠 NuroQ — Frontier Neural Quant\n`Neural · Ensemble · Sovereign Agent`")
        with gr.Column(scale=1, min_width=150):
            theme_toggle_btn = gr.Button("🌙 Switch to Dark Mode", size="sm")
    
    # ── GLOBAL QUICK TRADE BAR (Alpaca Paper) ──────────────────────────────
    with gr.Accordion("⚡ Quick Trade (Alpaca Paper)", open=False):
        with gr.Row():
            qt_ticker = gr.Textbox(label="Ticker", placeholder="AAPL", scale=2)
            qt_shares = gr.Number(label="Shares", value=1, scale=1)
            qt_type = gr.Dropdown(choices=["Market", "Limit", "Stop", "Stop Limit", "Trailing Stop"], value="Market", label="Order Type", scale=2)
            qt_tif = gr.Dropdown(choices=["Day", "GTC", "OPG", "IOC", "FOK"], value="GTC", label="Time in Force", scale=2)
        
        with gr.Row():
            qt_limit = gr.Number(label="Limit Price ($)", visible=False, scale=2)
            qt_stop = gr.Number(label="Stop Price / Trail ($)", visible=False, scale=2)
            qt_buy = gr.Button("🟢 BUY", variant="primary", scale=2)
            qt_sell = gr.Button("🔴 SELL", variant="stop", scale=2)
            
        qt_status = gr.Markdown("*Ready.*")
        
        # Dynamic visibility logic
        def update_price_inputs(order_type):
            show_limit = order_type in ["Limit", "Stop Limit"]
            show_stop = order_type in ["Stop", "Stop Limit", "Trailing Stop"]
            
            # Update button text to reflect order type
            buy_text = f"🟢 BUY {order_type.upper()}"
            sell_text = f"🔴 SELL {order_type.upper()}"
            
            return (
                gr.update(visible=show_limit), 
                gr.update(visible=show_stop),
                gr.update(value=buy_text),
                gr.update(value=sell_text)
            )

        qt_type.change(
            update_price_inputs, 
            inputs=[qt_type], 
            outputs=[qt_limit, qt_stop, qt_buy, qt_sell]
        )

    with gr.Tabs():
        with gr.TabItem("📊 Analyze"):
            # ── Compact input bar ───────────────────────────────────────────
            with gr.Row(equal_height=True):
                ticker_in = gr.Textbox(
                    label=None, placeholder="Enter ticker (e.g. NVDA, AAPL)",
                    scale=4, min_width=120, container=False
                )
                an_btn = gr.Button("🔍 RUN ANALYSIS", variant="primary", scale=1, min_width=140)

            # ── 2×2 grid ────────────────────────────────────────────────────
            with gr.Row(equal_height=True):
                with gr.Column(scale=1):
                    # BOX 1 — Chart
                    cht = gr.Plot(label="📊 Price Chart — SMA20 & Bollinger Bands")
                with gr.Column(scale=1):
                    # BOX 2 — Signal Card (metrics)
                    out_md = gr.Markdown("*Run analysis to see the signal card.*")
            with gr.Row(equal_height=True):
                with gr.Column(scale=1):
                    # BOX 3 — AI Chain-of-Thought
                    cot_md_out = gr.Markdown("*AI reasoning will appear here after analysis.*")
                with gr.Column(scale=1):
                    # BOX 4 — Trade Setup
                    setup_md_out = gr.Markdown("*Trade setup will appear here after analysis.*")

        with gr.TabItem("🔍 Market Scanner"):
            with gr.Tabs():
                with gr.TabItem("Top 20 Universe"):
                    sc_btn = gr.Button("SCAN WATCHLIST", variant="primary")
                    sc_sum = gr.Markdown("Click to scan.")
                    sc_tbl = gr.DataFrame(headers=["Ticker", "Price", "Change %", "Score", "Rating", "Analysis"], wrap=True)
                with gr.TabItem("Global Deep Scan"):
                    ds_btn = gr.Button("RUN GLOBAL DEEP SCAN", variant="primary")
                    ds_st = gr.Markdown("Ready.")
                    ds_tbl = gr.DataFrame(headers=["Ticker", "Price", "Change %", "Score", "Rating", "Analysis"], wrap=True)

        with gr.TabItem("💡 Signals"):
            gr.Markdown("### 📜 Universal Signal History")
            gr.Markdown("*(History of every equity analyzed by the system)*")
            refresh_sig_btn = gr.Button("🔄 Refresh Signal Log", variant="primary")
            signals_table = gr.DataFrame(
                headers=["Timestamp", "Ticker", "Name", "Industry", "Price", "Technicals", "Fundamentals", "Signal", "AI Score", "Quant Score"],
                wrap=True
            )

        with gr.TabItem("💼 Portfolio"):
            with gr.Row():
                refresh_p_btn = gr.Button("🔄 Refresh Portfolio & Prices", variant="primary")
                remove_p_btn = gr.Button("🗑️ Remove Selection (Input Ticker Below)", variant="secondary")
            ticker_remove = gr.Textbox(label="Ticker to Remove", placeholder="e.g. AAPL")
            p_tbl = gr.DataFrame(headers=portfolio_mgr.cols, interactive=False)

        with gr.TabItem("🤖 Agent"):
            with gr.Row():
                st_a_btn = gr.Button("▶️ START AGENT", variant="primary")
                sp_a_btn = gr.Button("⏹️ STOP AGENT", variant="stop")
            ag_st = gr.Markdown("### Status: Stopped")
            with gr.Row():
                ag_last = gr.Textbox(label="Last Run")
                ag_next = gr.Textbox(label="Next Run")
            ag_log = gr.DataFrame(headers=["Timestamp", "Ticker", "Action", "Price", "Shares", "Total"])

        with gr.TabItem("📜 Activity Log"):
            with gr.Row():
                ref_l_btn = gr.Button("🔄 Refresh")
                clr_l_btn = gr.Button("🗑️ Clear")
            act_disp = gr.Textbox(label="Logs", lines=25, autoscroll=True)

    # Event Bindings
    def handle_remove(t):
        portfolio_mgr.remove_position(t)
        return portfolio_mgr.get_portfolio()

    # Attach Quick Trade handlers
    qt_buy.click(
        handle_quick_trade, 
        inputs=[qt_ticker, qt_shares, gr.State("buy"), qt_type, qt_tif, qt_limit, qt_stop], 
        outputs=[qt_status]
    )
    qt_sell.click(
        handle_quick_trade, 
        inputs=[qt_ticker, qt_shares, gr.State("sell"), qt_type, qt_tif, qt_limit, qt_stop], 
        outputs=[qt_status]
    )

    an_btn.click(analyze_stock, [ticker_in], [out_md, cot_md_out, cht, setup_md_out])
    sc_btn.click(scan_market, None, [sc_tbl, sc_sum])
    ds_btn.click(deep_market_scan, None, [ds_tbl, ds_st])
    refresh_sig_btn.click(portfolio_mgr.get_buy_signals, None, signals_table)
    refresh_p_btn.click(portfolio_mgr.refresh_prices, None, p_tbl)
    remove_p_btn.click(handle_remove, ticker_remove, p_tbl)
    
    st_a_btn.click(agent.start).then(update_agent_status, outputs=[ag_st, ag_last, ag_next])
    sp_a_btn.click(agent.stop).then(update_agent_status, outputs=[ag_st, ag_last, ag_next])
    ref_l_btn.click(refresh_activity_log, outputs=act_disp)
    clr_l_btn.click(clear_activity_log, outputs=act_disp)
    theme_toggle_btn.click(None, None, theme_toggle_btn, js=toggle_theme_js)
    
    gr.Markdown("---")
    gr.Markdown("*Disclaimer: This is an AI tool for educational purposes and pattern recognition, not financial advice.*")

if __name__ == "__main__":
    kill_port(7860)
    # launch the app
    demo.launch(inbrowser=False, server_port=7860, server_name="0.0.0.0", share=True)


