"""
Phase 2B Market Structure, Regime & Divergence Engine - Benchmarking & Sample Generator
"""
import sys
import os
import time
import json
import math
from datetime import datetime, timezone, timedelta

# Ensure backend is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.data.candle_store import MultiTimeframeCandleStore, Candle
from app.core.indicators import TechnicalIndicatorService
from app.core.market_structure import MarketStructureService, DEFAULT_TIMEFRAME_K

def generate_realistic_nifty_store():
    store = MultiTimeframeCandleStore()
    ist = timezone(timedelta(hours=5, minutes=30))
    base_time = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist)
    timeframes = ["15s", "1m", "3m", "5m", "15m"]
    tf_seconds = {"15s": 15, "1m": 60, "3m": 180, "5m": 300, "15m": 900}

    sym = "NIFTY 50"
    store._ensure_symbol_initialized(sym)

    for tf in timeframes:
        delta_sec = tf_seconds[tf]
        base_price = 24200.0
        candle_list = []
        n_bars = 400

        # Simulate structured swings: Higher Highs, Higher Lows followed by consolidation
        curr_price = base_price
        for i in range(n_bars):
            s_dt = base_time + timedelta(seconds=i * delta_sec)
            e_dt = s_dt + timedelta(seconds=delta_sec)
            
            # Create wavy trend with clear fractal peaks and valleys
            swing_wave = 60.0 * math.sin(i / 12.0) + (i * 0.25)
            open_p = base_price + swing_wave
            high_p = open_p + 14.0 + (5.0 if (i % 24 == 12) else 0.0)
            low_p = open_p - 12.0 - (5.0 if (i % 24 == 0) else 0.0)
            close_p = open_p + 4.0 * math.cos(i / 6.0)

            c = Candle(
                symbol=sym,
                timeframe=tf,
                start_time=s_dt,
                end_time=e_dt,
                open_price=round(open_p, 2),
                high_price=round(high_p, 2),
                low_price=round(low_p, 2),
                close_price=round(close_p, 2),
                volume=0,  # Spot index
                is_closed=True,
                candle_status="HISTORICAL"
            )
            candle_list.append(c)

        store._closed_candles[sym][tf] = candle_list

        # Active candle
        s_dt = base_time + timedelta(seconds=n_bars * delta_sec)
        e_dt = s_dt + timedelta(seconds=delta_sec)
        active_c = Candle(
            symbol=sym,
            timeframe=tf,
            start_time=s_dt,
            end_time=e_dt,
            open_price=24310.0,
            high_price=24318.0,
            low_price=24305.0,
            close_price=24315.0,
            volume=0,
            is_closed=False,
            candle_status="DEVELOPING"
        )
        store._active_candle[sym][tf] = active_c

    return store

def run_benchmarks_and_generate_sample():
    store = generate_realistic_nifty_store()
    indicator_service = TechnicalIndicatorService(store)
    structure_service = MarketStructureService(store, indicator_service=indicator_service)

    symbol = "NIFTY 50"
    timeframes = ["15s", "1m", "3m", "5m", "15m"]

    # Pre-compute indicators for feeding into structure service
    print("\n" + "="*80)
    print("PHASE 2B MARKET STRUCTURE, REGIME & DIVERGENCE BENCHMARKS")
    print("="*80)

    # 1. Benchmark Single Timeframe Execution
    # Warmup
    _ = structure_service.analyze_market_structure(symbol, "5m")

    trials = 100
    t0 = time.perf_counter()
    for _ in range(trials):
        res_5m = structure_service.analyze_market_structure(symbol, "5m")
    elapsed_5m = (time.perf_counter() - t0) / trials * 1000.0
    print(f"Single Timeframe (5m, 400 bars) Latency: {elapsed_5m:.3f} ms / run (Target < 2.0 ms)")

    # 2. Benchmark Full Multi-Timeframe Suite (All 5 Timeframes)
    t0 = time.perf_counter()
    multi_suite = structure_service.analyze_multi_timeframe_suite(symbol, timeframes)
    elapsed_multi = (time.perf_counter() - t0) * 1000.0
    print(f"Full 5-Timeframe Suite (15s, 1m, 3m, 5m, 15m; 2,000+ bars) Latency: {elapsed_multi:.2f} ms (Target < 100 ms)")

    # 3. Timeframe Breakdown
    print("\nTimeframe-by-timeframe Breakdown:")
    for tf in timeframes:
        t_sub = time.perf_counter()
        _ = structure_service.analyze_market_structure(symbol, tf)
        dt_sub = (time.perf_counter() - t_sub) * 1000.0
        k_val = DEFAULT_TIMEFRAME_K.get(tf, 2)
        print(f"  - {tf:>3} (K={k_val}): {dt_sub:.3f} ms")

    # 4. Save Sample Output to JSON
    sample_file = "scratch/sample_nifty_market_structure.json"
    with open(sample_file, "w") as f:
        json.dump(multi_suite, f, indent=2)
    print(f"\n[OK] Sample Multi-Timeframe Market Structure JSON written to: {sample_file}")

    # Summary verification
    print("\nHardening Verification Summary:")
    for tf in timeframes:
        tf_data = multi_suite["timeframe_analyses"][tf]
        regime = tf_data["regime"]["regime"]
        struct = tf_data["structure"]["structure_state"]
        breaks = tf_data["structural_break"]["event_type"]
        b_lifecycle = tf_data["structural_break"]["lifecycle_state"]
        rsi_div = tf_data["divergences"]["rsi_divergence"]["divergence_type"]
        rsi_inv = tf_data["divergences"]["rsi_divergence"]["invalidation_status"]
        print(f"  [{tf:>3}] Regime: {regime:<22} | Struct: {struct:<10} | Break: {breaks:<12} ({b_lifecycle}) | RSI Div: {rsi_div} ({rsi_inv})")

if __name__ == "__main__":
    run_benchmarks_and_generate_sample()
