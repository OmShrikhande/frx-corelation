"""
Live Strategy Dashboard & Upcoming Possibilities Server
Serves a real-time web dashboard on port 5000:
- Live MetaTrader 5 Account & Positions
- Today's Intraday Session Breakdown (Asian Box High/Low vs London High/Low)
- Visual Distance to Liquidity Sweeps
- Upcoming High-Probability Strategy Scenarios & Possibilities with Live Countdowns
- Recent Signal Feed & Immutable Ledger Stream
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, Any, List
import pandas as pd
import MetaTrader5 as mt5

from config import config
from database import MongoDatabase, sanitize_mongo_value
from mt5_connector import MT5Connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("DashboardServer")

# Initialize database
try:
    db = MongoDatabase(uri=config.MONGO_URI, db_name=config.MONGO_DB_NAME)
except Exception as e:
    logger.warning(f"Could not connect to MongoDB: {e}")
    db = None

# Initialize MT5 connector
mt5_conn = MT5Connector(
    path=config.MT5_PATH,
    login=config.MT5_LOGIN,
    password=config.MT5_PASSWORD,
    server=config.MT5_SERVER
)

def get_live_dashboard_data() -> Dict[str, Any]:
    """Aggregates real-time MT5, Session, and MongoDB data for the dashboard."""
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    
    # 1. Connect to MT5 if not already active
    is_mt5_ok = mt5.initialize()
    account_data = {}
    positions_list = []
    prices = {"XAUUSD": None, "XAGUSD": None}
    broker_offset = 3

    if is_mt5_ok:
        acc = mt5.account_info()
        if acc:
            account_data = {
                "login": acc.login,
                "server": acc.server,
                "company": acc.company,
                "currency": acc.currency,
                "balance": round(acc.balance, 2),
                "equity": round(acc.equity, 2),
                "profit": round(acc.profit, 2),
                "margin": round(acc.margin, 2),
                "margin_free": round(acc.margin_free, 2),
                "leverage": acc.leverage
            }

        # Calculate exact broker offset in hours
        tick_xau = mt5.symbol_info_tick("XAUUSD")
        if tick_xau and tick_xau.time:
            broker_dt = datetime.fromtimestamp(tick_xau.time, tz=timezone.utc)
            broker_offset = int(round((broker_dt - now_utc).total_seconds() / 3600.0))
            prices["XAUUSD"] = {
                "bid": round(tick_xau.bid, 2),
                "ask": round(tick_xau.ask, 2),
                "spread": round((tick_xau.ask - tick_xau.bid) * 100, 1),
                "time": broker_dt.strftime("%H:%M:%S")
            }

        tick_xag = mt5.symbol_info_tick("XAGUSD")
        if tick_xag:
            prices["XAGUSD"] = {
                "bid": round(tick_xag.bid, 3),
                "ask": round(tick_xag.ask, 3),
                "spread": round((tick_xag.ask - tick_xag.bid) * 1000, 1)
            }

        # Open positions
        pos = mt5.positions_get()
        if pos:
            for p in pos:
                positions_list.append({
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "open_price": round(p.price_open, 2),
                    "current_price": round(p.price_current, 2),
                    "sl": round(p.sl, 2),
                    "tp": round(p.tp, 2),
                    "profit": round(p.profit, 2),
                    "comment": p.comment
                })

    # 2. Extract Session Breakdown for Today
    today_analysis = {
        "status": "AWAITING_DATA",
        "asian_box": {"high": 0.0, "low": 0.0, "range": 0.0, "direction": "NEUTRAL"},
        "london_session": {"open": 0.0, "high": 0.0, "low": 0.0, "current": 0.0, "swept_high": False, "swept_low": False},
        "verdict": "ANALYZING",
        "verdict_msg": "Analyzing session bars...",
        "box_pct_position": 50.0
    }

    if is_mt5_ok:
        rates_xau = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M15, 0, 100)
        if rates_xau is not None and len(rates_xau) >= 20:
            df_xau = pd.DataFrame(rates_xau)
            df_xau["time"] = pd.to_datetime(df_xau["time"], unit="s")
            df_xau["dt_utc"] = df_xau["time"] - pd.Timedelta(hours=broker_offset)

            # Filter today's bars in UTC
            asian_bars = df_xau[(df_xau["dt_utc"].dt.date == today) & (df_xau["dt_utc"].dt.hour < 7)]
            london_bars = df_xau[(df_xau["dt_utc"].dt.date == today) & (df_xau["dt_utc"].dt.hour >= 7)]

            if len(asian_bars) >= 4:
                asian_high = float(asian_bars["high"].max())
                asian_low = float(asian_bars["low"].min())
                asian_open = float(asian_bars["open"].iloc[0])
                asian_close = float(asian_bars["close"].iloc[-1])
                asian_dir = "BULLISH" if asian_close >= asian_open else "BEARISH"
                asian_range = round(asian_high - asian_low, 2)

                today_analysis["asian_box"] = {
                    "high": round(asian_high, 2),
                    "low": round(asian_low, 2),
                    "range": asian_range,
                    "direction": asian_dir
                }

                current_xau = float(df_xau["close"].iloc[-1])
                london_high = float(london_bars["high"].max()) if len(london_bars) > 0 else current_xau
                london_low = float(london_bars["low"].min()) if len(london_bars) > 0 else current_xau
                london_open = float(london_bars["open"].iloc[0]) if len(london_bars) > 0 else current_xau

                swept_high = bool(london_high > asian_high)
                swept_low = bool(london_low < asian_low)

                # Gauge position in Asian Box (0% = at low, 100% = at high)
                if asian_range > 0:
                    pct_pos = max(0.0, min(100.0, ((current_xau - asian_low) / asian_range) * 100.0))
                else:
                    pct_pos = 50.0

                dist_to_high = round(asian_high - current_xau, 2)
                dist_to_low = round(current_xau - asian_low, 2)

                today_analysis["london_session"] = {
                    "open": round(london_open, 2),
                    "high": round(london_high, 2),
                    "low": round(london_low, 2),
                    "current": round(current_xau, 2),
                    "swept_high": swept_high,
                    "swept_low": swept_low,
                    "dist_to_high": dist_to_high,
                    "dist_to_low": dist_to_low
                }
                today_analysis["box_pct_position"] = round(pct_pos, 1)

                # Diagnostic Verdict
                if not swept_high and not swept_low:
                    today_analysis["verdict"] = "INSIDE_CONSOLIDATION"
                    today_analysis["verdict_msg"] = (
                        f"Gold (${current_xau:.2f}) is hovering inside the Asian range (${asian_low:.2f} - ${asian_high:.2f}). "
                        f"No liquidity sweep triggered yet (Needs +${dist_to_high:.2f} to breach High, or -${dist_to_low:.2f} to breach Low). "
                        "Patiently holding to avoid 50/50 chop."
                    )
                elif swept_high and current_xau < asian_high:
                    today_analysis["verdict"] = "HIGH_SWEEP_REJECTION"
                    today_analysis["verdict_msg"] = f"London swept Asian High (${asian_high:.2f}) and rejected back down. Institutional sellers trapping breakout longs!"
                elif swept_low and current_xau > asian_low:
                    today_analysis["verdict"] = "LOW_SWEEP_RECLAIM"
                    today_analysis["verdict_msg"] = f"London swept Asian Low (${asian_low:.2f}) and reclaimed back up. Institutional buyers absorbing sell stops!"
                else:
                    today_analysis["verdict"] = "RANGE_EXPANSION"
                    today_analysis["verdict_msg"] = "Strong directional breakout underway past Asian boundaries."

    # 3. Calculate Upcoming Strategy Possibilities & Countdowns
    curr_hour = now_utc.hour + now_utc.minute / 60.0
    
    # Session windows in UTC
    london_open_active = (7.0 <= curr_hour < 9.0)
    overlap_active = (13.0 <= curr_hour < 15.5)
    fade_active = (13.25 <= curr_hour < 14.5)
    
    # Seconds to Overlap Session (13:00 UTC)
    target_overlap = now_utc.replace(hour=13, minute=0, second=0, microsecond=0)
    if now_utc > target_overlap:
        secs_to_overlap = 0
    else:
        secs_to_overlap = int((target_overlap - now_utc).total_seconds())

    # Seconds to London Open End (09:00 UTC)
    target_london_end = now_utc.replace(hour=9, minute=0, second=0, microsecond=0)
    secs_to_london_end = max(0, int((target_london_end - now_utc).total_seconds()))

    scenarios = [
        {
            "id": "setup_1_high_sweep",
            "name": "London Liquidity Sweep — Asian High Reversal",
            "session": "London Open (07:00 – 09:00 UTC | 12:30 – 14:30 IST)",
            "edge": "73.0% Win Rate",
            "target": "XAUUSD (SHORT / SELL)",
            "trigger_level": f"${today_analysis['asian_box']['high']:.2f}",
            "profit_target": f"${today_analysis['asian_box']['low']:.2f}",
            "condition": "Price pushes above Asian High, exhausts volume, and rejects back inside the box.",
            "status": "ACTIVE_WATCH" if london_open_active else "EXPIRED_FOR_TODAY" if curr_hour >= 9.0 else "UPCOMING",
            "status_badge": "Active Watch" if london_open_active else "Window Passed" if curr_hour >= 9.0 else "Upcoming",
            "time_remaining_mins": int(secs_to_london_end / 60) if london_open_active else 0
        },
        {
            "id": "setup_1_low_sweep",
            "name": "London Liquidity Sweep — Asian Low Reclaim",
            "session": "London Open (07:00 – 09:00 UTC | 12:30 – 14:30 IST)",
            "edge": "80.0% Win Rate",
            "target": "XAUUSD (LONG / BUY)",
            "trigger_level": f"${today_analysis['asian_box']['low']:.2f}",
            "profit_target": f"${today_analysis['asian_box']['high']:.2f}",
            "condition": "Price dips below Asian Low, triggers sell stops, and institutional buyers reclaim the level.",
            "status": "ACTIVE_WATCH" if london_open_active else "EXPIRED_FOR_TODAY" if curr_hour >= 9.0 else "UPCOMING",
            "status_badge": "Active Watch" if london_open_active else "Window Passed" if curr_hour >= 9.0 else "Upcoming",
            "time_remaining_mins": int(secs_to_london_end / 60) if london_open_active else 0
        },
        {
            "id": "setup_2_overlap_surge",
            "name": "London / NY Overlap Dual-Metal Momentum Surge",
            "session": "Overlap Peak (13:00 – 15:30 UTC | 18:30 – 21:00 IST)",
            "edge": "81.8% Co-Movement | 0.83 Correlation",
            "target": "XAUUSD & XAGUSD (Simultaneous Dual Entry)",
            "trigger_level": "Synchronized M15 directional expansion + EMA9 > EMA21",
            "profit_target": "Session extension with 2.12x Silver volatility multiplier",
            "condition": "Gold and Silver both confirm breakout direction during the day's highest liquidity overlap.",
            "status": "ACTIVE_NOW" if overlap_active else "COUNTDOWN",
            "status_badge": "Firing Now" if overlap_active else f"Starts in {int(secs_to_overlap/3600)}h {int((secs_to_overlap%3600)/60)}m",
            "countdown_seconds": secs_to_overlap
        },
        {
            "id": "setup_3_double_green_fade",
            "name": "Double-Green Morning Long Squeeze (Fade NY Open)",
            "session": "NY Open (13:15 – 14:30 UTC | 18:45 – 20:00 IST)",
            "edge": "58.8% Mean-Reversion Dump Edge",
            "target": "XAGUSD (SHORT / SELL)",
            "trigger_level": "Asian = UP and London = UP and Gold RSI > 68",
            "profit_target": "Morning liquidity flush back to London VWAP",
            "condition": "Fades extreme morning greed when NY opens to liquidate over-leveraged morning buyers.",
            "status": "PENDING_CHECK" if fade_active else "CONDITIONAL_UPCOMING",
            "status_badge": "Conditional Setup",
            "countdown_seconds": max(0, secs_to_overlap + 900)
        }
    ]

    # 4. Fetch recent Signals & Ledger from MongoDB
    recent_signals = []
    recent_ledger = []
    if db:
        try:
            sigs = list(db.db.signals.find().sort("timestamp", -1).limit(8))
            for s in sigs:
                recent_signals.append({
                    "time": s["timestamp"].strftime("%H:%M:%S") if isinstance(s["timestamp"], datetime) else str(s["timestamp"]),
                    "symbol": s.get("symbol", "XAUUSD"),
                    "signal": s.get("signal", "HOLD"),
                    "price": s.get("price", 0.0),
                    "reason": s.get("reason", ""),
                    "executed": s.get("executed", False)
                })

            leds = list(db.db.ledger.find().sort("timestamp", -1).limit(8))
            for l in leds:
                recent_ledger.append({
                    "time": l["timestamp"].strftime("%H:%M:%S") if isinstance(l["timestamp"], datetime) else str(l["timestamp"]),
                    "event_type": l.get("event_type", ""),
                    "symbol": l.get("symbol", "N/A"),
                    "amount": l.get("amount", 0.0),
                    "balance_after": l.get("balance_after", 0.0),
                    "notes": l.get("notes", "")
                })
        except Exception as e:
            logger.warning(f"Error reading Mongo collections: {e}")

    return {
        "timestamp_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "timestamp_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S (Local)"),
        "broker_offset_hours": broker_offset,
        "broker_time": (now_utc + timedelta(hours=broker_offset)).strftime("%H:%M:%S (UTC+3)"),
        "account": account_data,
        "open_positions": positions_list,
        "prices": prices,
        "today_analysis": today_analysis,
        "scenarios": scenarios,
        "recent_signals": recent_signals,
        "recent_ledger": recent_ledger
    }

# Complete self-contained HTML Dashboard
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Live Market Dashboard & Strategy Roadmap</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: rgba(18, 24, 38, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --accent-gold: #fbbf24;
      --accent-silver: #94a3b8;
      --accent-blue: #38bdf8;
      --accent-green: #10b981;
      --accent-red: #f43f5e;
      --accent-purple: #a855f7;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --font-main: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.12) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(251, 191, 36, 0.1) 0px, transparent 50%),
        radial-gradient(at 50% 100%, rgba(168, 85, 247, 0.08) 0px, transparent 50%);
      color: var(--text);
      font-family: var(--font-main);
      min-height: 100vh;
      padding: 24px;
      line-height: 1.5;
    }

    .container { max-width: 1380px; margin: 0 auto; }

    /* Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 16px;
    }
    .logo-group h1 {
      font-size: 24px;
      font-weight: 800;
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .logo-badge {
      background: linear-gradient(135deg, #fbbf24, #f59e0b);
      color: #000;
      font-size: 11px;
      font-weight: 800;
      padding: 3px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .header-sub {
      color: var(--text-muted);
      font-size: 13px;
      margin-top: 4px;
    }
    .header-status {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .status-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
      color: var(--accent-green);
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      background-color: var(--accent-green);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--accent-green);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    /* Grid Layouts */
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
    @media(max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

    /* Card styling */
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 20px;
      backdrop-filter: blur(12px);
      transition: border-color 0.2s ease;
    }
    .card:hover { border-color: rgba(255, 255, 255, 0.16); }

    .card-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .card-val {
      font-size: 26px;
      font-weight: 700;
      font-family: var(--font-mono);
      letter-spacing: -0.5px;
    }
    .card-foot {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 6px;
      display: flex;
      gap: 12px;
    }

    /* Live Session Clocks Bar */
    .clocks-bar {
      display: flex;
      gap: 12px;
      overflow-x: auto;
      padding-bottom: 8px;
      margin-bottom: 24px;
    }
    .clock-chip {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 10px 16px;
      min-width: 170px;
      backdrop-filter: blur(8px);
    }
    .clock-chip-name { font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 600; }
    .clock-chip-time { font-size: 15px; font-family: var(--font-mono); font-weight: 700; margin-top: 3px; }
    .clock-chip-state { font-size: 11px; margin-top: 2px; font-weight: 600; }

    /* Visual Asian Box Gauge */
    .box-visualizer {
      margin-top: 18px;
      padding: 18px;
      background: rgba(0, 0, 0, 0.3);
      border-radius: 12px;
      border: 1px solid var(--card-border);
    }
    .box-levels-header {
      display: flex;
      justify-content: space-between;
      font-size: 13px;
      font-family: var(--font-mono);
      font-weight: 600;
      margin-bottom: 8px;
    }
    .gauge-track {
      height: 12px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 8px;
      position: relative;
      overflow: visible;
      margin: 14px 0;
    }
    .gauge-fill {
      height: 100%;
      background: linear-gradient(90deg, #38bdf8, #fbbf24);
      border-radius: 8px;
      transition: width 0.4s ease;
    }
    .gauge-pin {
      position: absolute;
      top: -8px;
      width: 4px;
      height: 28px;
      background: #fff;
      box-shadow: 0 0 12px #fbbf24;
      border-radius: 2px;
      transform: translateX(-50%);
      transition: left 0.4s ease;
    }
    .pin-label {
      position: absolute;
      top: -24px;
      left: 50%;
      transform: translateX(-50%);
      background: #fbbf24;
      color: #000;
      font-size: 10px;
      font-weight: 800;
      padding: 1px 6px;
      border-radius: 4px;
      white-space: nowrap;
      font-family: var(--font-mono);
    }
    .box-verdict-banner {
      margin-top: 14px;
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 500;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .verdict-inside {
      background: rgba(56, 189, 248, 0.1);
      border: 1px solid rgba(56, 189, 248, 0.3);
      color: #7dd3fc;
    }
    .verdict-sweep {
      background: rgba(251, 191, 36, 0.15);
      border: 1px solid rgba(251, 191, 36, 0.4);
      color: #fde047;
    }

    /* Upcoming Scenarios Cards */
    .scenario-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 20px;
      margin-bottom: 14px;
      transition: all 0.2s ease;
      position: relative;
      overflow: hidden;
    }
    .scenario-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; width: 4px; height: 100%;
    }
    .scenario-card.active-watch::before { background: var(--accent-gold); }
    .scenario-card.countdown::before { background: var(--accent-blue); }
    .scenario-card.conditional::before { background: var(--accent-purple); }

    .scenario-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 10px;
      flex-wrap: wrap;
      gap: 8px;
    }
    .scenario-title {
      font-size: 16px;
      font-weight: 700;
      color: #fff;
    }
    .badge {
      font-size: 11px;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .badge-edge {
      background: rgba(16, 185, 129, 0.15);
      color: var(--accent-green);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-status {
      background: rgba(56, 189, 248, 0.15);
      color: var(--accent-blue);
      border: 1px solid rgba(56, 189, 248, 0.3);
    }

    .scenario-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin: 12px 0;
      background: rgba(0, 0, 0, 0.25);
      padding: 12px;
      border-radius: 8px;
    }
    .scenario-prop-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }
    .scenario-prop-val { font-size: 13px; font-weight: 600; font-family: var(--font-mono); margin-top: 2px; }

    /* Tables */
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th {
      text-align: left;
      padding: 10px 12px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
    }
    td {
      padding: 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: var(--font-mono);
    }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    .tag-buy { color: var(--accent-green); font-weight: 700; }
    .tag-sell { color: var(--accent-red); font-weight: 700; }
    .tag-hold { color: var(--text-muted); }

    /* Footer */
    footer {
      margin-top: 40px;
      text-align: center;
      font-size: 12px;
      color: var(--text-muted);
      border-top: 1px solid var(--card-border);
      padding-top: 20px;
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <header>
      <div class="logo-group">
        <h1>
          MetaTrader 5 Algorithmic Dashboard
          <span class="logo-badge">Live Strategy Engine</span>
        </h1>
        <div class="header-sub">
          Continuous Intraday Session Correlation &amp; Inter-Market Execution (XAUUSD + XAGUSD)
        </div>
      </div>
      <div class="header-status">
        <div class="status-pill">
          <div class="pulse-dot"></div>
          <span id="mt5-status-text">Connected to MT5</span>
        </div>
        <button onclick="refreshData()" style="background: rgba(255,255,255,0.08); border: 1px solid var(--card-border); color: #fff; padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600;">
          ↻ Refresh
        </button>
      </div>
    </header>

    <!-- Global Session Clocks -->
    <div class="clocks-bar">
      <div class="clock-chip">
        <div class="clock-chip-name">UTC Time</div>
        <div class="clock-chip-time" id="clock-utc">--:--:--</div>
        <div class="clock-chip-state" style="color: var(--text-muted);">Standard Baseline</div>
      </div>
      <div class="clock-chip">
        <div class="clock-chip-name">Vantage Broker Time</div>
        <div class="clock-chip-time" id="clock-broker">--:--:--</div>
        <div class="clock-chip-state" style="color: var(--accent-blue);">Server Clock (UTC+3)</div>
      </div>
      <div class="clock-chip">
        <div class="clock-chip-name">Asian Session</div>
        <div class="clock-chip-time">00:00 – 07:00 UTC</div>
        <div class="clock-chip-state" style="color: var(--text-muted);">● Closed (Box Formed)</div>
      </div>
      <div class="clock-chip" style="border-color: rgba(251, 191, 36, 0.4);">
        <div class="clock-chip-name" style="color: var(--accent-gold);">London Session</div>
        <div class="clock-chip-time">07:00 – 13:00 UTC</div>
        <div class="clock-chip-state" style="color: var(--accent-gold);">● Active (Liquidity Phase)</div>
      </div>
      <div class="clock-chip" style="border-color: rgba(56, 189, 248, 0.4);">
        <div class="clock-chip-name" style="color: var(--accent-blue);">London/NY Overlap</div>
        <div class="clock-chip-time">13:00 – 15:30 UTC</div>
        <div class="clock-chip-state" id="overlap-countdown-chip" style="color: var(--accent-blue);">⏳ Countdown</div>
      </div>
    </div>

    <!-- Top Account & Price Cards -->
    <div class="grid-4">
      <div class="card">
        <div class="card-title">Gold Spot (XAUUSD) <span style="color: var(--accent-gold);">● LIVE</span></div>
        <div class="card-val" id="xau-price" style="color: var(--accent-gold);">$0.00</div>
        <div class="card-foot">
          <span>Bid: <b id="xau-bid">0.00</b></span>
          <span>Ask: <b id="xau-ask">0.00</b></span>
          <span>Spread: <b id="xau-spread">0.0</b> pts</span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Silver Spot (XAGUSD) <span style="color: var(--accent-silver);">● LIVE</span></div>
        <div class="card-val" id="xag-price" style="color: var(--accent-silver);">$0.00</div>
        <div class="card-foot">
          <span>Bid: <b id="xag-bid">0.00</b></span>
          <span>Ask: <b id="xag-ask">0.00</b></span>
          <span>Multiplier: <b>2.12x</b></span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Account Equity</div>
        <div class="card-val" id="account-equity" style="color: var(--accent-green);">$0.00</div>
        <div class="card-foot">
          <span>Balance: <b id="account-balance">$0.00</b></span>
          <span>Floating: <b id="account-profit">$0.00</b></span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Bot Daily Target</div>
        <div class="card-val" style="color: #fff;">1 – 2 Trades</div>
        <div class="card-foot">
          <span>High-Conviction Only</span>
          <span>Max Today: <b>3</b></span>
        </div>
      </div>
    </div>

    <!-- Today's Intraday Analysis & Asian Box Visualizer -->
    <div class="card" style="margin-bottom: 24px;">
      <div class="card-title">
        <span>Today's Intraday Session Levels &amp; Asian Range Box</span>
        <span id="asian-dir-badge" class="badge" style="background: rgba(255,255,255,0.1);">Asian Direction: --</span>
      </div>

      <div class="grid-4" style="margin-bottom: 0;">
        <div>
          <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Asian Box High</div>
          <div style="font-size: 20px; font-weight: 700; font-family: var(--font-mono); color: var(--accent-red);" id="val-asian-high">$0.00</div>
        </div>
        <div>
          <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Asian Box Low</div>
          <div style="font-size: 20px; font-weight: 700; font-family: var(--font-mono); color: var(--accent-green);" id="val-asian-low">$0.00</div>
        </div>
        <div>
          <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">Asian Range Size</div>
          <div style="font-size: 20px; font-weight: 700; font-family: var(--font-mono);" id="val-asian-range">$0.00</div>
        </div>
        <div>
          <div style="font-size: 11px; color: var(--text-muted); text-transform: uppercase;">London High / Low So Far</div>
          <div style="font-size: 15px; font-weight: 700; font-family: var(--font-mono);" id="val-london-hl">$0.00 / $0.00</div>
        </div>
      </div>

      <!-- Real-Time Asian Box Visualizer Gauge -->
      <div class="box-visualizer">
        <div class="box-levels-header">
          <span style="color: var(--accent-green);">▼ Asian Low: <span id="gauge-asian-low">$0.00</span></span>
          <span style="color: var(--text-muted);">Consolidation Box Range</span>
          <span style="color: var(--accent-red);">▲ Asian High: <span id="gauge-asian-high">$0.00</span></span>
        </div>
        <div class="gauge-track">
          <div class="gauge-fill" id="gauge-fill" style="width: 50%;"></div>
          <div class="gauge-pin" id="gauge-pin" style="left: 50%;">
            <div class="pin-label" id="gauge-pin-label">Gold: $0.00</div>
          </div>
        </div>
        <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); margin-top: 6px;">
          <span>Distance to Low: <b id="dist-low" style="color: #fff;">$0.00</b></span>
          <span id="gauge-box-pos">Position: 50%</span>
          <span>Distance to High: <b id="dist-high" style="color: #fff;">$0.00</b></span>
        </div>
      </div>

      <!-- Diagnostic Verdict Banner -->
      <div class="box-verdict-banner verdict-inside" id="verdict-banner">
        <span>⚡</span>
        <span id="verdict-text">Loading session verdict...</span>
      </div>
    </div>

    <!-- Upcoming Possibilities & Scenario Roadmap -->
    <div style="margin-bottom: 24px;">
      <h2 style="font-size: 18px; font-weight: 800; margin-bottom: 16px; display: flex; align-items: center; gap: 8px;">
        🎯 Upcoming Strategy Scenarios &amp; High-Probability Possibilities
      </h2>
      <div id="scenarios-container">
        <!-- Rendered dynamically via JS -->
      </div>
    </div>

    <!-- Open Positions & Signals Log -->
    <div class="grid-2">
      <div class="card">
        <div class="card-title">Live Open Positions in MT5</div>
        <div style="overflow-x: auto;">
          <table id="positions-table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Symbol</th>
                <th>Type</th>
                <th>Lots</th>
                <th>Open Price</th>
                <th>PnL</th>
              </tr>
            </thead>
            <tbody id="positions-tbody">
              <tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No open positions currently</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-title">Live Strategy Decisions &amp; Signals</div>
        <div style="overflow-x: auto;">
          <table>
            <thead>
              <tr>
                <th>Time (UTC)</th>
                <th>Signal</th>
                <th>Price</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody id="signals-tbody">
              <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Awaiting signals...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Immutable Accounting Ledger -->
    <div class="card" style="margin-bottom: 24px;">
      <div class="card-title">Immutable Accounting Ledger (MongoDB Audit Log)</div>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Event</th>
              <th>Symbol</th>
              <th>Settled Amount</th>
              <th>Balance After</th>
              <th>Audit Notes</th>
            </tr>
          </thead>
          <tbody id="ledger-tbody">
            <tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No recent ledger entries</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer>
      MetaTrader 5 Automated Trading Bot • MongoDB Local Database • Quantitative 12-Month Empirical Session Engine
    </footer>
  </div>

  <script>
    async function refreshData() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderDashboard(data);
      } catch (err) {
        console.error("Dashboard poll failed:", err);
      }
    }

    function renderDashboard(data) {
      // Clocks
      if (data.timestamp_utc) {
        document.getElementById('clock-utc').innerText = data.timestamp_utc.split(' ')[1];
      }
      if (data.broker_time) {
        document.getElementById('clock-broker').innerText = data.broker_time;
      }

      // Account
      if (data.account && data.account.balance !== undefined) {
        document.getElementById('account-balance').innerText = '$' + data.account.balance.toFixed(2);
        document.getElementById('account-equity').innerText = '$' + data.account.equity.toFixed(2);
        document.getElementById('account-profit').innerText = (data.account.profit >= 0 ? '+$' : '-$') + Math.abs(data.account.profit).toFixed(2);
        document.getElementById('account-profit').style.color = data.account.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
      }

      // Live prices
      if (data.prices && data.prices.XAUUSD) {
        document.getElementById('xau-price').innerText = '$' + data.prices.XAUUSD.ask.toFixed(2);
        document.getElementById('xau-bid').innerText = data.prices.XAUUSD.bid.toFixed(2);
        document.getElementById('xau-ask').innerText = data.prices.XAUUSD.ask.toFixed(2);
        document.getElementById('xau-spread').innerText = data.prices.XAUUSD.spread;
      }
      if (data.prices && data.prices.XAGUSD) {
        document.getElementById('xag-price').innerText = '$' + data.prices.XAGUSD.ask.toFixed(3);
        document.getElementById('xag-bid').innerText = data.prices.XAGUSD.bid.toFixed(3);
        document.getElementById('xag-ask').innerText = data.prices.XAGUSD.ask.toFixed(3);
      }

      // Today's Analysis
      const ana = data.today_analysis;
      if (ana && ana.asian_box) {
        document.getElementById('val-asian-high').innerText = '$' + ana.asian_box.high.toFixed(2);
        document.getElementById('val-asian-low').innerText = '$' + ana.asian_box.low.toFixed(2);
        document.getElementById('val-asian-range').innerText = '$' + ana.asian_box.range.toFixed(2);
        document.getElementById('gauge-asian-high').innerText = '$' + ana.asian_box.high.toFixed(2);
        document.getElementById('gauge-asian-low').innerText = '$' + ana.asian_box.low.toFixed(2);
        document.getElementById('asian-dir-badge').innerText = 'Asian Direction: ' + ana.asian_box.direction;
        document.getElementById('asian-dir-badge').style.color = ana.asian_box.direction === 'BULLISH' ? 'var(--accent-green)' : 'var(--accent-red)';

        if (ana.london_session) {
          document.getElementById('val-london-hl').innerText = '$' + ana.london_session.high.toFixed(2) + ' / $' + ana.london_session.low.toFixed(2);
          document.getElementById('dist-high').innerText = '+$' + (ana.london_session.dist_to_high || 0).toFixed(2);
          document.getElementById('dist-low').innerText = '-$' + (ana.london_session.dist_to_low || 0).toFixed(2);
        }

        // Gauge Needle
        const pct = ana.box_pct_position || 50;
        document.getElementById('gauge-fill').style.width = pct + '%';
        document.getElementById('gauge-pin').style.left = pct + '%';
        document.getElementById('gauge-box-pos').innerText = 'Position: ' + pct + '%';
        if (data.prices && data.prices.XAUUSD) {
          document.getElementById('gauge-pin-label').innerText = 'Gold: $' + data.prices.XAUUSD.ask.toFixed(2);
        }

        // Verdict
        document.getElementById('verdict-text').innerText = ana.verdict_msg || 'Analyzing...';
        const banner = document.getElementById('verdict-banner');
        if (ana.verdict === 'INSIDE_CONSOLIDATION') {
          banner.className = 'box-verdict-banner verdict-inside';
        } else {
          banner.className = 'box-verdict-banner verdict-sweep';
        }
      }

      // Scenarios Roadmap
      if (data.scenarios) {
        const scContainer = document.getElementById('scenarios-container');
        scContainer.innerHTML = '';
        data.scenarios.forEach(sc => {
          const card = document.createElement('div');
          const isWatch = sc.status === 'ACTIVE_WATCH';
          const isFiring = sc.status === 'ACTIVE_NOW';
          card.className = 'scenario-card ' + (isWatch || isFiring ? 'active-watch' : 'countdown');

          card.innerHTML = `
            <div class="scenario-header">
              <div>
                <div class="scenario-title">${sc.name}</div>
                <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">${sc.session}</div>
              </div>
              <div style="display: flex; gap: 8px;">
                <span class="badge badge-edge">${sc.edge}</span>
                <span class="badge badge-status">${sc.status_badge}</span>
              </div>
            </div>
            <div class="scenario-grid">
              <div>
                <div class="scenario-prop-label">Execution Target</div>
                <div class="scenario-prop-val" style="color: var(--accent-gold);">${sc.target}</div>
              </div>
              <div>
                <div class="scenario-prop-label">Trigger Price / Level</div>
                <div class="scenario-prop-val">${sc.trigger_level}</div>
              </div>
              <div>
                <div class="scenario-prop-label">Profit Target Level</div>
                <div class="scenario-prop-val">${sc.profit_target}</div>
              </div>
            </div>
            <div style="font-size: 13px; color: #cbd5e1;">
              <b>Trigger Condition:</b> ${sc.condition}
            </div>
          `;
          scContainer.appendChild(card);
        });
      }

      // Open Positions Table
      const posTbody = document.getElementById('positions-tbody');
      if (data.open_positions && data.open_positions.length > 0) {
        posTbody.innerHTML = '';
        data.open_positions.forEach(p => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>#${p.ticket}</td>
            <td><b>${p.symbol}</b></td>
            <td class="${p.type === 'BUY' ? 'tag-buy' : 'tag-sell'}">${p.type}</td>
            <td>${p.volume}</td>
            <td>$${p.open_price}</td>
            <td style="color: ${p.profit >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">$${p.profit.toFixed(2)}</td>
          `;
          posTbody.appendChild(tr);
        });
      } else {
        posTbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No open positions currently</td></tr>';
      }

      // Recent Signals Table
      const sigTbody = document.getElementById('signals-tbody');
      if (data.recent_signals && data.recent_signals.length > 0) {
        sigTbody.innerHTML = '';
        data.recent_signals.forEach(s => {
          const tr = document.createElement('tr');
          const sigClass = s.signal === 'BUY' ? 'tag-buy' : s.signal === 'SELL' ? 'tag-sell' : 'tag-hold';
          tr.innerHTML = `
            <td>${s.time}</td>
            <td class="${sigClass}">${s.signal}</td>
            <td>$${s.price.toFixed(2)}</td>
            <td style="font-size: 12px; color: #cbd5e1;">${s.reason}</td>
          `;
          sigTbody.appendChild(tr);
        });
      }

      // Ledger Table
      const ledTbody = document.getElementById('ledger-tbody');
      if (data.recent_ledger && data.recent_ledger.length > 0) {
        ledTbody.innerHTML = '';
        data.recent_ledger.forEach(l => {
          const tr = document.createElement('tr');
          const amtColor = l.amount >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
          tr.innerHTML = `
            <td>${l.time}</td>
            <td><b>${l.event_type}</b></td>
            <td>${l.symbol}</td>
            <td style="color: ${amtColor}">${l.amount >= 0 ? '+$' : '-$'}${Math.abs(l.amount).toFixed(2)}</td>
            <td>$${l.balance_after.toFixed(2)}</td>
            <td style="font-size: 12px; color: #94a3b8;">${l.notes}</td>
          `;
          ledTbody.appendChild(tr);
        });
      }
    }

    // Auto-refresh every 3 seconds
    setInterval(refreshData, 3000);
    window.onload = refreshData;
  </script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = get_live_dashboard_data()
                data = sanitize_mongo_value(data)
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                logger.error(f"Error serving /api/status: {e}", exc_info=True)
                err_body = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(err_body)
        else:
            # Serve main HTML dashboard
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress routine GET logging to keep console clean
        return

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run_server(port: int = 5000):
    server = ThreadedHTTPServer(("0.0.0.0", port), DashboardHandler)
    logger.info("=" * 65)
    logger.info(f"🚀 LIVE STRATEGY DASHBOARD RUNNING ON: http://localhost:{port}")
    logger.info("=" * 65)
    logger.info("Open this URL in your browser to view:")
    logger.info("  1. Today's live Asian Box High/Low levels & Gold position")
    logger.info("  2. Upcoming high-probability strategy possibilities & countdowns")
    logger.info("  3. Live MT5 equity, open positions, signals, & ledger settlements")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping dashboard server...")
        server.server_close()

if __name__ == "__main__":
    run_server(5000)
