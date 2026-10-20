import time
import signal
import sys
import logging
from datetime import datetime, timezone
import MetaTrader5 as mt5

from config import config
from database import MongoDatabase
from mt5_connector import MT5Connector
from session_correlation_strategy import SessionCorrelationStrategy

# Configure logging with clean format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MT5Bot")

class MetaTraderBot:
    def __init__(self):
        self.running = True
        logger.info("Initializing MetaTrader 5 Session Correlation Trading Bot...")
        
        # 1. Initialize MongoDB
        self.db = MongoDatabase(
            uri=config.MONGO_URI,
            db_name=config.MONGO_DB_NAME
        )
        
        # 2. Initialize MT5 Connector (Auto-launches terminal if not running)
        self.mt5 = MT5Connector(
            path=config.MT5_PATH,
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
            magic_number=config.MAGIC_NUMBER,
            deviation=config.DEVIATION_SLIPPAGE
        )
        
        # 3. Session Correlation Strategy (Gold & Silver Inter-Market)
        self.strategy = SessionCorrelationStrategy()
        self.last_trade_time: float = 0.0
        self.trade_cooldown_seconds: float = 1800.0  # 30-minute cooldown between trades

        # Handle termination signals
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, sig, frame):
        logger.info("Shutdown signal received. Stopping bot gracefully...")
        self.running = False

    def start(self):
        """Starts the bot execution loop."""
        if not self.mt5.initialize():
            logger.critical("Could not connect to MT5. Exiting.")
            return

        acc = self.mt5.get_account_info()
        if not acc:
            logger.critical("Cannot read MT5 account. Exiting.")
            return

        logger.info("=" * 60)
        logger.info(f"Session Correlation Bot Active on Account: {acc['login']} ({acc['company']})")
        logger.info(f"Balance: ${acc['balance']:.2f} | Equity: ${acc['equity']:.2f} | Currency: {acc['currency']}")
        logger.info("Strategy: Session Correlation & Dual-Metal Inter-Market (XAUUSD + XAGUSD)")
        logger.info("Timeframe: M15 | Avg Trades: 1-2 High-Conviction Trades / Day")
        logger.info(f"MongoDB Target: {config.MONGO_URI} [{config.MONGO_DB_NAME}]")
        logger.info("=" * 60)

        # Record initial ledger balance event if ledger is empty
        recent_ledger = self.db.get_recent_ledger_entries(limit=1)
        if not recent_ledger:
            self.db.record_ledger_entry(
                event_type="INITIAL_BALANCE",
                ticket=None,
                symbol=None,
                entry_type="CREDIT",
                amount=acc["balance"],
                balance_after=acc["balance"],
                equity_after=acc["equity"],
                notes=f"Initial account balance recorded from MT5 terminal ({acc['server']})"
            )

        # Ensure both Gold and Silver are active in Market Watch
        trading_metals = ["XAUUSD", "XAGUSD"]
        for sym in trading_metals:
            if not self.mt5.ensure_symbol_active(sym):
                logger.warning(f"Could not activate {sym} in Market Watch.")

        # Main Trading Loop
        cycle_count = 0
        while self.running:
            try:
                cycle_count += 1
                self.reconcile_positions_and_ledger()

                acc = self.mt5.get_account_info()
                if not acc:
                    logger.warning("Terminal disconnected or unresponsive, attempting reconnect...")
                    time.sleep(2)
                    self.mt5.initialize()
                    continue

                open_positions = self.mt5.get_open_positions()
                active_symbols = {p["symbol"] for p in open_positions}

                # Fetch synchronized M15 rates for both XAUUSD and XAGUSD
                df_xau = self.mt5.get_rates("XAUUSD", mt5.TIMEFRAME_M15, count=100)
                df_xag = self.mt5.get_rates("XAGUSD", mt5.TIMEFRAME_M15, count=100)

                if df_xau is not None and df_xag is not None and len(df_xau) >= 30 and len(df_xag) >= 30:
                    # Evaluate Session Correlation Strategy with dynamic broker UTC offset
                    broker_offset = self.mt5.get_broker_utc_offset_hours()
                    signal_type, target_asset, meta = self.strategy.evaluate(df_xau, df_xag, utc_offset_hours=broker_offset)

                    # Determine target execution symbols
                    if target_asset == "BOTH":
                        target_symbols = ["XAUUSD", "XAGUSD"]
                    elif target_asset in ("XAUUSD", "XAGUSD"):
                        target_symbols = [target_asset]
                    else:
                        target_symbols = []

                    now_time = time.time()
                    cooldown_ok = (now_time - self.last_trade_time) >= self.trade_cooldown_seconds
                    can_open_more = len(open_positions) < config.MAX_OPEN_POSITIONS

                    # Execute if valid signal and cooldown passed
                    if signal_type in ("BUY", "SELL") and cooldown_ok and can_open_more and target_symbols:
                        logger.info(
                            f"[STRATEGY TRIGGER] Signal: {signal_type} | Setup: {meta.get('setup')} | "
                            f"Target: {target_asset} | Confidence: {meta.get('confidence', 'High')}"
                        )

                        executed_any = False
                        for symbol in target_symbols:
                            if symbol in active_symbols:
                                logger.info(f"Skipping {symbol}: open position already active.")
                                continue

                            # Execute order
                            symbol_lot = config.get_lot_size(symbol)
                            symbol_sl = meta.get("gold_sl_points", config.get_sl_points(symbol)) if symbol == "XAUUSD" else config.get_sl_points(symbol)
                            symbol_tp = meta.get("gold_tp_points", config.get_tp_points(symbol)) if symbol == "XAUUSD" else config.get_tp_points(symbol)

                            order_result = self.mt5.place_order(
                                symbol=symbol,
                                order_type=signal_type,
                                volume=symbol_lot,
                                sl_points=symbol_sl,
                                tp_points=symbol_tp,
                                comment=meta.get("setup", "SessionBot")[:25]
                            )

                            if order_result:
                                executed_any = True
                                order_result["open_time"] = datetime.now(timezone.utc)
                                order_result["setup"] = meta.get("setup")
                                order_result["strategy_meta"] = meta
                                self.db.record_open_trade(order_result)

                                acc_current = self.mt5.get_account_info() or acc
                                self.db.record_ledger_entry(
                                    event_type="TRADE_OPEN",
                                    ticket=order_result["ticket"],
                                    symbol=symbol,
                                    entry_type="POSITION_ENTRY",
                                    amount=0.0,
                                    balance_after=acc_current["balance"],
                                    equity_after=acc_current["equity"],
                                    notes=f"[{meta.get('setup')}] {signal_type} {symbol_lot} lots @ {order_result['price']} (SL: {order_result['sl']}, TP: {order_result['tp']})"
                                )
                                active_symbols.add(symbol)
                                open_positions.append({"ticket": order_result["ticket"], "symbol": symbol})

                        if executed_any:
                            self.last_trade_time = now_time

                    # Log signal to MongoDB for auditing
                    tick_xau = self.mt5.get_tick("XAUUSD")
                    self.db.record_signal(
                        symbol="XAUUSD",
                        signal=signal_type,
                        price=tick_xau["ask"] if tick_xau else 0.0,
                        indicators=meta,
                        executed=(signal_type in ("BUY", "SELL") and cooldown_ok and can_open_more),
                        reason=meta.get("reason", "")
                    )

                # Record periodic snapshot every 6 cycles (approx 30s)
                if cycle_count % 6 == 0:
                    self.db.record_snapshot(
                        balance=acc["balance"],
                        equity=acc["equity"],
                        margin=acc["margin"],
                        free_margin=acc["margin_free"],
                        open_positions_count=len(open_positions)
                    )
                    logger.info(
                        f"[Heartbeat] Balance: ${acc['balance']:.2f} | Equity: ${acc['equity']:.2f} | "
                        f"Floating PnL: ${acc['profit']:+.2f} | Open Positions: {len(open_positions)}"
                    )

                time.sleep(config.LOOP_INTERVAL_SECONDS)

            except Exception as e:
                logger.error(f"Error during bot execution loop: {e}", exc_info=True)
                time.sleep(3)

        self.cleanup()

    def reconcile_positions_and_ledger(self):
        """
        Cross-checks active positions in MT5 against open trades recorded in MongoDB.
        If a trade was closed (SL, TP, manual exit), updates MongoDB and records realized PnL in ledger.
        """
        try:
            db_open_trades = self.db.get_open_trades()
            if not db_open_trades:
                return

            current_mt5_positions = self.mt5.get_open_positions()
            current_tickets = {p["ticket"] for p in current_mt5_positions}

            for db_trade in db_open_trades:
                ticket = db_trade["ticket"]
                symbol = db_trade.get("symbol", "")

                # If trade is marked OPEN in DB, but no longer in MT5 active positions:
                if ticket not in current_tickets:
                    logger.info(f"Detected closed position #{ticket} for {symbol}. Fetching deal settlement...")
                    # Retrieve closed deal details from MT5 history
                    deals = mt5.history_deals_get(position=ticket)
                    realized_profit = 0.0
                    close_price = 0.0
                    close_time = datetime.now(timezone.utc)
                    close_reason = "POSITION_CLOSED"

                    if deals and len(deals) >= 1:
                        # The closing deal is typically the last deal for the position
                        exit_deal = deals[-1]
                        realized_profit = float(getattr(exit_deal, "profit", 0.0) + getattr(exit_deal, "commission", 0.0) + getattr(exit_deal, "swap", 0.0))
                        close_price = float(getattr(exit_deal, "price", 0.0))
                        deal_time = getattr(exit_deal, "time", None)
                        if deal_time:
                            close_time = datetime.fromtimestamp(deal_time, tz=timezone.utc)
                        deal_ticket = getattr(exit_deal, "ticket", getattr(exit_deal, "order", ticket))
                        close_reason = f"Deal #{deal_ticket} (Comment: {getattr(exit_deal, 'comment', '')})"

                    # 1. Update trade status in MongoDB
                    self.db.record_close_trade(
                        ticket=ticket,
                        close_price=close_price,
                        close_time=close_time,
                        profit=realized_profit,
                        close_reason=close_reason
                    )

                    # 2. Add realized PnL settlement to the immutable Ledger
                    acc = self.mt5.get_account_info()
                    bal_after = acc["balance"] if acc else 0.0
                    eq_after = acc["equity"] if acc else 0.0

                    self.db.record_ledger_entry(
                        event_type="TRADE_CLOSE_SETTLEMENT",
                        ticket=ticket,
                        symbol=symbol,
                        entry_type="REALIZED_PNL",
                        amount=realized_profit,
                        balance_after=bal_after,
                        equity_after=eq_after,
                        notes=f"Position #{ticket} closed at {close_price}. PnL: ${realized_profit:+.2f}. Reason: {close_reason}"
                    )
        except Exception as e:
            logger.error(f"Error reconciling positions: {e}")

    def cleanup(self):
        """Clean shutdown of connections."""
        logger.info("Cleaning up connections...")
        self.mt5.shutdown()
        self.db.close()
        logger.info("Bot stopped cleanly.")

if __name__ == "__main__":
    bot = MetaTraderBot()
    bot.start()
