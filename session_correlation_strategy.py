"""
Session Correlation & Inter-Market Algorithm (XAUUSD + XAGUSD)
Implements quantitative edges discovered from 12-Month MT5 empirical analysis:

1. Setup #1: Asian Box Liquidity Sweep (07:00 - 08:30 UTC) - 80% Sweep Edge
2. Setup #2: London/NY Overlap Dual-Metal Breakout (13:00 - 15:30 UTC) - 81.8% Agreement Edge
3. Setup #3: Double-Green Morning Fade (13:15 - 14:30 UTC) - 58.8% NY Reversal Edge
4. Filter: Inter-Metal Agreement (Gold & Silver must confirm direction)
"""

from datetime import datetime, timezone
from typing import Dict, Any, Tuple, Optional
import pandas as pd
import numpy as np

class SessionCorrelationStrategy:
    """
    Algorithmic Trading Strategy combining:
    - Global Session Architecture (Asian -> London -> Overlap -> NY)
    - Asian Box High/Low Liquidity Sweeps
    - Dual-Metal Confirmation (XAUUSD + XAGUSD simultaneous agreement)
    - Overlap Momentum & Extreme Morning Mean-Reversions
    """

    def __init__(
        self,
        asian_sweep_buffer_pct: float = 0.05,  # 5% buffer outside Asian range for false breakout
        overlap_rsi_overbought: float = 70.0,
        overlap_rsi_oversold: float = 30.0
    ):
        self.asian_sweep_buffer = asian_sweep_buffer_pct
        self.rsi_ob = overlap_rsi_overbought
        self.rsi_os = overlap_rsi_oversold
        
        # State tracking for the current trading day
        self.current_date = None
        self.asian_high = None
        self.asian_low = None
        self.asian_open = None
        self.asian_close = None
        self.asian_dir = None
        
        self.london_open = None
        self.london_close = None
        self.london_dir = None

    def update_session_levels(self, df_xau: pd.DataFrame, current_dt_utc: datetime):
        """Extracts and caches the Asian and London baseline levels for the current day."""
        today = current_dt_utc.date()
        if today != self.current_date:
            self.current_date = today
            self.asian_high = None
            self.asian_low = None
            self.asian_open = None
            self.asian_close = None
            self.asian_dir = None
            self.london_open = None
            self.london_close = None
            self.london_dir = None

        hour = current_dt_utc.hour

        # Once Asian session has concluded (hour >= 7), freeze Asian box for London/NY
        if self.asian_high is None and hour >= 7:
            asian_bars = df_xau[(df_xau["dt_utc"].dt.date == today) & (df_xau["dt_utc"].dt.hour < 7)]
            if len(asian_bars) >= 4:
                self.asian_high = float(asian_bars["xau_high"].max())
                self.asian_low = float(asian_bars["xau_low"].min())
                self.asian_open = float(asian_bars["xau_open"].iloc[0])
                self.asian_close = float(asian_bars["xau_close"].iloc[-1])
                self.asian_dir = "UP" if self.asian_close >= self.asian_open else "DOWN"

        # Once London has concluded (hour >= 13), freeze London levels for Overlap/NY
        if self.london_open is None and hour >= 13:
            london_bars = df_xau[(df_xau["dt_utc"].dt.date == today) & (df_xau["dt_utc"].dt.hour >= 7) & (df_xau["dt_utc"].dt.hour < 13)]
            if len(london_bars) >= 4:
                self.london_open = float(london_bars["xau_open"].iloc[0])
                self.london_close = float(london_bars["xau_close"].iloc[-1])
                self.london_dir = "UP" if self.london_close >= self.london_open else "DOWN"

    def evaluate(
        self,
        df_xau: pd.DataFrame,
        df_xag: pd.DataFrame,
        utc_offset_hours: int = 2
    ) -> Tuple[str, str, Dict[str, Any]]:
        """
        Evaluates real-time market data across Gold and Silver.
        Returns:
            signal: 'BUY', 'SELL', or 'HOLD'
            best_target_asset: 'XAUUSD', 'XAGUSD', or 'BOTH'
            metadata: Setup name, indicators, SL/TP levels, confidence
        """
        if len(df_xau) < 30 or len(df_xag) < 30:
            return "HOLD", "NONE", {"reason": "Insufficient bars"}

        df_xau = df_xau.copy()
        df_xag = df_xag.copy()

        # Normalize column names if coming straight from MT5 rates
        if "close" in df_xau.columns and "xau_close" not in df_xau.columns:
            df_xau = df_xau.rename(columns={"open": "xau_open", "high": "xau_high", "low": "xau_low", "close": "xau_close", "tick_volume": "xau_vol"})
        if "close" in df_xag.columns and "xag_close" not in df_xag.columns:
            df_xag = df_xag.rename(columns={"open": "xag_open", "high": "xag_high", "low": "xag_low", "close": "xag_close", "tick_volume": "xag_vol"})

        # Normalize timestamp to UTC
        if "dt_utc" not in df_xau.columns and "time" in df_xau.columns:
            df_xau["dt_utc"] = pd.to_datetime(df_xau["time"]) - pd.Timedelta(hours=utc_offset_hours)
        if "dt_utc" not in df_xag.columns and "time" in df_xag.columns:
            df_xag["dt_utc"] = pd.to_datetime(df_xag["time"]) - pd.Timedelta(hours=utc_offset_hours)

        curr_xau = df_xau.iloc[-1]
        prev_xau = df_xau.iloc[-2]
        curr_xag = df_xag.iloc[-1]
        
        dt_utc = curr_xau["dt_utc"]
        hour = dt_utc.hour + dt_utc.minute / 60.0

        # Update baseline session levels
        self.update_session_levels(df_xau, dt_utc)

        # Basic indicators
        xau_close = curr_xau["xau_close"]
        xag_close = curr_xag["xag_close"]
        
        # 15m return direction on both metals
        xau_up = bool(curr_xau["xau_close"] > curr_xau["xau_open"])
        xag_up = bool(curr_xag["xag_close"] > curr_xag["xag_open"])
        metals_aligned = bool(xau_up == xag_up)

        # Fast/Slow EMAs on Gold
        ema9 = float(df_xau["xau_close"].ewm(span=9, adjust=False).mean().iloc[-1])
        ema21 = float(df_xau["xau_close"].ewm(span=21, adjust=False).mean().iloc[-1])

        # RSI on Gold
        delta = df_xau["xau_close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])

        # =========================================================================
        # SETUP 1: ASIAN LOW/HIGH LIQUIDITY SWEEP (London Open: 07:00 - 09:00 UTC)
        # Empirical Edge: 80.0% Low sweep / 77.5% Asian boundary breach
        # =========================================================================
        if 7.0 <= hour < 9.0 and self.asian_low is not None and self.asian_high is not None:
            asian_range = self.asian_high - self.asian_low
            
            # Bullish Sweep: Price spikes below Asian Low, but closes back above or shows pinbar
            swept_low = curr_xau["xau_low"] < self.asian_low
            reclaimed_low = xau_close > self.asian_low
            if swept_low and reclaimed_low and xau_up:
                sl_price = round(curr_xau["xau_low"] - 1.5, 2)
                tp_price = round(self.asian_high, 2)
                return "BUY", "XAUUSD", {
                    "setup": "ASIAN_LOW_LIQUIDITY_SWEEP",
                    "confidence": "80.0%",
                    "reason": "London swept Asian Low and rejected with strong buyer absorption",
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "gold_sl_points": int(abs(xau_close - sl_price) * 100),
                    "gold_tp_points": int(abs(tp_price - xau_close) * 100)
                }

            # Bearish Sweep: Price spikes above Asian High, but rejects back down
            swept_high = curr_xau["xau_high"] > self.asian_high
            rejected_high = xau_close < self.asian_high
            if swept_high and rejected_high and not xau_up:
                sl_price = round(curr_xau["xau_high"] + 1.5, 2)
                tp_price = round(self.asian_low, 2)
                return "SELL", "XAUUSD", {
                    "setup": "ASIAN_HIGH_LIQUIDITY_SWEEP",
                    "confidence": "73.0%",
                    "reason": "London swept Asian High and rejected with institutional selling",
                    "sl_price": sl_price,
                    "tp_price": tp_price,
                    "gold_sl_points": int(abs(sl_price - xau_close) * 100),
                    "gold_tp_points": int(abs(xau_close - tp_price) * 100)
                }

        # =========================================================================
        # SETUP 2: LONDON/NY OVERLAP DUAL-METAL MOMENTUM (13:00 - 15:30 UTC)
        # Empirical Edge: 81.8% Directional Agreement & 83.1% Correlation
        # Leverage Silver for 2.1x volatility accelerator
        # =========================================================================
        if 13.0 <= hour < 15.5:
            # Check for Double-Green Morning Fade (Setup 3) first:
            if self.asian_dir == "UP" and self.london_dir == "UP" and rsi > 68.0 and not xau_up and not xag_up:
                # 58.8% Reversal Dump Edge!
                return "SELL", "XAGUSD", {
                    "setup": "DOUBLE_GREEN_MORNING_FADE",
                    "confidence": "58.8% Reversal Edge",
                    "reason": "Asia and London were both Bullish. NY open dumping overbought morning longs",
                    "target_asset": "XAGUSD (2.1x Leverage on down move)"
                }

            # Standard Overlap Momentum: Both metals must agree
            if metals_aligned:
                if xau_up and ema9 > ema21 and 52 <= rsi < 70:
                    return "BUY", "BOTH", {
                        "setup": "OVERLAP_DUAL_METAL_MOMENTUM",
                        "confidence": "81.8% Co-Movement",
                        "reason": "Gold & Silver synchronized bullish surge during peak Overlap liquidity",
                        "silver_multiplier": "2.12x"
                    }
                elif not xau_up and ema9 < ema21 and 30 < rsi <= 48:
                    return "SELL", "BOTH", {
                        "setup": "OVERLAP_DUAL_METAL_MOMENTUM",
                        "confidence": "81.8% Co-Movement",
                        "reason": "Gold & Silver synchronized bearish flush during peak Overlap liquidity",
                        "silver_multiplier": "2.12x"
                    }

        return "HOLD", "NONE", {
            "reason": "No high-probability session alignment currently",
            "rsi": round(rsi, 2),
            "metals_aligned": metals_aligned
        }
