import time
import threading
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from collections import deque
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar

load_dotenv()
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

class MarketStreamer:
    def __init__(self, trigger_callback):
        """
        Monitors a watchlist of tickers using Alpaca Real-Time WebSockets (IEX).
        Calculates volatility and potentially technical indicators on the fly.
        """
        self.trigger_callback = trigger_callback
        self.watchlist = []
        self.is_running = False
        self.stream = None
        self.last_prices = {}
        self.price_history = {} # ticker -> deque of prices for RSI/ATR
        self.loop = None

    def set_watchlist(self, tickers):
        """Updates the list of tickers to monitor."""
        old_watchlist = set(self.watchlist)
        new_watchlist = set([t.upper() for t in tickers])
        
        self.watchlist = list(new_watchlist)
        
        for t in self.watchlist:
            if t not in self.price_history:
                self.price_history[t] = deque(maxlen=20) # Buffer for SMA/Volatility
                
        # If already running, we need to subscribe/unsubscribe
        if self.is_running and self.stream:
            to_subscribe = new_watchlist - old_watchlist
            to_unsubscribe = old_watchlist - new_watchlist
            
            if to_subscribe:
                asyncio.run_coroutine_threadsafe(self.stream.subscribe_bars(self._handle_bar, *to_subscribe), self.loop)
            if to_unsubscribe:
                asyncio.run_coroutine_threadsafe(self.stream.unsubscribe_bars(*to_unsubscribe), self.loop)

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

    async def _handle_bar(self, bar):
        """Processes incoming 1-minute bar data."""
        ticker = bar.symbol
        price = bar.close
        
        prev_price = self.last_prices.get(ticker)
        self.last_prices[ticker] = price
        
        # Simple volatility trigger: > 2% move in one minute (rare but important)
        if prev_price and abs((price - prev_price) / prev_price) >= 0.02:
            self.trigger_callback(ticker)
            
        # Update rolling history for more advanced triggers
        if ticker in self.price_history:
            self.price_history[ticker].append(price)
            
            # If we have enough data, check for SMA crossovers or RSI alerts
            # For now, we'll keep it simple: trigger on high volume + price move
            # (Wait, bars also have volume)
            if len(self.price_history[ticker]) >= 5:
                avg_price = sum(self.price_history[ticker]) / len(self.price_history[ticker])
                # If price is 3% above 5-min average, might be a breakout
                if price > avg_price * 1.03:
                    self.trigger_callback(ticker)

    def stop(self):
        """Stops the websocket stream."""
        if self.stream:
            asyncio.run_coroutine_threadsafe(self.stream.stop(), self.loop)
        self.is_running = False
