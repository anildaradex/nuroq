import os
import uuid
import logging
from typing import Optional
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
        """(Re)attempts to connect. Returns True if the account is now ACTIVE.

        Safety belt: hard-fail if someone tries to set NUROQ_LIVE_TRADING=1
        without acknowledging the wash-sale exposure. The wash-sale check
        protects against accidental tax liability from algorithmic re-entries
        within the IRS 61-day window — it MUST be reviewed and acknowledged
        before any real money trades. The acknowledgment is satisfied by EITHER
        NUROQ_WASH_SALE_AWARE=1 (you've reviewed the guard) OR NUROQ_SECTION_475=1
        (you've asserted a valid §475(f) mark-to-market election, under which the
        wash-sale rule does not apply at all — so there is nothing to guard).
        """
        if not (self.api_key and self.api_secret):
            logger.warning("⚠️ Alpaca keys not found in .env. Execution will be simulated.")
            self.is_connected = False
            return False
        live = os.getenv("NUROQ_LIVE_TRADING", "0") == "1"
        section_475 = os.getenv("NUROQ_SECTION_475", "0") == "1"
        wash_sale_aware = os.getenv("NUROQ_WASH_SALE_AWARE", "0") == "1"
        if live and not (wash_sale_aware or section_475):
            raise RuntimeError(
                "REFUSING to enable live trading: NUROQ_LIVE_TRADING=1 was set "
                "but neither NUROQ_WASH_SALE_AWARE=1 nor NUROQ_SECTION_475=1 was. "
                "The agent's 30-min per-ticker cooldown is far shorter than the "
                "IRS wash-sale window (30 days), which can disallow real losses "
                "for tax purposes. Acknowledge by setting NUROQ_WASH_SALE_AWARE=1 "
                "in .env (you've reviewed the guard), or NUROQ_SECTION_475=1 if a "
                "valid §475(f) mark-to-market election is in effect (wash-sale "
                "rule does not apply)."
            )
        try:
            self.client = TradingClient(self.api_key, self.api_secret, paper=not live)
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
                             sl: float, tp: float, tif: str = "GTC",
                             limit_price: float = None):
        """
        Submits an entry + stop-loss + take-profit as a single atomic bracket.

        Entry type depends on `limit_price`:
          - None / 0  → MARKET entry (fills immediately at current price)
          - > 0       → LIMIT entry at the specified price (better price control,
                        but may not fill if the market runs away)

        For a long entry (action='buy'): require sl < entry < tp.
        For a short entry (action='sell'): require sl > entry > tp.
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

        # Limit-bracket needs the limit_price to be sandwiched between SL and TP,
        # otherwise the order is contradictory (e.g. limit BUY above TP would
        # auto-trigger take-profit immediately on fill).
        use_limit = limit_price is not None and limit_price > 0
        if use_limit:
            if action_l == 'buy' and not (sl < limit_price < tp):
                return (f"❌ Bracket LIMIT BUY rejected: limit ${limit_price} must be "
                        f"between SL (${sl}) and TP (${tp})")
            if action_l == 'sell' and not (sl > limit_price > tp):
                return (f"❌ Bracket LIMIT SELL rejected: limit ${limit_price} must be "
                        f"between TP (${tp}) and SL (${sl})")

        if not self._ensure_connection():
            entry_label = f"Limit @ ${limit_price}" if use_limit else "Market"
            return (f"⚠️ Simulated BRACKET {action.upper()} {shares} {ticker} "
                    f"({entry_label}, SL=${sl}, TP=${tp}) — Alpaca not connected")

        try:
            side = OrderSide.BUY if action_l == 'buy' else OrderSide.SELL
            tif_map = {
                "Day": TimeInForce.DAY, "GTC": TimeInForce.GTC,
                "OPG": TimeInForce.OPG, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK,
            }
            time_in_force = tif_map.get(tif, TimeInForce.GTC)
            client_order_id = f"nuroq-br-{uuid.uuid4().hex[:21]}"

            common = dict(
                symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp),
                stop_loss=StopLossRequest(stop_price=sl),
                client_order_id=client_order_id,
            )
            if use_limit:
                order_data = LimitOrderRequest(limit_price=limit_price, **common)
                entry_desc = f"Limit @ ${limit_price}"
            else:
                order_data = MarketOrderRequest(**common)
                entry_desc = "Market"

            self.client.submit_order(order_data=order_data)
            msg = (f"✅ Alpaca BRACKET {action.upper()} {shares} {ticker} "
                   f"— Entry: {entry_desc} | SL: ${sl} | TP: ${tp}")
            logger.info(msg)
            return msg
        except Exception as e:
            err = f"❌ Alpaca Bracket Order Failed for {ticker}: {e}"
            logger.error(err)
            return err

    def list_position_symbols(self) -> Optional[set]:
        """
        Returns the set of ticker symbols the Alpaca account actually holds.
        Used to reconcile the local portfolio tracker against the broker so
        phantom positions (already closed at Alpaca) stop generating alerts.

        Returns None on connection/API failure so callers can distinguish
        "no positions" (empty set) from "couldn't check" (None) and avoid
        wiping the local tracker on a transient error.
        """
        if not self._ensure_connection():
            return None
        try:
            positions = self.client.get_all_positions()
            return {p.symbol.upper() for p in positions}
        except Exception as e:
            logger.warning(f"⚠️ list_position_symbols failed: {e}")
            return None

    def list_positions(self) -> Optional[list]:
        """
        Full detail on every Alpaca position. Each item:
          {symbol, qty, avg_entry_price, current_price, market_value,
           cost_basis, unrealized_pl, unrealized_plpc,
           unrealized_intraday_pl, unrealized_intraday_plpc, change_today}
        Returns None on failure (so callers don't mistake an error for "flat").
        Used by the two-way portfolio reconcile to IMPORT positions that exist
        at Alpaca but aren't in the local tracker (e.g. opened outside NuroQ).
        Intraday fields drive the Today insight panel ("why is the account up
        or down TODAY?") — `unrealized_pl` is since-entry and would attribute
        a big move from weeks ago to today, which is wrong for that question.
        """
        if not self._ensure_connection():
            return None

        def _f(x, default=0.0):
            # Alpaca occasionally returns None / "" / "nan"; coerce defensively.
            try:
                v = float(x) if x not in (None, "") else default
                return v if v == v else default   # NaN guard
            except (TypeError, ValueError):
                return default

        try:
            out = []
            for p in self.client.get_all_positions():
                out.append({
                    "symbol":          p.symbol.upper(),
                    "qty":             _f(p.qty),
                    "avg_entry_price": _f(p.avg_entry_price),
                    "current_price":   _f(p.current_price, _f(p.avg_entry_price)),
                    "market_value":    _f(p.market_value),
                    "cost_basis":      _f(p.cost_basis),
                    "unrealized_pl":   _f(p.unrealized_pl),
                    "unrealized_plpc": _f(p.unrealized_plpc),
                    # Today-only attribution — Alpaca computes from prev_close.
                    "unrealized_intraday_pl":   _f(getattr(p, "unrealized_intraday_pl", None)),
                    "unrealized_intraday_plpc": _f(getattr(p, "unrealized_intraday_plpc", None)),
                    "change_today":             _f(getattr(p, "change_today", None)),
                })
            return out
        except Exception as e:
            logger.warning(f"⚠️ list_positions failed: {e}")
            return None

    def get_bracket_levels(self) -> dict:
        """
        Maps each held symbol → its open SL/TP levels parsed from open SELL
        bracket orders. Used by the reconcile to populate SL/TP on imported
        positions. Returns {symbol: {"sl": float|None, "tp": float|None}}.

        A bracket take-profit is a SELL LIMIT (limit_price = TP); a bracket
        stop-loss is a SELL STOP (stop_price = SL). We collect whichever legs
        are still open per symbol.
        """
        levels: dict = {}
        if not self._ensure_connection():
            return levels
        try:
            # Query with nested=True so OCO/bracket child legs come back attached
            # to their parent. An OCO sell is a parent LIMIT (TP) carrying a
            # nested STOP leg (SL) — the SL is NOT a top-level order, so a flat
            # query would miss it.
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200, nested=True)
            orders = self.client.get_orders(filter=req)

            def _absorb(o):
                """Pull TP (limit) / SL (stop) from one order object into levels."""
                try:
                    if o.side.value.upper() != "SELL":
                        return
                    sym = o.symbol.upper()
                    slot = levels.setdefault(sym, {"sl": None, "tp": None})
                    otype = o.order_type.value if o.order_type else ""
                    if o.limit_price and otype in ("limit", "stop_limit"):
                        slot["tp"] = float(o.limit_price)
                    if o.stop_price and otype in ("stop", "stop_limit"):
                        slot["sl"] = float(o.stop_price)
                except Exception:
                    pass

            for o in orders:
                _absorb(o)
                for leg in (getattr(o, "legs", None) or []):
                    _absorb(leg)
        except Exception as e:
            logger.warning(f"⚠️ get_bracket_levels failed: {e}")
        return levels

    def submit_protective_oco(self, ticker: str, shares: int,
                              sl: float, tp: float, tif: str = "GTC") -> str:
        """
        Places a protective OCO (One-Cancels-Other) SELL on an EXISTING long
        position: a take-profit limit (sell at tp) + a stop-loss (sell at sl).
        Whichever fills first auto-cancels the other. This makes a bare
        position auto-protected even if NuroQ is offline.

        Requires sl < current/entry < tp (a long exit). Alpaca rejects if the
        levels are crossed. Idempotent-ish: if an OCO/bracket SELL already
        covers this symbol, call cancel first or it may reject on qty.
        """
        ticker = ticker.upper()
        shares = int(shares)
        if shares < 1:
            return f"❌ Protective OCO rejected: shares={shares} (must be >= 1)"
        if sl <= 0 or tp <= 0:
            return f"❌ Protective OCO rejected: SL=${sl}, TP=${tp} (both must be > 0)"
        if not (sl < tp):
            return f"❌ Protective OCO rejected: SL (${sl}) must be below TP (${tp})"

        if not self._ensure_connection():
            return (f"⚠️ Simulated PROTECTIVE OCO SELL {shares} {ticker} "
                    f"(SL=${sl}, TP=${tp}) — Alpaca not connected")

        try:
            tif_map = {
                "Day": TimeInForce.DAY, "GTC": TimeInForce.GTC,
                "OPG": TimeInForce.OPG, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK,
            }
            time_in_force = tif_map.get(tif, TimeInForce.GTC)
            client_order_id = f"nuroq-oco-{uuid.uuid4().hex[:20]}"

            # OCO sell on a long: Alpaca wants BOTH exits expressed as child
            # legs — take_profit.limit_price (TP) and stop_loss.stop_price (SL).
            # The parent LimitOrderRequest carries no top-level limit_price for
            # OCO; the levels live entirely in the legs.
            order_data = LimitOrderRequest(
                symbol=ticker, qty=shares, side=OrderSide.SELL,
                time_in_force=time_in_force,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=tp),
                stop_loss=StopLossRequest(stop_price=sl),
                client_order_id=client_order_id,
            )
            self.client.submit_order(order_data=order_data)
            msg = (f"✅ Alpaca PROTECTIVE OCO on {shares} {ticker} "
                   f"— TP limit ${tp} / SL stop ${sl}")
            logger.info(msg)
            return msg
        except Exception as e:
            err = f"❌ Alpaca protective OCO failed for {ticker}: {e}"
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

    def get_recent_fills(self, ticker: str = None, days: int = 30) -> list:
        """
        Returns filled orders in the last N days. Each item is a dict with
        ticker, side, qty, fill_price, filled_at_ts. Used by the wash-sale
        check to find recent SELLs of a ticker before re-entering.

        If `ticker` is None, returns ALL fills (used for the dashboard's
        unfiltered trade history).
        """
        if not self._ensure_connection():
            return []
        try:
            from datetime import datetime as _dt, timezone, timedelta
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=_dt.now(timezone.utc) - timedelta(days=days),
                limit=500,
                nested=False,
            )
            orders = self.client.get_orders(filter=req)
            out = []
            for o in orders:
                if o.status.value not in ("filled", "partially_filled"):
                    continue
                if ticker and o.symbol.upper() != ticker.upper():
                    continue
                px = o.filled_avg_price
                if px is None:
                    continue
                out.append({
                    "ticker":        o.symbol.upper(),
                    "side":          o.side.value.upper(),       # "BUY" | "SELL"
                    "qty":           float(o.filled_qty or o.qty or 0),
                    "fill_price":    float(px),
                    "filled_at_ts":  o.filled_at.timestamp() if o.filled_at else (o.submitted_at.timestamp() if o.submitted_at else 0),
                    "order_type":    o.order_type.value if o.order_type else "market",
                    "order_class":   (o.order_class.value if o.order_class else "simple"),
                    "id":            str(o.id),
                })
            # Newest first
            out.sort(key=lambda x: x["filled_at_ts"], reverse=True)
            return out
        except Exception as e:
            logger.warning(f"⚠️ get_recent_fills({ticker}, {days}d) failed: {e}")
            return []
