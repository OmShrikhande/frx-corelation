"""
Self-diagnostic & Integration test script to verify:
1. MongoDB connectivity and ledger read/write.
2. MT5 auto-launch, connection, and account balance.
3. Live tick and bar data fetch for all 5 pairs.
4. Strategy signal generation.
"""

import sys
from config import config
from database import MongoDatabase
from mt5_connector import MT5Connector
from strategy import TradingStrategy

def run_diagnostics():
    print("=" * 60)
    print("RUNNING MT5 & MONGODB INTEGRATION DIAGNOSTICS")
    print("=" * 60)

    # 1. MongoDB Check
    print("\n[1/4] Checking MongoDB Connection...")
    try:
        db = MongoDatabase(uri=config.MONGO_URI, db_name=config.MONGO_DB_NAME)
        print(" -> MongoDB connection OK.")
        # Test ledger write
        test_success = db.record_ledger_entry(
            event_type="SYSTEM_DIAGNOSTIC",
            ticket=None,
            symbol="TEST",
            entry_type="INFO",
            amount=0.0,
            balance_after=0.0,
            equity_after=0.0,
            notes="Diagnostics verification entry"
        )
        print(f" -> Test ledger write: {'SUCCESS' if test_success else 'FAILED'}")
    except Exception as e:
        print(f" -> MongoDB Error: {e}")
        return False

    # 2. MT5 Connection & Terminal Check
    print("\n[2/4] Checking MetaTrader 5 Terminal...")
    mt5_conn = MT5Connector(
        path=config.MT5_PATH,
        login=config.MT5_LOGIN,
        password=config.MT5_PASSWORD,
        server=config.MT5_SERVER,
        magic_number=config.MAGIC_NUMBER
    )
    is_running = mt5_conn.is_terminal_process_running()
    print(f" -> Terminal process currently running: {is_running}")
    
    init_ok = mt5_conn.initialize()
    if not init_ok:
        print(" -> Failed to initialize MT5.")
        return False
    print(" -> MT5 initialize: SUCCESS")

    acc = mt5_conn.get_account_info()
    if acc:
        print(f" -> Account Login: {acc['login']} | Server: {acc['server']}")
        print(f" -> Balance: ${acc['balance']:.2f} | Equity: ${acc['equity']:.2f} {acc['currency']}")
    else:
        print(" -> Failed to retrieve account info.")
        return False

    # 3. Market Data for Top 5 Pairs
    print(f"\n[3/4] Checking Live Market Data for 5 Pairs: {config.TRADING_PAIRS}")
    strategy = TradingStrategy()
    
    for symbol in config.TRADING_PAIRS:
        active = mt5_conn.ensure_symbol_active(symbol)
        if not active:
            print(f" -> {symbol}: Failed to activate symbol")
            continue
            
        tick = mt5_conn.get_tick(symbol)
        df = mt5_conn.get_rates(symbol, config.TIMEFRAME, count=50)

        if tick and df is not None:
            signal, ind = strategy.evaluate(df)
            print(f" -> {symbol:<8}: Ask={tick['ask']:<9.5f} Bid={tick['bid']:<9.5f} Spread={tick['spread']:<5} | Signal={signal:<4} (RSI={ind.get('rsi', 0)})")
        else:
            print(f" -> {symbol:<8}: Missing tick or rate data")

    # 4. Open Positions Check
    print("\n[4/4] Checking Active Positions...")
    positions = mt5_conn.get_open_positions()
    print(f" -> Total open positions in MT5: {len(positions)}")
    for p in positions:
        print(f"    - Ticket #{p['ticket']}: {p['symbol']} {('BUY' if p['type'] == 0 else 'SELL')} {p['volume']} lots, Profit: ${p['profit']:.2f}")

    print("\n" + "=" * 60)
    print("ALL CHECKS COMPLETED SUCCESSFULLY!")
    print("=" * 60)
    
    mt5_conn.shutdown()
    db.close()
    return True

if __name__ == "__main__":
    success = run_diagnostics()
    sys.exit(0 if success else 1)
