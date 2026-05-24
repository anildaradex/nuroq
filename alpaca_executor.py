import os
import uuid
import logging
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    TrailingStopOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    GetPortfolioHistoryRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

# Setup basic logger for execution module
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger("AlpacaExecution")

class LiveAlpacaExecutor:
    """
    Handles live/paper execution via Alpaca Trade API (alpaca-py).
    """
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("ALPACA_API_KEY", "")
        self.api_secret = os.getenv("ALPACA_SECRET_KEY", "")
        
        self.is_connected = False
        self.client = None
        
        self._connect()

    def _connect(self) -> bool:
        """(Re)attempts to connect. Returns True if the account is now ACTIVE."""
        if not (self.api_key and self.api_secret):
            logger.warning("⚠️ Alpaca keys not found in .env. Execution will be simulated.")
            self.is_connected = False
            return False
        try:
            self.client = TradingClient(self.api_key, self.api_secret, paper=True)
            acct = self.client.get_account()
            status = acct.status.value if hasattr(acct.status, 'value') else acct.status
            self.is_connected = status == 'ACTIVE'
            if self.is_connected:
                logger.info("🔌 Connected to Alpaca Paper Trading. Status: ACTIVE")
            else:
                logger.warning(f"⚠️ Alpaca account status: {status} (not ACTIVE)")
            return self.is_connected
        except Exception as e:
            logger.warning(f"⚠️ Failed to connect to Alpaca: {e}")
            self.is_connected = False
            return False

    def _ensure_connection(self) -> bool:
        """Lazy reconnect — call before any order or account read."""
        if self.is_connected:
            return True
        return self._connect()

    def submit_advanced_order(self, ticker: str, action: str, shares: int,
                              order_type: str = "Market", tif: str = "GTC",
                              limit_price: float = None, stop_price: float = None):
        """
        Executes advanced brokerage orders on Alpaca.
        action: 'buy' or 'sell'
        order_type: 'Market', 'Limit', 'Stop', 'Stop Limit', 'Trailing Stop'
        tif: 'Day', 'GTC', 'OPG', 'IOC', 'FOK'
        """
        ticker = ticker.upper()
        if not self._ensure_connection():
            return f"⚠️ Simulated {action.upper()} of {shares} {ticker} ({order_type} Order) — Alpaca not connected"

        try:
            side = OrderSide.BUY if action.lower() == 'buy' else OrderSide.SELL
            
            # Map TIF string to Enum
            tif_map = {
                "Day": TimeInForce.DAY,
                "GTC": TimeInForce.GTC,
                "OPG": TimeInForce.OPG,
                "IOC": TimeInForce.IOC,
                "FOK": TimeInForce.FOK
            }
            time_in_force = tif_map.get(tif, TimeInForce.GTC)

            client_order_id = f"nuroq-{uuid.uuid4().hex[:24]}"

            # Build the specific Request Model based on order_type
            if order_type == "Market":
                order_data = MarketOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    client_order_id=client_order_id,
                )
            elif order_type == "Limit":
                order_data = LimitOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    limit_price=limit_price, client_order_id=client_order_id,
                )
            elif order_type == "Stop":
                order_data = StopOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    stop_price=stop_price, client_order_id=client_order_id,
                )
            elif order_type == "Stop Limit":
                order_data = StopLimitOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    stop_price=stop_price, limit_price=limit_price,
                    client_order_id=client_order_id,
                )
            elif order_type == "Trailing Stop":
                order_data = TrailingStopOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    trail_price=stop_price, client_order_id=client_order_id,
                )
            else:
                return f"❌ Invalid Order Type: {order_type}"

            order = self.client.submit_order(order_data=order_data)
            
            # Formatting the success message based on type
            px_info = ""
            if limit_price: px_info += f" Limit: ${limit_price}"
            if stop_price: px_info += f" Stop: ${stop_price}"
            
            msg = f"✅ Alpaca Order Submitted: {action.upper()} {shares} {ticker} [{order_type}]{px_info}"
            logger.info(msg)
            return msg
        except Exception as e:
            err = f"❌ Alpaca Order Failed for {ticker}: {e}"
            logger.error(err)
            return err

    def submit_bracket_order(self, ticker: str, action: str, shares: int,
                             sl: float, tp: float, tif: str = "GTC"):
        """
        Submits an entry + stop-loss + take-profit as a single atomic bracket.
        For a long entry (action='buy'): require sl < entry < tp.
        Alpaca brackets require integer share quantity.
        """
        ticker = ticker.upper()
        action_l = action.lower()
        if shares < 1:
            return f"❌ Bracket order rejected: shares={shares} (must be >= 1)"
        if sl <= 0 or tp <= 0:
            return f"❌ Bracket order rejected: SL=${sl}, TP=${tp} (both must be > 0)"
        if action_l == 'buy' and not (sl < tp):
            return f"❌ Bracket BUY rejected: SL (${sl}) must be below TP (${tp})"
        if action_l == 'sell' and not (sl > tp):
            return f"❌ Bracket SELL rejected: SL (${sl}) must be above TP (${tp})"

        if not self._ensure_connection():
            return (f"⚠️ Simulated BRACKET {action.upper()} {shares} {ticker} "
                    f"(SL=${sl}, TP=${tp}) — Alpaca not connected")

        try:
            side = OrderSide.BUY if action_l == 'buy' else OrderSide.SELL
            tif_map = {
                "Day": TimeInForce.DAY, "GTC": TimeInForce.GTC,
                "OPG": TimeInForce.OPG, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK,
            }
            time_in_force = tif_map.get(tif, TimeInForce.GTC)
            client_order_id = f"nuroq-br-{uuid.uuid4().hex[:21]}"

            order_data = MarketOrderRequest(
                symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp),
                stop_loss=StopLossRequest(stop_price=sl),
                client_order_id=client_order_id,
            )
            self.client.submit_order(order_data=order_data)
            msg = (f"✅ Alpaca BRACKET {action.upper()} {shares} {ticker} "
                   f"— Entry: Market | SL: ${sl} | TP: ${tp}")
            logger.info(msg)
            return msg
        except Exception as e:
            err = f"❌ Alpaca Bracket Order Failed for {ticker}: {e}"
            logger.error(err)
            return err

    def close_position(self, ticker: str) -> str:
        """
        Closes the entire Alpaca position for `ticker` and cancels any open
        SL/TP bracket legs. Returns a status string.
        """
        ticker = ticker.upper()
        if not self._ensure_connection():
            return f"⚠️ Simulated CLOSE of {ticker} — Alpaca not connected"
        try:
            self.client.close_position(ticker)
            msg = f"✅ Alpaca position closed for {ticker} (open SL/TP brackets cancelled)."
            logger.info(msg)
            return msg
        except Exception as e:
            err = f"❌ Alpaca close_position failed for {ticker}: {e}"
            logger.error(err)
            return err

    def get_open_orders(self, limit: int = 50) -> list:
        """
        Returns pending/open orders at Alpaca (anything not yet filled/cancelled).
        Each item is a dict with: id, symbol, side, qty, order_type, order_class,
        limit_price, stop_price, status, submitted_at, is_bracket.
        Returns [] if disconnected or on error.
        """
        if not self._ensure_connection():
            return []
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=limit, nested=True)
            orders = self.client.get_orders(filter=req)
        except Exception as e:
            logger.warning(f"⚠️ get_open_orders failed: {e}")
            return []

        result = []
        for o in orders:
            side = o.side.value if hasattr(o.side, 'value') else str(o.side)
            otype = o.order_type.value if hasattr(o.order_type, 'value') else str(o.order_type)
            oclass = (o.order_class.value if hasattr(o.order_class, 'value') else str(o.order_class)) if o.order_class else "simple"
            status = o.status.value if hasattr(o.status, 'value') else str(o.status)
            result.append({
                "id":             str(o.id),
                "symbol":         o.symbol,
                "side":           side.upper(),
                "qty":            float(o.qty) if o.qty else 0.0,
                "order_type":     otype,
                "order_class":    oclass,
                "limit_price":    float(o.limit_price) if o.limit_price else None,
                "stop_price":     float(o.stop_price) if o.stop_price else None,
                "status":         status,
                "submitted_at":   o.submitted_at.isoformat() if o.submitted_at else None,
                "is_bracket":     oclass == "bracket",
            })
        return result

    def get_account_summary(self) -> dict:
        """
        Returns current account snapshot. Keys: equity, cash, buying_power,
        last_equity, positions_value, todays_pl, todays_pl_pct, status, connected.
        Returns {connected: False, ...} with zeros when Alpaca isn't reachable.
        """
        empty = {
            "connected": False, "status": "DISCONNECTED",
            "equity": 0.0, "cash": 0.0, "buying_power": 0.0,
            "last_equity": 0.0, "positions_value": 0.0,
            "todays_pl": 0.0, "todays_pl_pct": 0.0,
        }
        if not self._ensure_connection():
            return empty
        try:
            a = self.client.get_account()
            equity = float(a.equity)
            cash = float(a.cash)
            last_equity = float(a.last_equity) if a.last_equity else equity
            todays_pl = equity - last_equity
            todays_pl_pct = (todays_pl / last_equity * 100) if last_equity else 0.0
            status = a.status.value if hasattr(a.status, 'value') else str(a.status)
            return {
                "connected": True, "status": status,
                "equity": round(equity, 2),
                "cash": round(cash, 2),
                "buying_power": round(float(a.buying_power), 2),
                "last_equity": round(last_equity, 2),
                "positions_value": round(equity - cash, 2),
                "todays_pl": round(todays_pl, 2),
                "todays_pl_pct": round(todays_pl_pct, 2),
            }
        except Exception as e:
            logger.warning(f"⚠️ get_account_summary failed: {e}")
            return empty

    def get_portfolio_history(self, period_days: int = 30) -> dict:
        """
        Returns equity history. Keys: connected, return_pct, equity_series (list),
        timestamps (list of unix seconds), period.
        """
        empty = {"connected": False, "return_pct": 0.0, "equity_series": [],
                 "timestamps": [], "period": f"{period_days}D"}
        if not self._ensure_connection():
            return empty
        try:
            req = GetPortfolioHistoryRequest(period=f"{period_days}D", timeframe="1D")
            h = self.client.get_portfolio_history(history_filter=req)
            equity_series = [float(e) for e in (h.equity or []) if e is not None]
            timestamps = list(h.timestamp or [])
            if len(equity_series) >= 2 and equity_series[0]:
                ret_pct = (equity_series[-1] - equity_series[0]) / equity_series[0] * 100
            else:
                ret_pct = 0.0
            return {
                "connected": True,
                "return_pct": round(ret_pct, 2),
                "equity_series": equity_series,
                "timestamps": timestamps,
                "period": f"{period_days}D",
            }
        except Exception as e:
            logger.warning(f"⚠️ get_portfolio_history failed: {e}")
            return empty
