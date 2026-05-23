import os
import logging
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    StopLimitOrderRequest,
    TrailingStopOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce

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
        
        if self.api_key and self.api_secret:
            try:
                self.client = TradingClient(self.api_key, self.api_secret, paper=True)
                acct = self.client.get_account()
                self.is_connected = acct.status.value == 'ACTIVE' if hasattr(acct.status, 'value') else acct.status == 'ACTIVE'
                logger.info(f"🔌 Connected to Alpaca Paper Trading. Status: ACTIVE")
            except Exception as e:
                logger.warning(f"⚠️ Failed to connect to Alpaca: {e}")
        else:
            logger.warning("⚠️ Alpaca keys not found in .env. Execution will be simulated.")

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
        if not self.is_connected:
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

            # Build the specific Request Model based on order_type
            if order_type == "Market":
                order_data = MarketOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force
                )
            elif order_type == "Limit":
                order_data = LimitOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    limit_price=limit_price
                )
            elif order_type == "Stop":
                order_data = StopOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    stop_price=stop_price
                )
            elif order_type == "Stop Limit":
                order_data = StopLimitOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    stop_price=stop_price, limit_price=limit_price
                )
            elif order_type == "Trailing Stop":
                # Trailing stop uses trail_price (dollar amount) or trail_percent. 
                # For simplicity, we use stop_price as the dollar trail_amount.
                order_data = TrailingStopOrderRequest(
                    symbol=ticker, qty=shares, side=side, time_in_force=time_in_force,
                    trail_price=stop_price
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
