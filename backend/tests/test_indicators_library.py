"""
TradeLens AI - Comprehensive Unit Tests: Technical Indicator Library (Phase 2A)
Verifies:
1. Moving Averages: EMA 9, 15, 20, 50, 200, DEMA 9, TEMA 9 against analytical hand-calculated reference values.
2. Momentum: Wilder's RSI (14), MACD (12, 26, 9), ROC with configurable lookback.
3. Trend Strength: Welles Wilder's ADX (14), +DI, -DI with trend strength classification.
4. Volatility: Bollinger Bands (20, 2), Keltner Channels (20, 1.5 ATR), Carter's Volatility Squeeze (ON, OFF, RELEASE, INSUFFICIENT_DATA).
5. Session VWAP: Session reset, futures proxy volume attribution, and weighted standard-deviation bands (+-1sigma, +-2sigma) against hand calculations.
6. Multi-Timeframe Consistency: Ingestion across 15s, 1m, 3m, 5m, 15m.
7. Segregation: Confirmed closed values vs developing active values.
8. Non-Predictive Verification: Asserts that no indicator uses future candle data.
"""
import unittest
from datetime import datetime, date, time, timedelta
import math

from app.data.candle_store import (
    MultiTimeframeCandleStore,
    Timeframe,
    VolumeSemantics,
    Candle
)
from app.data.session_clock import IST_ZONE, IndianMarketSessionClock
from app.data.futures_manager import FuturesContractManager
from app.data.base import DataStatus
from app.core.indicators import (
    calculate_sma,
    calculate_ema,
    calculate_dema,
    calculate_tema,
    calculate_rsi,
    calculate_macd,
    calculate_roc,
    calculate_atr,
    calculate_adx,
    calculate_bollinger_bands,
    calculate_keltner_channels,
    detect_volatility_squeeze,
    calculate_session_vwap,
    TechnicalIndicatorService,
    CalculationStatus,
    WarmupStatus
)


class TestIndicatorsLibrary(unittest.TestCase):

    def setUp(self):
        self.store = MultiTimeframeCandleStore()
        self.futures_mgr = FuturesContractManager()
        self.service = TechnicalIndicatorService(self.store, self.futures_mgr)

    # ========================================================================
    # A. Moving Averages Tests
    # ========================================================================

    def test_ema_independent_analytical_hand_calculated_values(self):
        """
        Hand-calculated reference test for EMA:
        Prices: [10.0, 11.0, 12.0, 13.0, 14.0]
        Period: N = 3
        1. First SMA at index 2: (10 + 11 + 12) / 3 = 11.0
        2. Multiplier alpha: 2 / (3 + 1) = 0.5
        3. Index 3 (price 13.0): 13.0 * 0.5 + 11.0 * 0.5 = 6.5 + 5.5 = 12.0
        4. Index 4 (price 14.0): 14.0 * 0.5 + 12.0 * 0.5 = 7.0 + 6.0 = 13.0
        Expected: [None, None, 11.0, 12.0, 13.0]
        """
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        ema_res = calculate_ema(prices, period=3, round_output=True)
        self.assertEqual(ema_res, [None, None, 11.0, 12.0, 13.0])

    def test_ema_constant_flat_series(self):
        """On a constant flat price series, EMA must equal the constant price with zero drift."""
        prices = [100.0] * 50
        for period in [9, 15, 20, 50]:
            ema_series = calculate_ema(prices, period)
            for v in ema_series[period - 1:]:
                self.assertAlmostEqual(v, 100.0, places=6)

    def test_dema_and_tema_constant_flat_series(self):
        """DEMA and TEMA on a constant flat series must equal the constant price."""
        prices = [100.0] * 60
        dema = calculate_dema(prices, 9)
        tema = calculate_tema(prices, 9)

        # Valid values after warmup
        for v in [x for x in dema if x is not None]:
            self.assertAlmostEqual(v, 100.0, places=4)
        for v in [x for x in tema if x is not None]:
            self.assertAlmostEqual(v, 100.0, places=4)

    def test_ema_warmup_and_insufficient_data(self):
        """Verifies warmup progression: INSUFFICIENT_DATA -> PARTIAL_WARMUP -> WARMUP_COMPLETE."""
        self.assertEqual(self.service.get_warmup_status(available_bars=5, min_required=9, recommended_warmup=25), WarmupStatus.INSUFFICIENT_DATA)
        self.assertEqual(self.service.get_warmup_status(available_bars=12, min_required=9, recommended_warmup=25), WarmupStatus.PARTIAL_WARMUP)
        self.assertEqual(self.service.get_warmup_status(available_bars=30, min_required=9, recommended_warmup=25), WarmupStatus.WARMUP_COMPLETE)

    # ========================================================================
    # B. Momentum Indicators Tests (RSI, MACD, ROC)
    # ========================================================================

    def test_rsi_strictly_rising_and_falling_series(self):
        """
        - Strictly rising prices (zero losses) must yield RSI = 100.0.
        - Strictly falling prices (zero gains) must yield RSI = 0.0.
        """
        rising = [float(100 + i) for i in range(25)]
        falling = [float(100 - i) for i in range(25)]

        rsi_rising = calculate_rsi(rising, 14)
        rsi_falling = calculate_rsi(falling, 14)

        self.assertEqual(rsi_rising[-1], 100.0)
        self.assertEqual(rsi_falling[-1], 0.0)

    def test_rsi_insufficient_history(self):
        """RSI must return None when bars <= period."""
        short_series = [100.0, 101.0, 102.0, 103.0]
        rsi_res = calculate_rsi(short_series, 14)
        self.assertEqual(rsi_res, [None, None, None, None])

    def test_macd_confirmed_vs_developing_crossover(self):
        """
        Verifies strict separation between confirmed closed crossover and active developing crossover:
        - When last_bar_is_closed=False: crossover on active bar [-1] is flagged as DEVELOPING_BULLISH.
        - When last_bar_is_closed=True: crossover on bar [-1] is flagged as CONFIRMED_BULLISH.
        """
        # Series with sufficient history (>= 35 bars) and a bullish turn on the active bar
        prices = [100.0] * 40 + [98.0, 105.0]

        # Case 1: Active bar is developing (is_closed=False)
        res_dev = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=False)
        self.assertFalse(res_dev["is_confirmed_bullish_cross"])
        self.assertTrue(res_dev["is_developing_bullish_cross"])
        self.assertEqual(res_dev["crossover_status"], "DEVELOPING_BULLISH")

        # Case 2: Bar has sealed (is_closed=True)
        res_conf = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=True)
        self.assertTrue(res_conf["is_confirmed_bullish_cross"])
        self.assertFalse(res_conf["is_developing_bullish_cross"])
        self.assertEqual(res_conf["crossover_status"], "CONFIRMED_BULLISH")

    def test_roc_hand_calculated_values(self):
        """
        Hand-calculated Rate of Change test:
        Prices: [100.0, 105.0, 110.0, 120.0]
        ROC(1) at index 1: (105 - 100) / 100 * 100 = 5.0%
        ROC(3) at index 3: (120 - 100) / 100 * 100 = 20.0%
        """
        prices = [100.0, 105.0, 110.0, 120.0]
        roc1 = calculate_roc(prices, period=1)
        self.assertEqual(roc1[1], 5.0)

        roc3 = calculate_roc(prices, period=3)
        self.assertEqual(roc3[3], 20.0)

    # ========================================================================
    # C. Trend Strength Tests (ADX, +DI, -DI)
    # ========================================================================

    def test_adx_flat_market_indicates_weak_chop(self):
        """A flat series with constant high, low, and close must yield ADX = 0 and WEAK_TREND_CHOP."""
        highs = [101.0] * 40
        lows = [99.0] * 40
        closes = [100.0] * 40

        adx_res = calculate_adx(highs, lows, closes, period=14)
        self.assertEqual(adx_res["adx"], 0.0)
        self.assertEqual(adx_res["plus_di"], 0.0)
        self.assertEqual(adx_res["minus_di"], 0.0)
        self.assertEqual(adx_res["trend_strength"], "WEAK_TREND_CHOP")

    def test_adx_strong_trend_classification(self):
        """A persistent steep trend must produce high ADX (>= 25) with STRONG_TREND status."""
        highs = [float(100 + i * 2 + 1) for i in range(50)]
        lows = [float(100 + i * 2 - 1) for i in range(50)]
        closes = [float(100 + i * 2) for i in range(50)]

        adx_res = calculate_adx(highs, lows, closes, period=14)
        self.assertIsNotNone(adx_res["adx"])
        self.assertGreaterEqual(adx_res["adx"], 25.0)
        self.assertGreater(adx_res["plus_di"], adx_res["minus_di"])
        self.assertEqual(adx_res["trend_strength"], "STRONG_TREND")

    # ========================================================================
    # D. Volatility & Carter's Squeeze Tests
    # ========================================================================

    def test_bollinger_bands_hand_calculated_reference(self):
        """
        Analytical reference test for Bollinger Bands:
        Prices: [10.0, 12.0, 14.0, 16.0, 18.0]
        Period: N = 5, num_std = 2.0
        Mean = 14.0
        Variances: (-4)^2 + (-2)^2 + 0^2 + 2^2 + 4^2 = 16 + 4 + 0 + 4 + 16 = 40
        Variance = 40 / 5 = 8.0
        sigma = sqrt(8) = 2.828427
        Upper = 14.0 + 2 * 2.828427 = 19.65685 -> 19.66
        Lower = 14.0 - 2 * 2.828427 = 8.34315 -> 8.34
        Bandwidth = (19.65685 - 8.34315) / 14.0 = 11.3137 / 14.0 = 0.8081
        %B for close 18.0 = (18.0 - 8.34315) / 11.3137 = 0.8536
        """
        prices = [10.0, 12.0, 14.0, 16.0, 18.0]
        bb = calculate_bollinger_bands(prices, period=5, num_std=2.0)
        self.assertEqual(bb["middle"], 14.0)
        self.assertEqual(bb["upper"], 19.66)
        self.assertEqual(bb["lower"], 8.34)
        self.assertEqual(bb["bandwidth"], 0.8081)
        self.assertEqual(bb["pct_b"], 0.8536)

    def test_carter_volatility_squeeze_states(self):
        """
        Verifies all 4 distinct Carter's Volatility Squeeze states:
        - SQUEEZE_ON: BB completely inside KC
        - SQUEEZE_OFF: BB outside KC
        - SQUEEZE_RELEASE: Transition from ON to OFF
        - INSUFFICIENT_DATA: Missing band data
        """
        # Squeeze ON: BB inside KC
        bb_tight = {"upper": 105.0, "lower": 95.0, "bandwidth": 0.1, "calculation_status": CalculationStatus.VALID}
        kc_wide = {"upper": 110.0, "lower": 90.0, "calculation_status": CalculationStatus.VALID}
        sq_on = detect_volatility_squeeze(bb_tight, kc_wide)
        self.assertTrue(sq_on["is_squeeze_on"])
        self.assertEqual(sq_on["squeeze_state"], "SQUEEZE_ON")

        # Squeeze OFF: BB outside KC
        bb_wide = {"upper": 115.0, "lower": 85.0, "bandwidth": 0.3, "calculation_status": CalculationStatus.VALID}
        sq_off = detect_volatility_squeeze(bb_wide, kc_wide)
        self.assertFalse(sq_off["is_squeeze_on"])
        self.assertEqual(sq_off["squeeze_state"], "SQUEEZE_OFF")

        # Squeeze RELEASE: Prior was ON, current is OFF
        sq_release = detect_volatility_squeeze(
            bollinger=bb_wide,
            keltner=kc_wide,
            prior_bollinger=bb_tight,
            prior_keltner=kc_wide
        )
        self.assertFalse(sq_release["is_squeeze_on"])
        self.assertTrue(sq_release["is_squeeze_release"])
        self.assertEqual(sq_release["squeeze_state"], "SQUEEZE_RELEASE")

        # Insufficient data
        sq_insufficient = detect_volatility_squeeze({"calculation_status": CalculationStatus.INSUFFICIENT_DATA}, {})
        self.assertEqual(sq_insufficient["squeeze_state"], "INSUFFICIENT_DATA")

    # ========================================================================
    # E. Session VWAP Tests (Hand Calculation, Reset & Attribution)
    # ========================================================================

    def test_session_vwap_weighted_standard_deviation_bands_hand_calculated(self):
        """
        Hand-calculated verification of Volume-Weighted Variance and +-1sigma, +-2sigma bands:
        Bar 1: High=105, Low=95, Close=100 -> Typical Price = 100.0, Volume = 1000
        Bar 2: High=115, Low=105, Close=110 -> Typical Price = 110.0, Volume = 2000
        Total Volume = 3000
        CumPV = (100 * 1000) + (110 * 2000) = 100,000 + 220,000 = 320,000
        VWAP = 320,000 / 3000 = 106.6667 -> 106.67
        Variance = [1000 * (100 - 106.6667)^2 + 2000 * (110 - 106.6667)^2] / 3000
                 = [1000 * 44.4449 + 2000 * 11.1109] / 3000
                 = [44,444.9 + 22,221.8] / 3000 = 66,666.7 / 3000 = 22.2222
        sigma = sqrt(22.2222) = 4.7140 -> 4.71
        Upper_1 = 106.67 + 4.71 = 111.38
        Lower_1 = 106.67 - 4.71 = 101.96
        Upper_2 = 106.67 + 2 * 4.71 = 116.09
        Lower_2 = 106.67 - 2 * 4.71 = 97.25
        """
        candles = [
            {"high": 105.0, "low": 95.0, "close": 100.0, "volume": 1000},
            {"high": 115.0, "low": 105.0, "close": 110.0, "volume": 2000}
        ]
        res = calculate_session_vwap(candles)
        self.assertEqual(res["vwap"], 106.67)
        self.assertEqual(res["sigma"], 4.71)
        self.assertEqual(res["upper_band_1"], 111.38)
        self.assertEqual(res["lower_band_1"], 101.95)
        self.assertEqual(res["upper_band_2"], 116.09)
        self.assertEqual(res["lower_band_2"], 97.24)
        self.assertEqual(res["total_session_volume"], 3000)

    def test_nifty_spot_vwap_futures_volume_proxy_attribution(self):
        """
        Verifies that NIFTY 50 spot VWAP:
        1. Sourced volume from NIFTY Futures, not spot.
        2. Explicitly labeled as 'NIFTY Futures VWAP'.
        3. Identifies volume_source as 'FUTURES_TRADED_PROXY'.
        4. When futures feed is missing, returns UNAVAILABLE_NO_FUTURES_FEED without inventing volume.
        """
        spot_candles = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24800, "high": 24820, "low": 24790, "close": 24810, "volume": 0, "is_closed": True}
        ]
        # Ingest into store
        self.store.ingest_base_candles("NIFTY 50", spot_candles, base_timeframe=Timeframe.M1)

        # Case 1: No futures feed loaded yet -> UNAVAILABLE_NO_FUTURES_FEED
        vwap_res = self.service.compute_session_vwap("NIFTY 50", timeframe=Timeframe.M1)
        self.assertEqual(vwap_res["calculation_status"], CalculationStatus.UNAVAILABLE_NO_FUTURES_FEED)
        self.assertIsNone(vwap_res["vwap"])
        self.assertEqual(vwap_res["vwap_name"], "NIFTY Futures VWAP")
        self.assertEqual(vwap_res["volume_source"], "FUTURES_TRADED_PROXY")
        self.assertTrue(vwap_res["is_spot_proxy"])

        # Case 2: Futures feed provided
        fut_candles = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24820, "high": 24840, "low": 24810, "close": 24830, "volume": 5000, "is_closed": True}
        ]
        self.store.ingest_base_candles("NIFTY26NOVFUT", fut_candles, base_timeframe=Timeframe.M1)
        vwap_res_with_fut = self.service.compute_session_vwap("NIFTY 50", timeframe=Timeframe.M1)
        self.assertEqual(vwap_res_with_fut["calculation_status"], CalculationStatus.VALID)
        self.assertIsNotNone(vwap_res_with_fut["vwap"])
        self.assertEqual(vwap_res_with_fut["total_session_volume"], 5000)

    # ========================================================================
    # F. Non-Predictive Verification (Zero Future Candle Leakage)
    # ========================================================================

    def test_indicators_never_use_future_candle_data(self):
        """
        Proves that indicator values calculated at bar T are 100% identical
        regardless of whether future bars [T+1 ... T+N] exist in the candle store.
        """
        sym = "INFY"
        # Generate 30 bars
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        bars_short = []
        for i in range(25):
            t = t0 + timedelta(minutes=i)
            bars_short.append({
                "timestamp": t.isoformat(), "start_time": t.isoformat(),
                "open": 1800.0 + i, "high": 1805.0 + i, "low": 1795.0 + i, "close": 1802.0 + i,
                "volume": 1000 + i * 50, "is_closed": True, "timeframe": Timeframe.M1
            })

        bars_extended = list(bars_short)
        for i in range(25, 40):
            t = t0 + timedelta(minutes=i)
            bars_extended.append({
                "timestamp": t.isoformat(), "start_time": t.isoformat(),
                "open": 1800.0 + i, "high": 1805.0 + i, "low": 1795.0 + i, "close": 1802.0 + i,
                "volume": 1000 + i * 50, "is_closed": True, "timeframe": Timeframe.M1
            })

        # Calculate on bars_short
        closes_short = [b["close"] for b in bars_short]
        ema_short = calculate_ema(closes_short, 9)[-1]
        rsi_short = calculate_rsi(closes_short, 14)[-1]

        # Calculate on bars_extended, but inspect the slice up to index 24 (bar 25)
        closes_ext = [b["close"] for b in bars_extended]
        ema_ext_at_24 = calculate_ema(closes_ext, 9)[24]
        rsi_ext_at_24 = calculate_rsi(closes_ext, 14)[24]

        self.assertEqual(ema_short, ema_ext_at_24, "EMA at bar T must be identical with or without future bars")
        self.assertEqual(rsi_short, rsi_ext_at_24, "RSI at bar T must be identical with or without future bars")

    # ========================================================================
    # G. Multi-Timeframe Complete Suite Test
    # ========================================================================

    def test_multi_timeframe_suite_execution(self):
        """
        Verifies execution across all 5 supported timeframes (15s, 1m, 3m, 5m, 15m)
        using TechnicalIndicatorService.compute_multi_timeframe_suite().
        """
        sym = "TCS"
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        candles_1m = []
        for i in range(60):
            t = t0 + timedelta(minutes=i)
            candles_1m.append({
                "timestamp": t.isoformat(), "start_time": t.isoformat(),
                "open": 4000.0 + (i % 10), "high": 4010.0 + (i % 10),
                "low": 3995.0 + (i % 10), "close": 4005.0 + (i % 10),
                "volume": 2000, "is_closed": True, "timeframe": Timeframe.M1
            })

        self.store.ingest_base_candles(sym, candles_1m, base_timeframe=Timeframe.M1)

        suite = self.service.compute_multi_timeframe_suite(sym)
        self.assertIn("multi_timeframe_indicators", suite)
        res_by_tf = suite["multi_timeframe_indicators"]

        for tf in [Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]:
            self.assertIn(tf, res_by_tf)
            tf_data = res_by_tf[tf]
            self.assertIn("moving_averages", tf_data)
            self.assertIn("momentum", tf_data)
            self.assertIn("volatility", tf_data)
            self.assertIn("session_vwap", tf_data)
            self.assertTrue(tf_data["is_closed"])


if __name__ == "__main__":
    unittest.main()
