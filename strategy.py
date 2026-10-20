import pandas as pd
import numpy as np
from typing import Dict, Any, Tuple

class TradingStrategy:
    """
    Multi-pair Trend Following Strategy with EMA Crossover & RSI Confirmation.
    """

    def __init__(
        self,
        fast_ema: int = 9,
        slow_ema: int = 21,
        rsi_period: int = 14,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0
    ):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculates Fast EMA, Slow EMA, RSI, and ATR indicators."""
        df = df.copy()

        # Exponential Moving Averages
        df["ema_fast"] = df["close"].ewm(span=self.fast_ema, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.slow_ema, adjust=False).mean()

        # Relative Strength Index (RSI)
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.rolling(window=self.rsi_period, min_periods=self.rsi_period).mean()
        avg_loss = loss.rolling(window=self.rsi_period, min_periods=self.rsi_period).mean()

        rs = avg_gain / (avg_loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Average True Range (ATR)
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"] = true_range.rolling(window=14).mean()

        return df

    def evaluate(self, df: pd.DataFrame) -> Tuple[str, Dict[str, Any]]:
        """
        Analyzes the latest completed bar and current bar to produce:
        signal: 'BUY', 'SELL', or 'HOLD'
        indicator_data: snapshot of values at decision time
        """
        if len(df) < max(self.slow_ema, self.rsi_period) + 5:
            return "HOLD", {"reason": "Insufficient data bars"}

        df_ind = self.calculate_indicators(df)

        curr = df_ind.iloc[-1]
        prev = df_ind.iloc[-2]

        ema_fast_curr = float(curr["ema_fast"])
        ema_slow_curr = float(curr["ema_slow"])
        ema_fast_prev = float(prev["ema_fast"])
        ema_slow_prev = float(prev["ema_slow"])
        rsi_curr = float(curr["rsi"])
        close_curr = float(curr["close"])
        atr_curr = float(curr["atr"]) if not np.isnan(curr["atr"]) else 0.0

        indicator_snapshot = {
            "close": round(close_curr, 5),
            "ema_fast": round(ema_fast_curr, 5),
            "ema_slow": round(ema_slow_curr, 5),
            "rsi": round(rsi_curr, 2),
            "atr": round(atr_curr, 5)
        }

        # Check Crossovers
        bullish_crossover = (ema_fast_prev <= ema_slow_prev) and (ema_fast_curr > ema_slow_curr)
        bearish_crossover = (ema_fast_prev >= ema_slow_prev) and (ema_fast_curr < ema_slow_curr)

        # Bullish condition: Fast crosses above slow OR strong trend with healthy RSI
        if bullish_crossover or (ema_fast_curr > ema_slow_curr and 52 <= rsi_curr < self.rsi_overbought):
            if rsi_curr < self.rsi_overbought:
                indicator_snapshot["reason"] = "Bullish EMA alignment with confirmed RSI"
                return "BUY", indicator_snapshot

        # Bearish condition: Fast crosses below slow OR strong downtrend with healthy RSI
        if bearish_crossover or (ema_fast_curr < ema_slow_curr and self.rsi_oversold < rsi_curr <= 48):
            if rsi_curr > self.rsi_oversold:
                indicator_snapshot["reason"] = "Bearish EMA alignment with confirmed RSI"
                return "SELL", indicator_snapshot

        indicator_snapshot["reason"] = "No clear trend or extreme RSI"
        return "HOLD", indicator_snapshot
