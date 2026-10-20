import sys
from datetime import datetime
from pymongo import MongoClient
from config import config

def view_ledger(limit: int = 25):
    client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=3000)
    db = client[config.MONGO_DB_NAME]

    print("=" * 80)
    print(f"       METATRADER 5 AUDIT LEDGER & TRADE JOURNAL (MongoDB: {config.MONGO_DB_NAME})")
    print("=" * 80)

    # 1. Summary Statistics
    total_trades = db.trades.count_documents({})
    open_trades = db.trades.count_documents({"status": "OPEN"})
    closed_trades = list(db.trades.find({"status": "CLOSED"}))
    
    winning_trades = [t for t in closed_trades if t.get("profit", 0) > 0]
    losing_trades = [t for t in closed_trades if t.get("profit", 0) < 0]
    total_realized_pnl = sum(t.get("profit", 0) for t in closed_trades)
    win_rate = (len(winning_trades) / len(closed_trades) * 100) if closed_trades else 0.0

    print(f"Total Logged Trades : {total_trades}")
    print(f"Currently Open      : {open_trades}")
    print(f"Closed Positions    : {len(closed_trades)}")
    print(f"Win Rate            : {win_rate:.1f}% ({len(winning_trades)} wins / {len(losing_trades)} losses)")
    print(f"Total Realized PnL  : ${total_realized_pnl:+.2f}")
    print("-" * 80)

    # 2. Open Trades
    open_docs = list(db.trades.find({"status": "OPEN"}))
    if open_docs:
        print("\n[ACTIVE OPEN TRADES IN MONGODB]")
        print(f"{'Ticket':<10} {'Symbol':<10} {'Dir':<6} {'Volume':<8} {'Open Price':<12} {'SL':<10} {'TP':<10} {'Opened At'}")
        print("-" * 80)
        for t in open_docs:
            open_time_str = t.get("open_time", "")
            if isinstance(open_time_str, datetime):
                open_time_str = open_time_str.strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"{t.get('ticket', 0):<10} "
                f"{t.get('symbol', ''):<10} "
                f"{t.get('direction', ''):<6} "
                f"{t.get('volume', 0.0):<8} "
                f"{t.get('price', 0.0):<12.5f} "
                f"{t.get('sl', 0.0):<10.5f} "
                f"{t.get('tp', 0.0):<10.5f} "
                f"{open_time_str}"
            )
    else:
        print("\n[ACTIVE OPEN TRADES]: None currently open.")

    # 3. Recent Ledger Transactions
    ledger_entries = list(db.ledger.find().sort("timestamp", -1).limit(limit))
    print(f"\n[RECENT LEDGER TRANSACTIONS (Last {len(ledger_entries)})]")
    print(f"{'Timestamp':<20} {'Event':<22} {'Ticket':<10} {'Symbol':<8} {'Type':<10} {'Amount':<10} {'Balance After':<14} {'Notes'}")
    print("-" * 110)

    for entry in reversed(ledger_entries):
        ts = entry.get("timestamp")
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)[:19]
        ticket = str(entry.get("ticket") or "-")
        sym = str(entry.get("symbol") or "-")
        amt = entry.get("amount", 0.0)
        bal = entry.get("balance_after", 0.0)
        event = entry.get("event_type", "")
        etype = entry.get("entry_type", "")
        notes = entry.get("notes", "")

        amt_str = f"${amt:+.2f}" if amt != 0 else "$0.00"
        print(f"{ts_str:<20} {event:<22} {ticket:<10} {sym:<8} {etype:<10} {amt_str:<10} ${bal:<13.2f} {notes}")

    print("=" * 110)

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    view_ledger(limit=limit)
