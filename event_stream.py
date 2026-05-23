import time
import threading
import os
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from collections import deque
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

load_dotenv()
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

_log = logging.getLogger("MarketStreamer")


class MarketStreamer:
    """
    Monitors a watchlist of tickers via Alpaca Real-Time WebSockets.

    Bar handling is non-blocking: the WebSocket reader stays in the async loop,
    and any trigger fans out to a small thread pool so a long LLM analysis call
    can't stall incoming bars. Each ticker has a per-symbol debounce so a single
    sustained breakout doesn't spam the callback every minute.
    """

    def __init__(self, trigger_callback, debounce_seconds: int = 300,
                 max_callback_workers: int = 2):
        self.trigger_callback = trigger_callback
        self.debounce_seconds = debounce_seconds
        self.watchlist = []
        self.is_running = False
        self.stream = None
        self.last_prices = {}
        self.price_history = {}  # ticker -> deque of prices for SMA/breakout
        self.loop = None
        self._last_trigger_at = {}        # ticker -> unix ts of last fire
        self._trigger_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_callback_workers,
            thread_name_prefix="streamer-cb",
        )

    def set_watchlist(self, tickers):
        """Updates the list of tickers to monitor."""
        old_watchlist = set(self.watchlist)
        new_watchlist = set([t.upper() for t in tickers])
        
        self.watchlist = list(new_watchlist)
        
        for t in self.watchlist:
            if t not in self.price_history:
                self.price_history[t] = deque(maxlen=20) # Buffer for SMA/Volatility
                
        # If already running, we need to subscribe/unsubscribe.
        # alpaca-py's subscribe_bars / unsubscribe_bars are SYNCHRONOUS (they return None
        # and just queue a subscription message internally), so we use call_soon_threadsafe
        # to schedule them onto the WebSocket's event loop from this (agent) thread.
        # Passing them to run_coroutine_threadsafe raised "A coroutine object is required".
        if self.is_running and self.stream and self.loop:
            to_subscribe = new_watchlist - old_watchlist
            to_unsubscribe = old_watchlist - new_watchlist

            if to_subscribe:
                self.loop.call_soon_threadsafe(
                    self.stream.subscribe_bars, self._handle_bar, *to_subscribe
                )
            if to_unsubscribe:
                self.loop.call_soon_threadsafe(
                    self.stream.unsubscribe_bars, *to_unsubscribe
                )

    def start(self):
        """Starts the background websocket thread."""
        if self.is_running: return
        self.is_running = True
        
        # Run the async stream in a dedicated thread
        self.thread = threading.Thread(target=self._run_stream, daemon=True)
        self.thread.start()

    def _run_stream(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.stream = StockDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        
        async def subscribe():
            if self.watchlist:
                await self.stream.subscribe_bars(self._handle_bar, *self.watchlist)
            await self.stream._run_forever()

        self.loop.run_until_complete(subscribe())

    def _maybe_fire(self, ticker: str, reason: str) -> None:
        """
        Dispatches trigger_callback to the thread pool if the per-ticker debounce
        has elapsed. Returns immediately so the WebSocket loop is never blocked.
        """
        now = time.time()
        with self._trigger_lock:
            last = self._last_trigger_at.get(ticker, 0)
            if now - last < self.debounce_seconds:
                return  # still cooling down
            self._last_trigger_at[ticker] = now

        def _run():
            try:
                self.trigger_callback(ticker)
            except Exception as e:
                _log.warning("trigger_callback for %s (%s) raised: %s", ticker, reason, e)

        self._executor.submit(_run)

    async def _handle_bar(self, bar):
        """Processes incoming 1-minute bar data. Must not block the asyncio loop."""
        ticker = bar.symbol
        price = bar.close

        prev_price = self.last_prices.get(ticker)
        self.last_prices[ticker] = price

        # Volatility trigger: ≥2% move in one minute
        if prev_price and abs((price - prev_price) / prev_price) >= 0.02:
            self._maybe_fire(ticker, "volatility>=2%")

        # Rolling history trigger: ≥3% above 5-min average (breakout)
        if ticker in self.price_history:
            self.price_history[ticker].append(price)
            if len(self.price_history[ticker]) >= 5:
                avg_price = sum(self.price_history[ticker]) / len(self.price_history[ticker])
                if price > avg_price * 1.03:
                    self._maybe_fire(ticker, "breakout>3%_5min_avg")

    def stop(self):
        """Stops the websocket stream and shuts down the dispatch pool."""
        if self.stream:
            asyncio.run_coroutine_threadsafe(self.stream.stop(), self.loop)
        self.is_running = False
        self._executor.shutdown(wait=False, cancel_futures=True)
