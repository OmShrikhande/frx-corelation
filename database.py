import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, PyMongoError

logger = logging.getLogger(__name__)

def sanitize_mongo_value(obj: Any) -> Any:
    """Recursively converts numpy/pandas types to native Python types for MongoDB/BSON serialization."""
    if isinstance(obj, dict):
        return {str(k): sanitize_mongo_value(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [sanitize_mongo_value(v) for v in obj]
    elif hasattr(obj, "item"):  # numpy scalar types (np.bool_, np.int64, np.float64)
        return obj.item()
    return obj

class MongoDatabase:
    """
    MongoDB database wrapper managing:
    1. 'trades': Full lifecycle of executed trades.
    2. 'ledger': Immutable accounting ledger for every financial transaction & PnL settlement.
    3. 'signals': Log of generated trade signals and indicator data.
    4. 'snapshots': Account balance and equity tracking over time.
    """

    def __init__(self, uri: str = "mongodb://localhost:27017/", db_name: str = "mt5_trading_db"):
        self.uri = uri
        self.db_name = db_name
        self.client: Optional[MongoClient] = None
        self.db = None
        
        self.connect()
        self._init_indexes()

    def connect(self):
        try:
            self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            # Verify connection
            self.client.admin.command("ping")
            self.db = self.client[self.db_name]
            logger.info(f"Successfully connected to MongoDB at {self.uri}, database: '{self.db_name}'")
        except ConnectionFailure as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    def _init_indexes(self):
        """Creates appropriate indexes for fast queries and integrity."""
        try:
            self.db.trades.create_index([("ticket", ASCENDING)], unique=True)
            self.db.trades.create_index([("symbol", ASCENDING), ("status", ASCENDING)])
            self.db.ledger.create_index([("timestamp", DESCENDING)])
            self.db.ledger.create_index([("ticket", ASCENDING)])
            self.db.signals.create_index([("timestamp", DESCENDING)])
            self.db.snapshots.create_index([("timestamp", DESCENDING)])
            logger.info("MongoDB indexes verified.")
        except Exception as e:
            logger.warning(f"Could not initialize indexes: {e}")

    # ==========================================
    # Trade Management
    # ==========================================
    def record_open_trade(self, trade_data: Dict[str, Any]) -> bool:
        """Records a newly opened trade in MongoDB."""
        try:
            trade_data = sanitize_mongo_value(trade_data)
            trade_data["status"] = "OPEN"
            trade_data["created_at"] = datetime.now(timezone.utc)
            trade_data["updated_at"] = datetime.now(timezone.utc)
            self.db.trades.update_one(
                {"ticket": trade_data["ticket"]},
                {"$set": trade_data},
                upsert=True
            )
            logger.info(f"Recorded trade #{trade_data['ticket']} ({trade_data.get('symbol')} {trade_data.get('direction')}) in MongoDB.")
            return True
        except PyMongoError as e:
            logger.error(f"Error saving open trade #{trade_data.get('ticket')}: {e}")
            return False

    def record_close_trade(self, ticket: int, close_price: float, close_time: datetime, profit: float, close_reason: str = "STRATEGY_EXIT") -> bool:
        """Updates an existing trade with close details and realized profit."""
        try:
            res = self.db.trades.update_one(
                {"ticket": int(ticket)},
                {
                    "$set": {
                        "status": "CLOSED",
                        "close_price": float(close_price),
                        "close_time": close_time,
                        "profit": float(profit),
                        "close_reason": str(close_reason),
                        "updated_at": datetime.now(timezone.utc)
                    }
                }
            )
            return res.modified_count > 0
        except PyMongoError as e:
            logger.error(f"Error closing trade #{ticket} in MongoDB: {e}")
            return False

    def get_open_trades(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns open trades, optionally filtered by symbol."""
        query = {"status": "OPEN"}
        if symbol:
            query["symbol"] = symbol
        return list(self.db.trades.find(query))

    def get_trade_by_ticket(self, ticket: int) -> Optional[Dict[str, Any]]:
        """Finds a trade by ticket number."""
        return self.db.trades.find_one({"ticket": ticket})

    # ==========================================
    # Ledger Accounting
    # ==========================================
    def record_ledger_entry(
        self,
        event_type: str,
        ticket: Optional[int],
        symbol: Optional[str],
        entry_type: str,  # 'CREDIT', 'DEBIT', 'SETTLEMENT', 'INFO'
        amount: float,
        balance_after: float,
        equity_after: float,
        notes: str = ""
    ) -> bool:
        """
        Records an immutable financial event into the ledger.
        """
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc),
                "event_type": str(event_type),
                "ticket": int(ticket) if ticket is not None else None,
                "symbol": str(symbol) if symbol is not None else None,
                "entry_type": str(entry_type),
                "amount": round(float(amount), 4),
                "balance_after": round(float(balance_after), 4),
                "equity_after": round(float(equity_after), 4),
                "notes": str(notes)
            }
            self.db.ledger.insert_one(entry)
            logger.info(f"Ledger entry recorded: {event_type} | {symbol or 'N/A'} | Amount: ${amount:.2f} | Balance: ${balance_after:.2f}")
            return True
        except PyMongoError as e:
            logger.error(f"Failed to record ledger entry: {e}")
            return False

    def get_recent_ledger_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetches the latest ledger entries."""
        return list(self.db.ledger.find().sort("timestamp", DESCENDING).limit(limit))

    # ==========================================
    # Signals & Snapshots
    # ==========================================
    def record_signal(self, symbol: str, signal: str, price: float, indicators: Dict[str, Any], executed: bool, reason: str = ""):
        """Logs an analysis signal generated by the strategy."""
        try:
            doc = {
                "timestamp": datetime.now(timezone.utc),
                "symbol": str(symbol),
                "signal": str(signal),
                "price": float(price),
                "indicators": sanitize_mongo_value(indicators),
                "executed": bool(executed),
                "reason": str(reason)
            }
            self.db.signals.insert_one(doc)
        except PyMongoError as e:
            logger.warning(f"Failed to log signal for {symbol}: {e}")

    def record_snapshot(self, balance: float, equity: float, margin: float, free_margin: float, open_positions_count: int):
        """Records a periodic terminal account snapshot."""
        try:
            self.db.snapshots.insert_one({
                "timestamp": datetime.now(timezone.utc),
                "balance": balance,
                "equity": equity,
                "margin": margin,
                "free_margin": free_margin,
                "open_positions": open_positions_count
            })
        except PyMongoError as e:
            logger.warning(f"Failed to record balance snapshot: {e}")

    def close(self):
        """Closes MongoDB connection."""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed.")
