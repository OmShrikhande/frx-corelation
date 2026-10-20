import os
import time
import subprocess
import logging
from typing import Optional, List, Dict, Any
import pandas as pd
import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

class MT5Connector:
    """
    Handles MetaTrader 5 terminal process management, initialization,
    live data retrieval, and order execution.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        login: Optional[int] = None,
        password: str = "",
        server: str = "",
        magic_number: int = 778899,
        deviation: int = 10
    ):
        self.path = path
        self.login = login
        self.password = password
        self.server = server
        self.magic_number = magic_number
        self.deviation = deviation
        self.connected = False

    def is_terminal_process_running(self) -> bool:
        """Checks if terminal64.exe or terminal.exe is running on Windows."""
        try:
            output = subprocess.check_output(
                'tasklist /FI "IMAGENAME eq terminal64.exe" /NH',
                shell=True,
                text=True,
                stderr=subprocess.DEVNULL
            )
            return "terminal64.exe" in output.lower()
        except Exception:
            return False

    def launch_terminal_if_needed(self):
        """Spawns the MT5 terminal process if it is not currently open."""
        if not self.is_terminal_process_running():
            logger.info("MetaTrader 5 terminal is not running. Launching terminal...")
            if self.path and os.path.exists(self.path):
                try:
                    subprocess.Popen([self.path], shell=False)
                    logger.info(f"Launched MT5 process: {self.path}")
                    # Allow terminal a few seconds to start up
                    time.sleep(5)
                except Exception as e:
                    logger.error(f"Failed to launch MT5 process: {e}")
            else:
                logger.warning("No explicit MT5 executable path found. Relying on mt5.initialize() auto-discovery.")

    def initialize(self) -> bool:
        """
        Initializes connection to MT5 terminal. Launches terminal if closed.
        """
        # Ensure terminal is running
        self.launch_terminal_if_needed()

        init_args = {}
        if self.path and os.path.exists(self.path):
            init_args["path"] = self.path
        if self.login and self.password and self.server:
            init_args["login"] = self.login
            init_args["password"] = self.password
            init_args["server"] = self.server

        logger.info(f"Connecting to MT5 (path={self.path or 'default'})...")
        initialized = mt5.initialize(**init_args)

        if not initialized:
            error_code, error_msg = mt5.last_error()
            logger.error(f"mt5.initialize() failed: ({error_code}) {error_msg}")
            self.connected = False
            return False

        # If credentials provided separately, login
        if self.login and self.password and self.server:
            logged_in = mt5.login(login=self.login, password=self.password, server=self.server)
            if not logged_in:
                error_code, error_msg = mt5.last_error()
                logger.error(f"mt5.login() failed: ({error_code}) {error_msg}")
                self.connected = False
                return False

        account = mt5.account_info()
        terminal = mt5.terminal_info()

        if not account or not terminal:
            logger.error("Could not obtain account or terminal info.")
            self.connected = False
            return False

        logger.info(
            f"MT5 Connected successfully! Server: '{account.server}', "
            f"Account #{account.login} ({account.company}), Balance: ${account.balance:.2f}, Leverage: 1:{account.leverage}"
        )
        self.connected = True
        return True

    def ensure_symbol_active(self, symbol: str) -> bool:
        """Ensures a trading pair is selected and visible in MarketWatch."""
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"Symbol '{symbol}' not found on broker server.")
            return False
        
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.error(f"Failed to select symbol '{symbol}' into Market Watch.")
                return False
        return True

    def get_symbol_info(self, symbol: str):
        """Returns symbol specifications (points, digits, spread, etc.)."""
        return mt5.symbol_info(symbol)

    def get_broker_utc_offset_hours(self) -> int:
        """Dynamically computes the integer hour offset between broker server time and UTC."""
        try:
            tick = mt5.symbol_info_tick("XAUUSD") or mt5.symbol_info_tick("EURUSD")
            if tick and tick.time:
                from datetime import datetime, timezone
                broker_dt = datetime.fromtimestamp(tick.time, tz=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                return int(round((broker_dt - now_utc).total_seconds() / 3600.0))
        except Exception as e:
            logger.warning(f"Could not compute broker UTC offset dynamically: {e}")
        return 3

    def get_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetches the latest tick price for a symbol."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "symbol": symbol,
            "ask": tick.ask,
            "bid": tick.bid,
            "spread": round((tick.ask - tick.bid) / (mt5.symbol_info(symbol).point or 0.00001), 1),
            "time": tick.time
        }

    def get_rates(self, symbol: str, timeframe: int, count: int = 100) -> Optional[pd.DataFrame]:
        """
        Fetches historical OHLCV data as a pandas DataFrame.
        """
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"Could not retrieve rates for {symbol}.")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Returns current account metrics."""
        acc = mt5.account_info()
        if not acc:
            return None
        return acc._asdict()

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns currently active open positions."""
        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()

        if positions is None:
            return []

        return [p._asdict() for p in positions]

    def get_supported_filling_mode(self, symbol: str) -> int:
        """
        Determines the supported filling mode for a symbol.
        Checks symbol.filling_mode bit flags and verifies with order_check.
        """
        info = mt5.symbol_info(symbol)
        if not info:
            return mt5.ORDER_FILLING_IOC

        # MT5 symbol.filling_mode bitmask:
        # Bit 1 (value 2): SYMBOL_FILLING_IOC -> mt5.ORDER_FILLING_IOC (1)
        # Bit 0 (value 1): SYMBOL_FILLING_FOK -> mt5.ORDER_FILLING_FOK (0)
        # Bit 2 (value 4): SYMBOL_FILLING_RETURN -> mt5.ORDER_FILLING_RETURN (2)
        candidates = []
        if info.filling_mode & 2:
            candidates.append(mt5.ORDER_FILLING_IOC)
        if info.filling_mode & 1:
            candidates.append(mt5.ORDER_FILLING_FOK)
        if info.filling_mode & 4:
            candidates.append(mt5.ORDER_FILLING_RETURN)

        for m in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
            if m not in candidates:
                candidates.append(m)

        # Quick verification using order_check
        tick = mt5.symbol_info_tick(symbol)
        if tick and tick.ask > 0:
            test_req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": 0.01,
                "type": mt5.ORDER_TYPE_BUY,
                "price": tick.ask,
                "deviation": self.deviation,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            for cand in candidates:
                test_req["type_filling"] = cand
                check_res = mt5.order_check(test_req)
                if check_res and check_res.retcode != 10030:
                    return cand

        return candidates[0] if candidates else mt5.ORDER_FILLING_IOC

    def place_order(
        self,
        symbol: str,
        order_type: str,  # 'BUY' or 'SELL'
        volume: float,
        sl_points: int = 200,
        tp_points: int = 400,
        comment: str = "MT5Bot"
    ) -> Optional[Dict[str, Any]]:
        """
        Submits a market execution order with Stop Loss and Take Profit.
        """
        self.ensure_symbol_active(symbol)
        info = mt5.symbol_info(symbol)
        if not info:
            logger.error(f"Cannot place order: symbol info missing for {symbol}")
            return None

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logger.error(f"Cannot place order: no live tick for {symbol}")
            return None

        point = info.point
        digits = info.digits

        if order_type.upper() == "BUY":
            mt5_order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
            sl = round(price - (sl_points * point), digits) if sl_points > 0 else 0.0
            tp = round(price + (tp_points * point), digits) if tp_points > 0 else 0.0
        elif order_type.upper() == "SELL":
            mt5_order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
            sl = round(price + (sl_points * point), digits) if sl_points > 0 else 0.0
            tp = round(price - (tp_points * point), digits) if tp_points > 0 else 0.0
        else:
            logger.error(f"Invalid order type '{order_type}'")
            return None

        filling_mode = self.get_supported_filling_mode(symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5_order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        logger.info(f"Submitting order: {order_type} {volume} {symbol} @ {price} (SL: {sl}, TP: {tp}, filling_mode: {filling_mode})")
        result = mt5.order_send(request)

        if result is None:
            err = mt5.last_error()
            logger.error(f"Order failed with no response. MT5 Error: {err}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order rejected! Return code: {result.retcode} ({result.comment})")
            return None

        logger.info(f"Order executed successfully! Order ticket #{result.order}, deal #{result.deal}")
        return {
            "ticket": result.order,
            "deal": result.deal,
            "symbol": symbol,
            "direction": order_type.upper(),
            "volume": volume,
            "price": result.price or price,
            "sl": sl,
            "tp": tp,
            "comment": comment,
            "magic": self.magic_number,
            "retcode": result.retcode
        }

    def close_position(self, ticket: int) -> bool:
        """Closes an open position by its ticket."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions or len(positions) == 0:
            logger.warning(f"Position #{ticket} not found or already closed.")
            return False

        pos = positions[0]
        symbol = pos.symbol
        volume = pos.volume
        pos_type = pos.type

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            logger.error(f"No tick data to close position #{ticket}")
            return False

        if pos_type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        filling_mode = self.get_supported_filling_mode(symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": symbol,
            "volume": volume,
            "type": close_type,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": f"Close #{ticket}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        logger.info(f"Closing position #{ticket} {symbol} @ {price}...")
        result = mt5.order_send(request)

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Position #{ticket} closed successfully.")
            return True
        else:
            retcode = result.retcode if result else 'No result'
            comment = result.comment if result else ''
            logger.error(f"Failed to close position #{ticket}: {retcode} {comment}")
            return False

    def shutdown(self):
        """Disconnects cleanly from MT5."""
        if self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info("MT5 connection shut down.")
