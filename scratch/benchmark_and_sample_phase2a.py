"""
Phase 2A Technical Indicator Library - Benchmarking and JSON Sample Generator
"""
import time
import json
from datetime import datetime, timezone, timedelta
from app.data.candle_store import MultiTimeframeCandleStore, Candle, Timeframe
from app.core.indicators import TechnicalIndicatorService

def generate_synthetic_session_candles():
    store = MultiTimeframeCandleStore()
    
    # Base timestamp: 2026-11-09 09:15:00 IST
    ist = timezone(timedelta(hours=5, minutes=30))
    base_time = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist)
    
    timeframes = ["15s", "1m", "3m", "5m", "15m"]
    tf_seconds = {"15s": 15, "1m": 60, "3m": 180, "5m": 300, "15m": 900}
    
    for tf in timeframes:
        delta_sec = tf_seconds[tf]
        base_price = 24250.0
        fut_base_price = 24310.0
        
        for sym, bp, is_spot in [("NIFTY 50", base_price, True), ("NIFTY26NOVFUT", fut_base_price, False)]:
            store._ensure_symbol_initialized(sym)
            candle_list = []
            for i in range(500):
                s_dt = base_time + timedelta(seconds=i * delta_sec)
                e_dt = s_dt + timedelta(seconds=delta_sec)
                import math
                wave = 45.0 * math.sin(i / 15.0) + (i * 0.15)
                open_p = bp + wave
                high_p = open_p + 12.0
                low_p = open_p - 10.0
                close_p = open_p + 5.0
                vol = 0 if is_spot else (25000 + int(abs(math.sin(i)) * 15000))
                
                c = Candle(
                    symbol=sym,
                    timeframe=tf,
                    start_time=s_dt,
                    end_time=e_dt,
                    open_price=round(open_p, 2),
                    high_price=round(high_p, 2),
                    low_price=round(low_p, 2),
                    close_price=round(close_p, 2),
                    volume=vol,
                    is_closed=True,
                    candle_status="HISTORICAL"
                )
                candle_list.append(c)
            store._closed_candles[sym][tf] = candle_list
            
            # Active candle
            s_dt = base_time + timedelta(seconds=500 * delta_sec)
            e_dt = s_dt + timedelta(seconds=delta_sec)
            active_c = Candle(
                symbol=sym,
                timeframe=tf,
                start_time=s_dt,
                end_time=e_dt,
                open_price=round(close_p, 2),
                high_price=round(close_p + 8.0, 2),
                low_price=round(close_p - 3.0, 2),
                close_price=round(close_p + 4.5, 2),
                volume=0 if is_spot else 1250,
                is_closed=False,
                candle_status="LIVE"
            )
            store._active_candle[sym][tf] = active_c
            
    return store

def run_benchmarks(store: MultiTimeframeCandleStore):
    service = TechnicalIndicatorService(store)
    ist = timezone(timedelta(hours=5, minutes=30))
    current_dt = datetime(2026, 11, 9, 10, 0, 0, tzinfo=ist)
    
    # Benchmark 1: Individual indicator calculations on 500 candles (5m timeframe)
    iterations = 500
    
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_moving_averages("NIFTY 50", "5m")
    ema_time = (time.perf_counter() - t0) / iterations * 1000.0  # ms
    
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_momentum("NIFTY 50", "5m")
    mom_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_trend_strength("NIFTY 50", "5m")
    adx_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_volatility("NIFTY 50", "5m")
    vol_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_session_vwap("NIFTY 50", "5m", current_dt=current_dt)
    vwap_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    # Benchmark 2: Full suite on a single timeframe
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_all_indicators("NIFTY 50", "5m")
    full_tf_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    # Benchmark 3: Multi-timeframe suite (all 5 timeframes: 15s, 1m, 3m, 5m, 15m)
    t0 = time.perf_counter()
    for _ in range(iterations):
        service.compute_multi_timeframe_suite("NIFTY 50", timeframes=["15s", "1m", "3m", "5m", "15m"])
    mtf_time = (time.perf_counter() - t0) / iterations * 1000.0
    
    print("=== Phase 2A Indicator Performance Benchmarks (500 bars per timeframe) ===")
    print(f"Moving Averages (EMA 9,15,20,50,200 + DEMA 9 + TEMA 9): {ema_time:.3f} ms")
    print(f"Momentum (RSI 14 + MACD 12,26,9 + ROC 14):            {mom_time:.3f} ms")
    print(f"Trend Strength (ADX 14 + +DI + -DI):                   {adx_time:.3f} ms")
    print(f"Volatility (BB 20,2.0 + KC 20,1.5 + Squeeze + ATR 14):   {vol_time:.3f} ms")
    print(f"Session VWAP (Variance + Bands + Futures Proxy):        {vwap_time:.3f} ms")
    print(f"Full Single-Timeframe Indicator Suite:                  {full_tf_time:.3f} ms")
    print(f"Full Multi-Timeframe Suite (5 Timeframes x 500 Bars):   {mtf_time:.3f} ms")
    print(f"Throughput: {1000.0 / mtf_time:.1f} complete multi-timeframe suite calculations/sec")

def dump_sample_json(store: MultiTimeframeCandleStore):
    service = TechnicalIndicatorService(store)
    result_closed = service.compute_multi_timeframe_suite(
        "NIFTY 50",
        timeframes=["15s", "1m", "3m", "5m", "15m"],
        include_developing=False
    )
    result_developing = service.compute_multi_timeframe_suite(
        "NIFTY 50",
        timeframes=["15s", "1m", "3m", "5m", "15m"],
        include_developing=True
    )
    
    combined = {
        "closed_indicators": result_closed,
        "developing_active_indicators": result_developing
    }
    
    with open("scratch/sample_nifty_indicators.json", "w") as f:
        json.dump(combined, f, indent=2)
    
    print("Saved sample JSON output to scratch/sample_nifty_indicators.json")

if __name__ == "__main__":
    store = generate_synthetic_session_candles()
    run_benchmarks(store)
    dump_sample_json(store)
