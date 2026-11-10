"""
Generates comprehensive canonical sample outputs for Phase 2C:
1. Bullish Qualified Setup (Score >= 75, All gates passed, R:R >= 1.5, liquid option)
2. Bearish Qualified Setup (Score >= 75, All gates passed, R:R >= 1.5, liquid option)
3. Conflicted Market Setup (Both Bullish and Bearish scores high -> NO TRADE)
4. Squeeze Compression Gate Trigger (Carter's Squeeze ON -> NO TRADE)
5. Illiquid Option Gate Trigger (Spread > 4% -> NO TRADE)
"""
import sys
import os
import json
import math
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.data.candle_store import MultiTimeframeCandleStore, Candle, Timeframe
from app.core.setup_scoring import SetupScoringService

def create_setup_candles(scenario="BULLISH", bars=100, base_p=24200.0):
    candles_by_tf = {}
    ist = timezone(timedelta(hours=5, minutes=30))
    base_time = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist)
    timeframes = [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]
    tf_seconds = {Timeframe.S15: 15, Timeframe.M1: 60, Timeframe.M3: 180, Timeframe.M5: 300, Timeframe.M15: 900}

    for tf in timeframes:
        delta_sec = tf_seconds[tf]
        c_list = []
        for i in range(bars):
            s_dt = base_time + timedelta(seconds=i * delta_sec)
            e_dt = s_dt + timedelta(seconds=delta_sec)

            if scenario == "SQUEEZE":
                p = base_p + math.sin(i / 5.0) * 1.5
                c = Candle(
                    symbol="NIFTY 50", timeframe=tf, start_time=s_dt, end_time=e_dt,
                    open_price=round(p, 2), high_price=round(p + 1.0, 2),
                    low_price=round(p - 1.0, 2), close_price=round(p + 0.2, 2),
                    volume=0, is_closed=True, candle_status="HISTORICAL"
                )
            elif scenario == "BULLISH_QUALIFIED":
                # Upward trend with pullback to HL and breakout toward higher target
                trend_slope = (i * 0.8)
                wave = 20.0 * math.sin(i / 6.0)
                p = base_p + trend_slope + wave
                # Ensure the last bar is in clean expansion above recent swing low
                c = Candle(
                    symbol="NIFTY 50", timeframe=tf, start_time=s_dt, end_time=e_dt,
                    open_price=round(p, 2), high_price=round(p + 10.0, 2),
                    low_price=round(p - 6.0, 2), close_price=round(p + 4.0, 2),
                    volume=0, is_closed=True, candle_status="HISTORICAL"
                )
            elif scenario == "BEARISH_QUALIFIED":
                trend_slope = -(i * 0.8)
                wave = 20.0 * math.sin(i / 6.0)
                p = base_p + trend_slope + wave
                c = Candle(
                    symbol="NIFTY 50", timeframe=tf, start_time=s_dt, end_time=e_dt,
                    open_price=round(p, 2), high_price=round(p + 6.0, 2),
                    low_price=round(p - 10.0, 2), close_price=round(p - 4.0, 2),
                    volume=0, is_closed=True, candle_status="HISTORICAL"
                )
            else:
                p = base_p + (i * 0.5)
                c = Candle(
                    symbol="NIFTY 50", timeframe=tf, start_time=s_dt, end_time=e_dt,
                    open_price=round(p, 2), high_price=round(p + 5.0, 2),
                    low_price=round(p - 5.0, 2), close_price=round(p + 1.0, 2),
                    volume=0, is_closed=True, candle_status="HISTORICAL"
                )
            c_list.append(c)
        candles_by_tf[tf] = c_list
    return candles_by_tf

def run_generate_samples():
    store = MultiTimeframeCandleStore()
    scoring_service = SetupScoringService(store)
    symbol = "NIFTY 50"
    store._ensure_symbol_initialized(symbol)

    # 1. Bullish Setup
    store._closed_candles[symbol] = create_setup_candles(scenario="BULLISH_QUALIFIED", bars=120, base_p=24200.0)
    now_ist_str = datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat()
    liquid_call_quote = {
        "symbol": "NIFTY26NOV24300CE",
        "strike": 24300.0,
        "option_type": "CE",
        "ltp": 165.0, "bid": 164.5, "ask": 165.5,
        "oi": 180000, "volume": 75000,
        "expiry_date": "2026-11-23",
        "lot_size": 65,
        "instrument_token": "999123",
        "last_trade_time": now_ist_str,
        "status": "ACTIVE"
    }
    bullish_sample = scoring_service.evaluate_opportunity(symbol, option_quote=liquid_call_quote)

    # 2. Bearish Setup
    store._closed_candles[symbol] = create_setup_candles(scenario="BEARISH_QUALIFIED", bars=120, base_p=24500.0)
    liquid_put_quote = {
        "symbol": "NIFTY26NOV24300PE",
        "strike": 24300.0,
        "option_type": "PE",
        "ltp": 145.0, "bid": 144.5, "ask": 145.5,
        "oi": 160000, "volume": 62000,
        "expiry_date": "2026-11-23",
        "lot_size": 65,
        "instrument_token": "999124",
        "last_trade_time": now_ist_str,
        "status": "ACTIVE"
    }
    bearish_sample = scoring_service.evaluate_opportunity(symbol, option_quote=liquid_put_quote)

    # 3. Squeeze Compression Gate (NO TRADE)
    store._closed_candles[symbol] = create_setup_candles(scenario="SQUEEZE", bars=80, base_p=24300.0)
    squeeze_sample = scoring_service.evaluate_opportunity(symbol, option_quote=liquid_call_quote)

    # 4. Illiquid Option Gate (NO TRADE)
    store._closed_candles[symbol] = create_setup_candles(scenario="BULLISH_QUALIFIED", bars=120, base_p=24200.0)
    illiquid_quote = {
        "symbol": "NIFTY26NOV25000CE",
        "strike": 25000.0,
        "option_type": "CE",
        "ltp": 15.0, "bid": 12.0, "ask": 18.0,  # 40% spread
        "oi": 5000, "volume": 400,
        "expiry_date": "2026-11-23",
        "lot_size": 65,
        "instrument_token": "999999",
        "last_trade_time": now_ist_str,
        "status": "ACTIVE"
    }
    illiquid_sample = scoring_service.evaluate_opportunity(symbol, option_quote=illiquid_quote)

    samples = {
        "bullish_opportunity_evaluation": bullish_sample,
        "bearish_opportunity_evaluation": bearish_sample,
        "squeeze_compression_gate_no_trade": squeeze_sample,
        "illiquid_option_gate_no_trade": illiquid_sample
    }

    out_file = "scratch/sample_phase2c_setups.json"
    with open(out_file, "w") as f:
        json.dump(samples, f, indent=2)
    print(f"[OK] Phase 2C canonical samples generated and written to: {out_file}")

if __name__ == "__main__":
    run_generate_samples()
