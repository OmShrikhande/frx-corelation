import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
import MetaTrader5 as mt5

# Load environment variables
load_dotenv()

# Standard MT5 executable paths on Windows to check automatically
COMMON_MT5_PATHS = [
    r"C:\MT5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\Vantage Markets MetaTrader 5\terminal64.exe",
    r"C:\Program Files\IC Markets Global MT5\terminal64.exe",
    r"C:\Program Files\FTMO MetaTrader 5\terminal64.exe",
]

def find_mt5_path() -> str:
    """Finds an existing MT5 terminal64.exe path from environment or common locations."""
    env_path = os.getenv("MT5_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return env_path
    
    for candidate in COMMON_MT5_PATHS:
        if Path(candidate).exists():
            return candidate
            
    return ""

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

class Config:
    # MT5 Terminal Settings
    MT5_PATH: str = find_mt5_path()
    MT5_LOGIN: int = int(os.getenv("MT5_LOGIN")) if os.getenv("MT5_LOGIN") else None
    MT5_PASSWORD: str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER: str = os.getenv("MT5_SERVER", "")
    
    # Pairs (default 5 liquid majors)
    raw_pairs = os.getenv("TRADING_PAIRS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD")
    TRADING_PAIRS: list[str] = [p.strip().upper() for p in raw_pairs.split(",") if p.strip()]
    
    # Timeframe
    TIMEFRAME_STR: str = os.getenv("MT5_TIMEFRAME", "M5").upper()
    TIMEFRAME: int = TIMEFRAME_MAP.get(TIMEFRAME_STR, mt5.TIMEFRAME_M5)
    
    # Default Risk & Orders
    LOT_SIZE: float = float(os.getenv("LOT_SIZE", "0.01"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    MAX_POSITIONS_PER_SYMBOL: int = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1"))
    STOP_LOSS_POINTS: int = int(os.getenv("STOP_LOSS_POINTS", "200"))
    TAKE_PROFIT_POINTS: int = int(os.getenv("TAKE_PROFIT_POINTS", "400"))
    DEVIATION_SLIPPAGE: int = int(os.getenv("DEVIATION_SLIPPAGE", "10"))
    MAGIC_NUMBER: int = int(os.getenv("MAGIC_NUMBER", "778899"))
    
    # Per-pair custom profiles for lot sizing and Stop Loss / Take Profit
    # Tailored to contract sizes and volatility (e.g., Gold & Silver vs Forex)
    PAIR_PROFILES: dict[str, dict[str, Any]] = {
        # Forex Majors (100,000 units, digits 5, 20 pips = 200 pts)
        "EURUSD": {"lot": 0.02, "sl_points": 200, "tp_points": 400},
        "GBPUSD": {"lot": 0.01, "sl_points": 250, "tp_points": 500},
        "USDJPY": {"lot": 0.02, "sl_points": 200, "tp_points": 400},
        "AUDUSD": {"lot": 0.01, "sl_points": 200, "tp_points": 400},
        "USDCAD": {"lot": 0.02, "sl_points": 200, "tp_points": 400},
        # Precious Metals (Commodities)
        # Gold: 100 oz contract. 1500 pts = $15.00 SL, 3000 pts = $30.00 TP
        "XAUUSD": {"lot": 0.01, "sl_points": 1500, "tp_points": 3000},
        # Silver: 5,000 oz contract. 500 pts = $0.50 SL, 1000 pts = $1.00 TP
        "XAGUSD": {"lot": 0.01, "sl_points": 500, "tp_points": 1000},
    }

    @classmethod
    def get_lot_size(cls, symbol: str) -> float:
        """Returns the specific lot size configured for a pair or default."""
        env_override = os.getenv(f"LOT_{symbol}")
        if env_override:
            return float(env_override)
        profile = cls.PAIR_PROFILES.get(symbol.upper(), {})
        return profile.get("lot", cls.LOT_SIZE)

    @classmethod
    def get_sl_points(cls, symbol: str) -> int:
        """Returns specific Stop Loss points for a pair or default."""
        profile = cls.PAIR_PROFILES.get(symbol.upper(), {})
        return profile.get("sl_points", cls.STOP_LOSS_POINTS)

    @classmethod
    def get_tp_points(cls, symbol: str) -> int:
        """Returns specific Take Profit points for a pair or default."""
        profile = cls.PAIR_PROFILES.get(symbol.upper(), {})
        return profile.get("tp_points", cls.TAKE_PROFIT_POINTS)
    
    # Strategy settings
    FAST_EMA_PERIOD: int = int(os.getenv("FAST_EMA_PERIOD", "9"))
    SLOW_EMA_PERIOD: int = int(os.getenv("SLOW_EMA_PERIOD", "21"))
    RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))
    RSI_OVERBOUGHT: float = float(os.getenv("RSI_OVERBOUGHT", "70"))
    RSI_OVERSOLD: float = float(os.getenv("RSI_OVERSOLD", "30"))
    
    # MongoDB
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "mt5_trading_db")
    
    # Loop
    LOOP_INTERVAL_SECONDS: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "5"))

config = Config()
