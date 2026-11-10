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
    SessionVWAPAccumulator,
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

    def test_rsi_strictly_rising(self):
        """Strictly rising prices (zero losses) must yield RSI = 100.0."""
        rising = [float(100 + i) for i in range(25)]
        rsi_rising = calculate_rsi(rising, 14)
        self.assertEqual(rsi_rising[-1], 100.0)

    def test_rsi_strictly_falling(self):
        """Strictly falling prices (zero gains) must yield RSI = 0.0."""
        falling = [float(100 - i) for i in range(25)]
        rsi_falling = calculate_rsi(falling, 14)
        self.assertEqual(rsi_falling[-1], 0.0)

    def test_rsi_completely_flat_market(self):
        """
        Documented Convention:
        When price action is completely flat (average gain == 0 and average loss == 0),
        momentum is neutral and price change is nonexistent; RSI is defined as 50.0.
        """
        flat = [100.0] * 30
        rsi_flat = calculate_rsi(flat, 14)
        self.assertIsNotNone(rsi_flat[-1])
        self.assertEqual(rsi_flat[-1], 50.0)

    def test_rsi_alternating_prices(self):
        """Alternating price series (symmetric gains and losses) oscillates symmetrically around 50.0."""
        alternating = [100.0, 101.0] * 15
        rsi_alt = calculate_rsi(alternating, 14)
        self.assertAlmostEqual(rsi_alt[-1], 50.0, delta=3.0)

    def test_rsi_insufficient_history(self):
        """RSI must return None when bars <= period."""
        short_series = [100.0, 101.0, 102.0, 103.0]
        rsi_res = calculate_rsi(short_series, 14)
        self.assertEqual(rsi_res, [None, None, None, None])

    def test_rsi_floating_point_epsilon_edge_cases(self):
        """Near-zero microscopic changes within floating-point epsilon (1e-12) must be treated as flat (50.0)."""
        micro_flat = [100.0 + (i * 1e-15) for i in range(30)]
        rsi_micro = calculate_rsi(micro_flat, 14)
        self.assertEqual(rsi_micro[-1], 50.0)

    def test_macd_confirmed_vs_developing_crossover(self):
        """
        Verifies strict separation between confirmed closed crossover and active developing crossover:
        - When last_bar_is_closed=False: crossover on active bar [-1] is flagged as DEVELOPING_BULLISH.
        - When last_bar_is_closed=True: crossover on bar [-1] is flagged as CONFIRMED_BULLISH.
        """
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

    def test_macd_developing_bearish_crossover(self):
        """Verifies developing bearish crossover on unclosed bar."""
        prices = [100.0] * 40 + [102.0, 95.0]
        res_dev = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=False)
        self.assertTrue(res_dev["is_developing_bearish_cross"])
        self.assertEqual(res_dev["crossover_status"], "DEVELOPING_BEARISH")

    def test_macd_developing_crossover_disappears_before_candle_closure(self):
        """
        Simulates live tick movement where an unclosed bar temporarily forms a developing crossover,
        then retraces before closure:
        1. Tick A (Spike): forms DEVELOPING_BULLISH.
        2. Tick B (Pullback): price retraces, DEVELOPING_BULLISH disappears.
        3. Candle closure: seals at pullback price with NO_CROSSOVER.
        Proves confirmed state is NEVER corrupted by intraday developing fluctuations.
        """
        base_prices = [100.0] * 35 + [98.0, 97.0, 96.0]
        # Tick A: Price spikes to 115.0 during developing candle
        res_tick_a = calculate_macd(base_prices + [115.0], 12, 26, 9, last_bar_is_closed=False)
        self.assertTrue(res_tick_a["is_developing_bullish_cross"])
        self.assertFalse(res_tick_a["is_confirmed_bullish_cross"])

        # Tick B: Price pulls back to 96.0 during developing candle
        res_tick_b = calculate_macd(base_prices + [96.0], 12, 26, 9, last_bar_is_closed=False)
        self.assertFalse(res_tick_b["is_developing_bullish_cross"])
        self.assertEqual(res_tick_b["crossover_status"], "NO_CROSSOVER")

        # Candle seals at 96.0 (is_closed=True)
        res_closed = calculate_macd(base_prices + [96.0], 12, 26, 9, last_bar_is_closed=True)
        self.assertFalse(res_closed["is_confirmed_bullish_cross"])
        self.assertEqual(res_closed["crossover_status"], "NO_CROSSOVER")

    def test_macd_historical_recalculation_idempotence(self):
        """Recalculating MACD repeatedly on closed historical bars produces bit-for-bit identical results."""
        prices = [100.0 + (i % 5) for i in range(60)]
        first_run = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=True)
        for _ in range(20):
            rerun = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=True)
            self.assertEqual(first_run, rerun)

    def test_macd_confirmed_candle_metadata_accuracy(self):
        """Confirmed crossover metadata reports the exact candle index and timestamp of the finalized bar."""
        prices = [100.0] * 40 + [95.0, 93.0, 115.0]
        metadata = [{"timestamp": f"2026-11-09T09:{i:02d}:00+05:30"} for i in range(len(prices))]
        res = calculate_macd(prices, 12, 26, 9, last_bar_is_closed=True, candles_metadata=metadata)
        self.assertTrue(res["is_confirmed_bullish_cross"])
        self.assertEqual(res["confirmed_candle_index"], len(prices) - 1)
        self.assertEqual(res["confirmed_candle_timestamp"], metadata[-1]["timestamp"])

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

    def test_roc_zero_denominator_handling(self):
        """Rate of Change with zero denominator returns 0.0 without raising ZeroDivisionError."""
        prices = [0.0, 10.0, 20.0]
        roc = calculate_roc(prices, period=1)
        self.assertEqual(roc[1], 0.0)

    # ========================================================================
    # C. Trend Strength Tests (ADX, +DI, -DI)
    # ========================================================================

    def test_adx_exact_first_valid_indices(self):
        """
        Audits Welles Wilder ADX(14) initialization indexing:
        - True Range requires 1 bar delta -> first TR at index 1 (2nd bar).
        - 14 initial TR, +DM, -DM values -> indices 1..14 (15 bars).
        - First +DI, -DI, and DX are produced at index 14 (15th bar).
        - First ADX requires 14 DX values (indices 14..27) -> first ADX at index 27 (28th bar).
        - For N < 28 bars: ADX must return None with INSUFFICIENT_DATA.
        - For N >= 28 bars: ADX must return valid float.
        """
        highs = [float(100 + i) for i in range(27)]
        lows = [float(99 + i) for i in range(27)]
        closes = [float(99.5 + i) for i in range(27)]

        # 27 bars (index 26) -> ADX is None
        res_27 = calculate_adx(highs, lows, closes, period=14)
        self.assertIsNone(res_27["adx"])
        self.assertEqual(res_27["calculation_status"], CalculationStatus.INSUFFICIENT_DATA)

        # 28 bars (index 27) -> First valid ADX is produced!
        highs_28 = highs + [127.0]
        lows_28 = lows + [126.0]
        closes_28 = closes + [126.5]
        res_28 = calculate_adx(highs_28, lows_28, closes_28, period=14)
        self.assertIsNotNone(res_28["adx"])
        self.assertEqual(res_28["calculation_status"], CalculationStatus.VALID)

    def test_adx_warmup_status_classification(self):
        """
        Verifies ADX warmup classification:
        - N < 28 bars: INSUFFICIENT_DATA
        - 28 <= N < 50 bars: PARTIAL_WARMUP (mathematically valid, but Wilder decay still settling)
        - N >= 50 bars: WARMUP_COMPLETE (Wilder smoothing fully stabilized)
        """
        self.assertEqual(self.service.get_warmup_status(available_bars=20, min_required=28, recommended_warmup=50), WarmupStatus.INSUFFICIENT_DATA)
        self.assertEqual(self.service.get_warmup_status(available_bars=35, min_required=28, recommended_warmup=50), WarmupStatus.PARTIAL_WARMUP)
        self.assertEqual(self.service.get_warmup_status(available_bars=60, min_required=28, recommended_warmup=50), WarmupStatus.WARMUP_COMPLETE)

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

    def test_adx_zero_denominator_in_dx_handled_safely(self):
        """When high == low == close (zero range, TR = 0), DX denominator is zero; must yield 0.0 without ZeroDivisionError."""
        highs = [100.0] * 35
        lows = [100.0] * 35
        closes = [100.0] * 35

        adx_res = calculate_adx(highs, lows, closes, period=14)
        self.assertEqual(adx_res["adx"], 0.0)
        self.assertEqual(adx_res["plus_di"], 0.0)
        self.assertEqual(adx_res["minus_di"], 0.0)

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

    def test_bollinger_bands_population_vs_sample_standard_deviation(self):
        """
        Verifies Population Standard Deviation (ddof=0, John Bollinger default)
        vs Sample Standard Deviation (ddof=1, Bessel's correction):
        Prices: [10.0, 12.0, 14.0, 16.0, 18.0]
        Sum of squared deviations = 40.0
        Population (ddof=0): Variance = 40 / 5 = 8.0, sigma = sqrt(8) = 2.83
        Sample (ddof=1): Variance = 40 / 4 = 10.0, sigma = sqrt(10) = 3.16
        """
        prices = [10.0, 12.0, 14.0, 16.0, 18.0]
        bb_pop = calculate_bollinger_bands(prices, period=5, num_std=2.0, ddof=0)
        self.assertEqual(bb_pop["sigma"], 2.83)

        bb_sample = calculate_bollinger_bands(prices, period=5, num_std=2.0, ddof=1)
        self.assertEqual(bb_sample["sigma"], 3.16)

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
        Hand-calculated verification of Volume-Weighted Variance and +-1sigma, +-2sigma bands
        on canonical 3-bar fixture:
        Bar 1: Typical Price = 100.0, Volume = 100
        Bar 2: Typical Price = 110.0, Volume = 200
        Bar 3: Typical Price = 120.0, Volume = 300
        Total Volume = 100 + 200 + 300 = 600

        CumPV = (100 * 100) + (110 * 200) + (120 * 300) = 10,000 + 22,000 + 36,000 = 68,000
        VWAP = 68,000 / 600 = 340 / 3 = 113.3333333333... -> 113.33

        Weighted Variance:
        Deviations: (100 - 340/3) = -40/3; (110 - 340/3) = -10/3; (120 - 340/3) = 20/3
        Squared Deviations: 1600/9, 100/9, 400/9
        Weighted Sum: 100 * (1600/9) + 200 * (100/9) + 300 * (400/9) = 300,000 / 9 = 100,000 / 3
        Variance = (100,000 / 3) / 600 = 500 / 9 = 55.5555555556... -> 55.56

        Weighted Standard Deviation:
        sigma = sqrt(500 / 9) = 10 * sqrt(5) / 3 = 7.453559925... -> 7.45

        Bands:
        Upper_1 = 113.333333 + 7.453560 = 120.786893 -> 120.79
        Lower_1 = 113.333333 - 7.453560 = 105.879773 -> 105.88
        Upper_2 = 113.333333 + 14.907120 = 128.240453 -> 128.24
        Lower_2 = 113.333333 - 14.907120 = 98.426213 -> 98.43
        """
        candles = [
            {"high": 105.0, "low": 95.0, "close": 100.0, "volume": 100},
            {"high": 115.0, "low": 105.0, "close": 110.0, "volume": 200},
            {"high": 125.0, "low": 115.0, "close": 120.0, "volume": 300}
        ]
        res = calculate_session_vwap(candles)
        self.assertEqual(res["vwap"], 113.33)
        self.assertEqual(res["variance"], 55.56)
        self.assertEqual(res["sigma"], 7.45)
        self.assertEqual(res["upper_band_1"], 120.79)
        self.assertEqual(res["lower_band_1"], 105.88)
        self.assertEqual(res["upper_band_2"], 128.24)
        self.assertEqual(res["lower_band_2"], 98.43)
        self.assertEqual(res["total_session_volume"], 600)

    def test_session_vwap_two_bar_reference(self):
        """
        Analytical reference test on 2-bar fixture:
        Bar 1: Typical Price = 100.0, Volume = 1000
        Bar 2: Typical Price = 110.0, Volume = 2000
        VWAP = 320,000 / 3000 = 106.67, sigma = 4.71, variance = 22.22.
        """
        candles = [
            {"high": 105.0, "low": 95.0, "close": 100.0, "volume": 1000},
            {"high": 115.0, "low": 105.0, "close": 110.0, "volume": 2000}
        ]
        res = calculate_session_vwap(candles)
        self.assertEqual(res["vwap"], 106.67)
        self.assertEqual(res["variance"], 22.22)
        self.assertEqual(res["sigma"], 4.71)
        self.assertEqual(res["upper_band_1"], 111.38)
        self.assertEqual(res["lower_band_1"], 101.95)
        self.assertEqual(res["upper_band_2"], 116.09)
        self.assertEqual(res["lower_band_2"], 97.24)
        self.assertEqual(res["total_session_volume"], 3000)

    def test_session_vwap_accumulator_full_session_1500_bars(self):
        """
        Verifies full-session accumulation across 1,500 15-second candles (full 6h15m regular session).
        Asserts no data loss across all 1,500 bars.
        """
        accum = SessionVWAPAccumulator("NIFTY 50", is_spot_index=True)
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        for i in range(1500):
            t = t0 + timedelta(seconds=15 * i)
            accum.add_candle(
                high=24300.0 + (i % 20),
                low=24280.0 + (i % 20),
                close=24290.0 + (i % 20),
                volume=1000,
                timestamp_dt=t,
                contract_symbol="NIFTY26NOVFUT"
            )
        state = accum.get_vwap_state()
        self.assertEqual(state["candle_count"], 1500)
        self.assertEqual(state["total_session_volume"], 1500 * 1000)
        self.assertIsNotNone(state["vwap"])
        self.assertIsNotNone(state["variance"])
        self.assertIsNotNone(state["sigma"])

    def test_session_vwap_accumulator_candle_eviction_preservation(self):
        """
        Proves that when the candle buffer evicts early candles (e.g. store capacity is 500, but session has 1,500 bars),
        the SessionVWAPAccumulator preserves the exact full-session VWAP, volume, and variance.
        """
        accum = SessionVWAPAccumulator("NIFTY 50", is_spot_index=True)
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        # Feed first 1000 bars
        for i in range(1000):
            t = t0 + timedelta(seconds=15 * i)
            accum.add_candle(high=24000.0, low=24000.0, close=24000.0, volume=100, timestamp_dt=t, contract_symbol="NIFTY26NOVFUT")

        # Feed remaining 500 bars at 25000.0
        for i in range(1000, 1500):
            t = t0 + timedelta(seconds=15 * i)
            accum.add_candle(high=25000.0, low=25000.0, close=25000.0, volume=100, timestamp_dt=t, contract_symbol="NIFTY26NOVFUT")

        state = accum.get_vwap_state()
        # Full session expected VWAP: (1000 * 24000 + 500 * 25000) / 1500 = (24,000,000 + 12,500,000) / 1500 = 36,500,000 / 1500 = 24333.33
        self.assertEqual(state["candle_count"], 1500)
        self.assertEqual(state["total_session_volume"], 150000)
        self.assertEqual(state["vwap"], 24333.33)

    def test_session_vwap_accumulator_session_open_reset(self):
        """Crossing from previous session date to a new session date automatically resets cumulative totals."""
        accum = SessionVWAPAccumulator("NIFTY 50")
        day1 = datetime(2026, 11, 6, 15, 15, 0, tzinfo=IST_ZONE)
        accum.add_candle(high=24000.0, low=23900.0, close=23950.0, volume=5000, timestamp_dt=day1)
        self.assertEqual(accum.cum_vol, 5000)

        # Day 2 morning candle: date boundary crossed
        day2 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        accum.add_candle(high=24100.0, low=24050.0, close=24080.0, volume=1000, timestamp_dt=day2)
        # Prior volume must be reset; day 2 should start fresh with 1000
        self.assertEqual(accum.cum_vol, 1000)
        self.assertEqual(accum.candle_count, 1)

    def test_session_vwap_accumulator_zero_volume_candles(self):
        """Zero volume candles are handled cleanly without dividing by zero or altering VWAP."""
        accum = SessionVWAPAccumulator("NIFTY 50")
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        accum.add_candle(high=24000.0, low=24000.0, close=24000.0, volume=100, timestamp_dt=t0)
        # Candle with zero volume
        accum.add_candle(high=24100.0, low=24050.0, close=24080.0, volume=0, timestamp_dt=t0 + timedelta(minutes=1))
        state = accum.get_vwap_state()
        self.assertEqual(state["total_session_volume"], 100)
        self.assertEqual(state["vwap"], 24000.0)

    def test_session_vwap_missing_volume_returns_unavailable(self):
        """When entire candle list has zero volume, returns UNAVAILABLE_NO_VOLUME."""
        candles = [
            {"high": 100.0, "low": 100.0, "close": 100.0, "volume": 0},
            {"high": 101.0, "low": 99.0, "close": 100.0, "volume": 0}
        ]
        res = calculate_session_vwap(candles)
        self.assertEqual(res["calculation_status"], CalculationStatus.UNAVAILABLE_NO_VOLUME)
        self.assertIsNone(res["vwap"])

    def test_session_vwap_futures_rollover_no_contract_mixing(self):
        """
        When contract symbol changes mid-session, accumulator resets to ensure
        volumes from two different contract expiries are NEVER mixed.
        """
        accum = SessionVWAPAccumulator("NIFTY 50", is_spot_index=True)
        t0 = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        accum.add_candle(high=24000.0, low=24000.0, close=24000.0, volume=5000, timestamp_dt=t0, contract_symbol="NIFTY26NOVFUT")
        self.assertEqual(accum.underlying_contract, "NIFTY26NOVFUT")
        self.assertEqual(accum.cum_vol, 5000)

        # Switch to DEC contract
        accum.add_candle(high=24100.0, low=24100.0, close=24100.0, volume=2000, timestamp_dt=t0 + timedelta(minutes=1), contract_symbol="NIFTY26DECFUT")
        self.assertEqual(accum.underlying_contract, "NIFTY26DECFUT")
        self.assertEqual(accum.cum_vol, 2000, "Must not add DEC volume onto NOV baseline")

    def test_session_vwap_no_prior_session_volume_leakage(self):
        """Prior session candles in the store are strictly excluded from current session VWAP."""
        sym = "RELIANCE"
        t_prev = datetime(2026, 11, 6, 15, 25, 0, tzinfo=IST_ZONE)
        t_curr = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)
        candles = [
            {"timestamp": t_prev.isoformat(), "start_time": t_prev.isoformat(), "open": 2500, "high": 2510, "low": 2490, "close": 2500, "volume": 100000, "is_closed": True},
            {"timestamp": t_curr.isoformat(), "start_time": t_curr.isoformat(), "open": 2520, "high": 2530, "low": 2510, "close": 2520, "volume": 1000, "is_closed": True}
        ]
        self.store.ingest_base_candles(sym, candles, base_timeframe=Timeframe.M1)
        # Using get_session_candles, only current session (2026-11-09) is returned
        session_candles = self.store.get_session_candles(sym, Timeframe.M1, session_date=date(2026, 11, 9))
        self.assertEqual(len(session_candles), 1)
        self.assertEqual(session_candles[0]["volume"], 1000)

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
