"""
TradeLens AI - Phase 2B Market Structure Profiling & Reproducible Benchmark
Measures Warmup, Iterations, Median, P95, Max, and Pipeline Stage Breakdown.
"""
import sys
import os
import time
import math
import cProfile
import pstats
import io
from datetime import datetime, timezone, timedelta

# Ensure backend is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.data.candle_store import MultiTimeframeCandleStore, Candle, Timeframe
from app.core.indicators import TechnicalIndicatorService
from app.core.market_structure import (
    MarketStructureService,
    identify_fractal_pivots,
    classify_market_structure,
    detect_structural_breaks,
    classify_market_regime,
    detect_divergence,
    DEFAULT_TIMEFRAME_K
)

def create_benchmark_store(bars_per_tf: int = 400) -> MultiTimeframeCandleStore:
    store = MultiTimeframeCandleStore()
    ist = timezone(timedelta(hours=5, minutes=30))
    base_time = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist)
    timeframes = [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]
    tf_seconds = {Timeframe.S15: 15, Timeframe.M1: 60, Timeframe.M3: 180, Timeframe.M5: 300, Timeframe.M15: 900}
    sym = "NIFTY 50"
    store._ensure_symbol_initialized(sym)

    for tf in timeframes:
        delta_sec = tf_seconds[tf]
        base_price = 24200.0
        candle_list = []
        for i in range(bars_per_tf):
            s_dt = base_time + timedelta(seconds=i * delta_sec)
            e_dt = s_dt + timedelta(seconds=delta_sec)
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
                volume=0,
                is_closed=True,
                candle_status="HISTORICAL"
            )
            candle_list.append(c)
        store._closed_candles[sym][tf] = candle_list

        # Active forming bar
        s_dt = base_time + timedelta(seconds=bars_per_tf * delta_sec)
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

def run_reproducible_benchmark():
    store = create_benchmark_store(bars_per_tf=400)
    ind_service = TechnicalIndicatorService(store)
    struct_service = MarketStructureService(store, indicator_service=ind_service)
    symbol = "NIFTY 50"
    timeframes = [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]

    print("=" * 80)
    print("PHASE 2B RIGOROUS BENCHMARK & REPRODUCIBILITY PROFILING")
    print("=" * 80)
    print(f"Dataset: 5 timeframes x 400 bars = 2,000 candles total (NIFTY 50)")

    # 1. Warm-up Phase
    warmup_runs = 10
    print(f"\n[1] Executing {warmup_runs} warm-up iterations (JIT / cache stabilization)...")
    for _ in range(warmup_runs):
        _ = struct_service.analyze_multi_timeframe_suite(symbol, timeframes)
    print("    Warm-up complete.")

    # 2. Measurement Phase (N = 50)
    iterations = 50
    durations_ms = []
    print(f"\n[2] Executing {iterations} timed iterations for Multi-Timeframe Suite...")
    for _ in range(iterations):
        t0 = time.perf_counter()
        _ = struct_service.analyze_multi_timeframe_suite(symbol, timeframes)
        t1 = time.perf_counter()
        durations_ms.append((t1 - t0) * 1000.0)

    durations_ms.sort()
    min_lat = durations_ms[0]
    max_lat = durations_ms[-1]
    mean_lat = sum(durations_ms) / len(durations_ms)
    median_lat = durations_ms[len(durations_ms) // 2]
    p95_idx = int(0.95 * len(durations_ms))
    p95_lat = durations_ms[p95_idx]

    print("\n--- Multi-Timeframe Suite Latency Distribution (5 Timeframes, 2,000 bars) ---")
    print(f"  Iterations: {iterations}")
    print(f"  Warm-up:    {warmup_runs}")
    print(f"  Min:        {min_lat:.3f} ms")
    print(f"  Mean:       {mean_lat:.3f} ms")
    print(f"  Median:     {median_lat:.3f} ms")
    print(f"  P95:        {p95_lat:.3f} ms")
    print(f"  Max:        {max_lat:.3f} ms")
    print(f"  Budget:     < 100.000 ms -> Status: {'PASS' if p95_lat < 100.0 else 'EXCEEDED'}")

    # 3. Stage-by-Stage Profiling Breakdown
    print("\n[3] Pipeline Stage-by-Stage Latency Breakdown (Single 5m Timeframe, 400 bars):")
    candles = store.get_closed_candles(symbol, "5m")
    closes = [float(c["close"]) for c in candles]

    # Stage A: Indicators
    t_start = time.perf_counter()
    for _ in range(50):
        ind_packet = ind_service.compute_all_indicators(symbol, "5m")
    dt_ind = (time.perf_counter() - t_start) / 50 * 1000.0

    # Stage B: Fractal Pivots
    t_start = time.perf_counter()
    for _ in range(50):
        pivots = identify_fractal_pivots(candles, k=3, symbol=symbol, timeframe="5m")
    dt_pivots = (time.perf_counter() - t_start) / 50 * 1000.0

    # Stage C: Market Structure
    t_start = time.perf_counter()
    for _ in range(50):
        struct = classify_market_structure(candles, k=3, symbol=symbol, timeframe="5m")
    dt_struct = (time.perf_counter() - t_start) / 50 * 1000.0

    # Stage D: Structural Breaks & Lifecycle
    t_start = time.perf_counter()
    for _ in range(50):
        breaks = detect_structural_breaks(candles, k=3, symbol=symbol, timeframe="5m")
    dt_breaks = (time.perf_counter() - t_start) / 50 * 1000.0

    # Stage E: Regime Classification
    t_start = time.perf_counter()
    for _ in range(50):
        regime = classify_market_regime(candles, ind_packet, k=3, symbol=symbol, timeframe="5m", structure_data=struct, break_data=breaks)
    dt_regime = (time.perf_counter() - t_start) / 50 * 1000.0

    # Stage F: Divergence Detection (RSI + MACD)
    rsi_series = ind_packet.get("oscillators", {}).get("rsi_series", [])
    if not rsi_series:
        from app.core.indicators import calculate_rsi
        rsi_series = calculate_rsi(closes, 14)
    t_start = time.perf_counter()
    for _ in range(50):
        div_rsi = detect_divergence(candles, rsi_series, indicator_name="RSI", k=3, symbol=symbol, timeframe="5m")
    dt_div = (time.perf_counter() - t_start) / 50 * 1000.0

    print(f"  1. Technical Indicators Suite (EMAs, VWAP, BB, KC, Squeeze, ADX, RSI, MACD): {dt_ind:.3f} ms")
    print(f"  2. Symmetrical Fractal Pivot Identification (K=3, 400 bars):               {dt_pivots:.3f} ms")
    print(f"  3. Market Structure Classification (HH/HL/LH/LL):                             {dt_struct:.3f} ms")
    print(f"  4. Structural Break & Event Lifecycle Detection:                              {dt_breaks:.3f} ms")
    print(f"  5. Market Regime Precedence Engine (7 Priorities):                            {dt_regime:.3f} ms")
    print(f"  6. Multi-Oscillator Divergence Detection (Tie-breaking & Invalidation):       {dt_div:.3f} ms")
    print(f"  Total Single-Timeframe Execution:                                            {dt_ind + dt_pivots + dt_struct + dt_breaks + dt_regime + dt_div:.3f} ms")

    # 4. cProfile Detailed Summary
    print("\n[4] cProfile Top 10 Function Calls by Cumulative Time:")
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(10):
        _ = struct_service.analyze_multi_timeframe_suite(symbol, timeframes)
    profiler.disable()

    s = io.StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats(pstats.SortKey.CUMULATIVE)
    ps.print_stats(10)
    print(s.getvalue())

if __name__ == "__main__":
    run_reproducible_benchmark()
