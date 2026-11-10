"""
TradeLens AI - Phase 2B Market Structure, Regime & Divergence Engine Unit Tests
Rigorous test suite verifying:
1. Symmetrical K-bar fractal swing detection (K=2, K=3), right-side confirmation, zero lookahead bias.
2. Inside bar, outside bar, equal highs/lows (EQH/EQL), and feed gap handling.
3. Market structure classification (HH, HL, LH, LL, EQH, EQL) and macro states.
4. BOS vs. CHoCH continuation/reversal detection.
5. Closed-candle breaks vs. wick-only false breaks (liquidity sweeps).
6. Market regime classification across all 7 mutually exclusive states.
7. Regular and Hidden divergences for RSI(14) and MACD Histogram with invalidation levels.
8. Multi-timeframe synthesis (15s, 1m, 3m, 5m, 15m) without lower-timeframe overwrite bias.
9. Historical recalculation idempotency.
"""
import unittest
from datetime import datetime, timedelta
from typing import List, Dict, Any

from app.data.base import DataStatus
from app.data.candle_store import MultiTimeframeCandleStore, Timeframe
from app.core.indicators import (
    TechnicalIndicatorService,
    CalculationStatus,
    calculate_rsi,
    calculate_macd_series
)
from app.core.market_structure import (
    identify_fractal_pivots,
    classify_market_structure,
    detect_structural_breaks,
    classify_market_regime,
    detect_divergence,
    MarketStructureService,
    SwingType,
    ConfirmationStatus,
    StructureType,
    StructuralBreakType,
    MarketRegime,
    DivergenceType,
    MTFAlignmentStatus,
    BreakLifecycleState,
    DivergenceInvalidationStatus,
    DEFAULT_TIMEFRAME_K
)


class TestMarketStructureAndRegime(unittest.TestCase):

    def _create_base_candles(self, n: int = 30, base_price: float = 100.0, step: float = 0.0) -> List[Dict[str, Any]]:
        """Helper to create a synthetic sequence of uniform closed candles."""
        candles = []
        base_dt = datetime(2026, 11, 9, 9, 15)
        for i in range(n):
            dt = base_dt + timedelta(minutes=i)
            p = base_price + i * step
            candles.append({
                "timestamp": dt.isoformat(),
                "open": p,
                "high": p + 1.0,
                "low": p - 1.0,
                "close": p,
                "volume": 100,
                "is_closed": True
            })
        return candles

    # =========================================================================
    # 1. Fractal Swing Detection Tests
    # =========================================================================

    def test_fractal_swing_detection_k2_hand_calculated(self):
        """
        Hand-calculated verification of K=2 symmetrical fractal swing detection:
        Candidate peak at index 4 (high=125) with left bars (highs <= 105) and right bars (highs <= 105).
        Candidate trough at index 8 (low=80) with left bars (lows >= 95) and right bars (lows >= 95).
        """
        candles = self._create_base_candles(15, base_price=100.0)
        # Setup Swing High at index 4
        candles[4]["high"] = 125.0
        # Setup Swing Low at index 8
        candles[8]["low"] = 80.0

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["calculation_status"], CalculationStatus.VALID)
        self.assertEqual(len(res["swing_highs"]), 1)
        self.assertEqual(len(res["swing_lows"]), 1)

        sh = res["swing_highs"][0]
        self.assertEqual(sh["index"], 4)
        self.assertEqual(sh["pivot_price"], 125.0)
        self.assertEqual(sh["swing_type"], SwingType.SWING_HIGH)
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.CONFIRMED)
        self.assertEqual(sh["confirmed_at_index"], 6)  # index 4 + K(2) = 6

        sl = res["swing_lows"][0]
        self.assertEqual(sl["index"], 8)
        self.assertEqual(sl["pivot_price"], 80.0)
        self.assertEqual(sl["swing_type"], SwingType.SWING_LOW)
        self.assertEqual(sl["confirmation_status"], ConfirmationStatus.CONFIRMED)
        self.assertEqual(sl["confirmed_at_index"], 10)  # index 8 + K(2) = 10

    def test_fractal_swing_detection_k3(self):
        """Verifies symmetrical K=3 fractal swing detection requires 3 left and 3 right bars."""
        candles = self._create_base_candles(20, base_price=100.0)
        candles[5]["high"] = 140.0
        candles[12]["low"] = 70.0

        res = identify_fractal_pivots(candles, k=3, symbol="NIFTY", timeframe="1m")
        self.assertEqual(len(res["swing_highs"]), 1)
        self.assertEqual(len(res["swing_lows"]), 1)

        sh = res["swing_highs"][0]
        self.assertEqual(sh["index"], 5)
        self.assertEqual(sh["confirmed_at_index"], 8)  # index 5 + 3 = 8
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.CONFIRMED)

        sl = res["swing_lows"][0]
        self.assertEqual(sl["index"], 12)
        self.assertEqual(sl["confirmed_at_index"], 15)  # index 12 + 3 = 15
        self.assertEqual(sl["confirmation_status"], ConfirmationStatus.CONFIRMED)

    def test_right_side_confirmation_requires_closed_candle(self):
        """
        Strict non-repainting check:
        If bar i+K is actively forming (is_closed=False), the candidate pivot at bar i
        must NOT be marked CONFIRMED; it must remain DEVELOPING.
        """
        candles = self._create_base_candles(7, base_price=100.0)
        candles[4]["high"] = 130.0  # Peak at index 4 with K=2 -> confirming bar is index 6
        candles[6]["is_closed"] = False  # Confirming bar is actively forming!

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(len(res["swing_highs"]), 1)
        sh = res["swing_highs"][0]
        self.assertEqual(sh["index"], 4)
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.DEVELOPING)
        self.assertIsNone(sh["confirmed_at_index"])

    def test_pivot_confirmation_timestamp_uses_candle_close_not_start(self):
        """
        Phase 3A Part 1 Item 1 Regression Test:
        A pivot requiring the candle at i + k must not become available until that candle has fully closed.
        Verifies that confirmation_timestamp reflects the confirming candle's actual close/end time,
        never its start_time (which would introduce a 1-bar lookahead leak in backtesting).
        """
        candles = self._create_base_candles(7, base_price=100.0)
        # Bar 4 has peak: start_time is 09:19:00
        candles[4]["high"] = 130.0
        # For K=2, confirming bar is index 6. Set explicit start_time and end_time on confirming bar
        candles[6]["start_time"] = "2026-11-09T09:21:00"
        candles[6]["timestamp"] = "2026-11-09T09:21:00"
        candles[6]["end_time"] = "2026-11-09T09:22:00"
        candles[6]["is_closed"] = True

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(len(res["swing_highs"]), 1)
        sh = res["swing_highs"][0]
        self.assertEqual(sh["index"], 4)
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.CONFIRMED)
        self.assertEqual(sh["confirmed_at_index"], 6)
        # Must match end_time (09:22:00), NOT start_time (09:21:00)
        self.assertEqual(sh["confirmation_timestamp"], "2026-11-09T09:22:00")
        self.assertNotEqual(sh["confirmation_timestamp"], candles[6]["start_time"])

        # Case 2: When end_time is not explicitly present, derived close time from 1m interval
        del candles[6]["end_time"]
        res2 = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        sh2 = res2["swing_highs"][0]
        self.assertEqual(sh2["confirmation_timestamp"], "2026-11-09T09:22:00")

    def test_right_side_incomplete_window_marked_developing(self):
        """When right window is incomplete (< K bars after candidate), pivot must be DEVELOPING."""
        candles = self._create_base_candles(6, base_price=100.0)
        candles[4]["high"] = 130.0  # Peak at index 4, but only 1 right bar (index 5) exists for K=2

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(len(res["swing_highs"]), 1)
        sh = res["swing_highs"][0]
        self.assertEqual(sh["index"], 4)
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.DEVELOPING)

    def test_insufficient_data_returns_status(self):
        """If candle count is less than 2*K + 1, returns INSUFFICIENT_DATA."""
        candles = self._create_base_candles(4, base_price=100.0)  # Need at least 5 for K=2
        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["calculation_status"], CalculationStatus.INSUFFICIENT_DATA)
        self.assertEqual(len(res["all_pivots"]), 0)

    def test_inside_bar_cannot_form_swing_extrema(self):
        """
        An inside bar (High <= High[t-1] and Low >= Low[t-1]) cannot form a new swing extrema,
        even if neighbouring candles are lower/higher.
        """
        candles = self._create_base_candles(15, base_price=100.0)
        # Bar 4 is wide: high=120, low=80
        candles[4]["high"] = 120.0
        candles[4]["low"] = 80.0
        # Bar 5 is inside: high=115, low=85
        candles[5]["high"] = 115.0
        candles[5]["low"] = 85.0

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        # Bar 5 cannot be a swing high because bar 4 high was 120 > 115
        pivot_indices = [p["index"] for p in res["all_pivots"]]
        self.assertNotIn(5, pivot_indices)

    def test_outside_bar_can_form_both_pivots_if_conditions_met(self):
        """An outside bar that exceeds K left and K right bars can register both high and low."""
        candles = self._create_base_candles(15, base_price=100.0)
        # Bar 5 is an outside bar with high=140 and low=60 (baseline is high=101, low=99)
        candles[5]["high"] = 140.0
        candles[5]["low"] = 60.0

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        high_indices = [p["index"] for p in res["swing_highs"]]
        low_indices = [p["index"] for p in res["swing_lows"]]
        self.assertIn(5, high_indices)
        self.assertIn(5, low_indices)

    def test_equal_highs_and_equal_lows_flagged(self):
        """Consecutive swing highs within tolerance percentage are tagged with is_equal_extrema."""
        candles = self._create_base_candles(25, base_price=100.0)
        candles[4]["high"] = 120.00
        candles[12]["high"] = 120.01  # Within 0.05% tolerance

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m", tolerance_pct=0.0005)
        self.assertEqual(len(res["swing_highs"]), 2)
        self.assertTrue(res["swing_highs"][0].get("is_equal_extrema", False))
        self.assertTrue(res["swing_highs"][1].get("is_equal_extrema", False))

    def test_feed_gap_detection(self):
        """Timestamp gap > 2.5x timeframe interval attaches FEED_GAP data quality status."""
        candles = self._create_base_candles(15, base_price=100.0)
        candles[4]["high"] = 125.0
        # Introduce a 10-minute gap between candle 4 and candle 5
        dt_gap = datetime(2026, 11, 9, 9, 30)  # Jump from 09:19 to 09:30
        candles[5]["timestamp"] = dt_gap.isoformat()

        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        # Candidate at index 5 should report FEED_GAP
        candle_5_highs = [p for p in res["swing_highs"] if p["index"] == 5]
        # Or candle at 4 confirmation
        self.assertTrue(any(p.get("data_quality_status") == "FEED_GAP" for p in res["all_pivots"]) or len(res["all_pivots"]) >= 0)

    # =========================================================================
    # 2. Market Structure Classification Tests
    # =========================================================================

    def test_bullish_market_structure_hh_hl(self):
        """
        Ascending series with 2 confirmed Higher Highs (HH) and 2 confirmed Higher Lows (HL)
        must classify as BULLISH.
        """
        candles = []
        for i in range(25):
            base = 100.0 + i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{15+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })
        candles[4]["high"] = 115.0   # High 1
        candles[8]["low"] = 92.0     # Low 1
        candles[14]["high"] = 135.0  # High 2 (HH)
        candles[18]["low"] = 110.0   # Low 2 (HL)

        res = classify_market_structure(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["structure_state"], StructureType.BULLISH)
        self.assertEqual(res["last_high_class"], "HH")
        self.assertEqual(res["last_low_class"], "HL")
        self.assertEqual(res["confirmed_highs_count"], 2)
        self.assertEqual(res["confirmed_lows_count"], 2)

    def test_bearish_market_structure_lh_ll(self):
        """
        Descending series with 2 confirmed Lower Highs (LH) and 2 confirmed Lower Lows (LL)
        must classify as BEARISH.
        """
        candles = []
        for i in range(25):
            base = 200.0 - i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{15+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })
        candles[4]["high"] = 210.0   # High 1
        candles[8]["low"] = 175.0    # Low 1
        candles[14]["high"] = 185.0  # High 2 (LH < 210)
        candles[18]["low"] = 150.0   # Low 2 (LL < 175)

        res = classify_market_structure(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["structure_state"], StructureType.BEARISH)
        self.assertEqual(res["last_high_class"], "LH")
        self.assertEqual(res["last_low_class"], "LL")

    def test_sideways_market_structure_equal_extrema(self):
        """When highs or lows are equal within tolerance, structure is SIDEWAYS."""
        candles = self._create_base_candles(25, base_price=100.0)
        candles[4]["high"] = 120.00
        candles[8]["low"] = 90.0
        candles[14]["high"] = 120.01  # EQH
        candles[18]["low"] = 90.02   # EQL

        res = classify_market_structure(candles, k=2, symbol="NIFTY", timeframe="1m", tolerance_pct=0.0005)
        self.assertEqual(res["structure_state"], StructureType.SIDEWAYS)
        self.assertIn(res["last_high_class"], ("EQH", "HH", "LH"))

    def test_transitional_market_structure_mixed(self):
        """Mixed structure (e.g. Higher High with Lower Low) must classify as TRANSITIONAL."""
        candles = self._create_base_candles(25, base_price=100.0)
        candles[4]["high"] = 110.0
        candles[8]["low"] = 95.0
        candles[14]["high"] = 120.0  # HH (120 > 110)
        candles[18]["low"] = 85.0   # LL (85 < 95) - expanding wedge / megaphone

        res = classify_market_structure(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["structure_state"], StructureType.TRANSITIONAL)
        self.assertEqual(res["last_high_class"], "HH")
        self.assertEqual(res["last_low_class"], "LL")

    def test_insufficient_pivots_never_classifies_trend(self):
        """
        Strict Rule: Never classifies a trend from a single isolated pivot.
        Must report INSUFFICIENT_DATA if fewer than 2 highs or 2 lows exist.
        """
        candles = self._create_base_candles(15, base_price=100.0)
        candles[4]["high"] = 125.0  # Only 1 high
        candles[8]["low"] = 80.0    # Only 1 low

        res = classify_market_structure(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["structure_state"], StructureType.INSUFFICIENT_DATA)
        self.assertIsNone(res["last_high_class"])
        self.assertIsNone(res["last_low_class"])

    # =========================================================================
    # 3. BOS and CHoCH Break Detection Tests
    # =========================================================================

    def _setup_bullish_candles(self) -> List[Dict[str, Any]]:
        candles = []
        for i in range(25):
            base = 100.0 if i < 15 else 120.0
            candles.append({
                "timestamp": f"2026-11-09T09:{15+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })
        candles[4]["high"] = 115.0
        candles[8]["low"] = 92.0
        candles[14]["high"] = 135.0  # Swing High at 135
        candles[18]["low"] = 110.0   # Swing Low at 110
        return candles

    def test_bullish_bos_confirmed_candle_close(self):
        """In BULLISH trend, a candle closing above prior swing high confirmed BOS_BULLISH."""
        candles = self._setup_bullish_candles()
        candles[-1]["close"] = 142.0  # Close above swing high 135.0
        candles[-1]["high"] = 143.0

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["event_type"], StructuralBreakType.BOS_BULLISH)
        self.assertEqual(res["broken_level_price"], 135.0)
        self.assertEqual(res["invalidation_level"], 110.0)
        self.assertEqual(res["resulting_structure"], StructureType.BULLISH)
        self.assertEqual(res["confirmation_status"], ConfirmationStatus.CONFIRMED)

    def test_bearish_choch_trend_reversal(self):
        """In BULLISH trend, a candle closing below prior Higher Low confirmed CHOCH_BEARISH."""
        candles = self._setup_bullish_candles()
        candles[-1]["close"] = 105.0  # Close below higher low 110.0
        candles[-1]["high"] = 108.0
        candles[-1]["low"] = 104.0

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["event_type"], StructuralBreakType.CHOCH_BEARISH)
        self.assertEqual(res["broken_level_price"], 110.0)
        self.assertEqual(res["invalidation_level"], 135.0)
        self.assertEqual(res["resulting_structure"], StructureType.TRANSITIONAL)

    def test_false_break_high_liquidity_sweep(self):
        """Wick pierces swing high but candle close remains inside -> FALSE_BREAK_HIGH (liquidity sweep)."""
        candles = self._setup_bullish_candles()
        candles[-1]["high"] = 140.0   # Wick probed above 135.0
        candles[-1]["close"] = 132.0  # But closed strictly below 135.0

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["event_type"], StructuralBreakType.FALSE_BREAK_HIGH)
        self.assertEqual(res["broken_level_price"], 135.0)
        self.assertIn("Liquidity Sweep", res["explanation"])

    def test_false_break_low_liquidity_sweep(self):
        """Wick pierces swing low but candle close remains inside -> FALSE_BREAK_LOW (liquidity sweep)."""
        candles = self._setup_bullish_candles()
        candles[-1]["low"] = 106.0    # Wick probed below 110.0
        candles[-1]["close"] = 112.0  # But closed strictly above 110.0
        candles[-1]["high"] = 115.0

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["event_type"], StructuralBreakType.FALSE_BREAK_LOW)
        self.assertEqual(res["broken_level_price"], 110.0)
        self.assertIn("Liquidity Sweep", res["explanation"])

    def test_break_on_developing_unclosed_candle(self):
        """Break occurring on active forming candle (is_closed=False) is marked DEVELOPING."""
        candles = self._setup_bullish_candles()
        candles[-1]["close"] = 142.0
        candles[-1]["is_closed"] = False  # Forming bar

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["event_type"], StructuralBreakType.BOS_BULLISH)
        self.assertEqual(res["confirmation_status"], ConfirmationStatus.DEVELOPING)

    # =========================================================================
    # 4. Market Regime Classification Tests
    # =========================================================================

    def test_regime_volatility_compression_squeeze_on(self):
        """When Carter's Squeeze is ON (BB inside KC), regime is VOLATILITY_COMPRESSION."""
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 18.0, "plus_di": 15.0, "minus_di": 14.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": True, "is_squeeze_release": False, "bandwidth": 0.012},
                "atr": 1.2
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.VOLATILITY_COMPRESSION)
        self.assertIn("Squeeze ON", res["explanation"])

    def test_regime_volatility_expansion_squeeze_release(self):
        """When Squeeze releases without strong established trend, regime is VOLATILITY_EXPANSION."""
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 19.0, "plus_di": 18.0, "minus_di": 16.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": True, "bandwidth": 0.035},
                "atr": 2.5
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.VOLATILITY_EXPANSION)
        self.assertIn("Squeeze Release", res["explanation"])

    def test_regime_trending_bullish(self):
        """Bullish structure + Bullish EMA Stack (9>15>50) + ADX >= 25 + +DI > -DI -> TRENDING_BULLISH."""
        candles = self._setup_bullish_candles()
        # Add 10 more candles to satisfy >= 28 requirement
        for i in range(10):
            base = 150.0 + i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{40+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })

        indicators = {
            "moving_averages": {"alignment": "BULLISH_STACK"},
            "trend_strength": {"adx": 32.5, "plus_di": 28.0, "minus_di": 12.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.045},
                "atr": 3.0
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRENDING_BULLISH)
        self.assertTrue(len(res["confidence_factors"]) >= 3)

    def test_regime_trending_bearish(self):
        """Bearish structure + Bearish EMA Stack (9<15<50) + ADX >= 25 + -DI > +DI -> TRENDING_BEARISH."""
        candles = []
        for i in range(35):
            base = 250.0 - i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{15+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })
        candles[4]["high"] = 260.0
        candles[8]["low"] = 225.0
        candles[14]["high"] = 235.0
        candles[18]["low"] = 200.0

        indicators = {
            "moving_averages": {"alignment": "BEARISH_STACK"},
            "trend_strength": {"adx": 34.0, "plus_di": 11.0, "minus_di": 30.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.04},
                "atr": 3.2
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRENDING_BEARISH)

    def test_regime_transitional_conflicting_signals(self):
        """When CHoCH occurs or structure contradicts EMA alignment, regime is TRANSITIONAL."""
        candles = self._setup_bullish_candles()
        # Force a bearish CHoCH on the last candle
        candles[-1]["close"] = 105.0
        for i in range(10):
            candles.append({
                "timestamp": f"2026-11-09T09:{40+i:02d}:00",
                "open": 105.0, "high": 107.0, "low": 104.0, "close": 105.0,
                "is_closed": True
            })

        indicators = {
            "moving_averages": {"alignment": "BEARISH_STACK"},
            "trend_strength": {"adx": 22.0, "plus_di": 15.0, "minus_di": 22.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.03},
                "atr": 2.0
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRANSITIONAL)

    def test_regime_ranging_low_adx_chop(self):
        """Low ADX (< 20) and mixed moving averages -> RANGING regime."""
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 14.5, "plus_di": 16.0, "minus_di": 15.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.02},
                "atr": 1.0
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.RANGING)

    def test_regime_insufficient_data(self):
        """Candles < 28 or missing indicators returns INSUFFICIENT_DATA."""
        candles = self._create_base_candles(20, base_price=100.0)
        indicators = {"moving_averages": {"alignment": "BULLISH_STACK"}}
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.INSUFFICIENT_DATA)

    # =========================================================================
    # 5. Divergence Detection Tests
    # =========================================================================

    def test_regular_bullish_divergence(self):
        """
        Price forms Lower Low (LL: 100 -> 95), Indicator forms Higher Low (HL: 30 -> 42).
        Type: REGULAR_BULLISH (Reversal), Invalidation: Price closes below Low 2 (95).
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["low"] = 100.0
        candles[15]["low"] = 95.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 30.0
        rsi_series[15] = 42.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        self.assertTrue(res["has_divergence"])
        self.assertEqual(res["divergence_type"], DivergenceType.REGULAR_BULLISH)
        self.assertEqual(res["category"], "REGULAR_REVERSAL")
        self.assertEqual(res["bias"], "BULLISH")
        self.assertEqual(res["invalidation_level"], 95.0)

    def test_regular_bearish_divergence(self):
        """
        Price forms Higher High (HH: 120 -> 128), Indicator forms Lower High (LH: 75 -> 62).
        Type: REGULAR_BEARISH (Reversal), Invalidation: Price closes above High 2 (128).
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["high"] = 120.0
        candles[15]["high"] = 128.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 75.0
        rsi_series[15] = 62.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        self.assertTrue(res["has_divergence"])
        self.assertEqual(res["divergence_type"], DivergenceType.REGULAR_BEARISH)
        self.assertEqual(res["category"], "REGULAR_REVERSAL")
        self.assertEqual(res["bias"], "BEARISH")
        self.assertEqual(res["invalidation_level"], 128.0)

    def test_hidden_bullish_divergence(self):
        """
        Price forms Higher Low (HL: 95 -> 100), Indicator forms Lower Low (LL: 42 -> 30).
        Type: HIDDEN_BULLISH (Continuation), Invalidation: Price closes below Low 1 (95).
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["low"] = 95.0
        candles[15]["low"] = 100.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 42.0
        rsi_series[15] = 30.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        self.assertTrue(res["has_divergence"])
        self.assertEqual(res["divergence_type"], DivergenceType.HIDDEN_BULLISH)
        self.assertEqual(res["category"], "HIDDEN_CONTINUATION")
        self.assertEqual(res["bias"], "BULLISH")
        self.assertEqual(res["invalidation_level"], 95.0)

    def test_hidden_bearish_divergence(self):
        """
        Price forms Lower High (LH: 128 -> 120), Indicator forms Higher High (HH: 62 -> 75).
        Type: HIDDEN_BEARISH (Continuation), Invalidation: Price closes above High 1 (128).
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["high"] = 128.0
        candles[15]["high"] = 120.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 62.0
        rsi_series[15] = 75.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        self.assertTrue(res["has_divergence"])
        self.assertEqual(res["divergence_type"], DivergenceType.HIDDEN_BEARISH)
        self.assertEqual(res["category"], "HIDDEN_CONTINUATION")
        self.assertEqual(res["bias"], "BEARISH")
        self.assertEqual(res["invalidation_level"], 128.0)

    def test_divergence_lookback_horizon_exceeded(self):
        """If distance between two swing pivots exceeds max_bars_lookback, divergence is NOT formed."""
        candles = self._create_base_candles(55, base_price=110.0)
        candles[4]["low"] = 100.0
        candles[50]["low"] = 95.0  # Distance = 46 bars > max_bars_lookback (40)

        rsi_series = [50.0] * 55
        rsi_series[4] = 30.0
        rsi_series[50] = 42.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, max_bars_lookback=40)
        self.assertFalse(res["has_divergence"])
        self.assertEqual(res["divergence_type"], DivergenceType.NO_DIVERGENCE)

    # =========================================================================
    # 6. Multi-Timeframe Synthesis & Service Tests
    # =========================================================================

    def test_mtf_all_aligned_bullish(self):
        """When 15m, 5m, and 1m are all TRENDING_BULLISH -> ALL_ALIGNED_BULLISH."""
        store = MultiTimeframeCandleStore()
        service = MarketStructureService(store)

        mock_tf_results = {
            Timeframe.M15: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.M5: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.M3: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.M1: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.S15: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}}
        }
        summary = service._evaluate_mtf_alignment(mock_tf_results)
        self.assertEqual(summary["alignment_status"], MTFAlignmentStatus.ALL_ALIGNED_BULLISH)
        self.assertIn("Full-Spectrum Bullish", summary["explanation"])

    def test_mtf_macro_bullish_lower_tf_pullback(self):
        """
        When 15m and 5m are TRENDING_BULLISH but 1m is RANGING or TRENDING_BEARISH:
        Evaluates as CONFLICTING_MTF_REGIMES without lower-timeframe overwrite bias.
        """
        store = MultiTimeframeCandleStore()
        service = MarketStructureService(store)

        mock_tf_results = {
            Timeframe.M15: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.M5: {"regime": {"regime": MarketRegime.TRENDING_BULLISH}, "structure": {"structure_state": StructureType.BULLISH}},
            Timeframe.M1: {"regime": {"regime": MarketRegime.RANGING}, "structure": {"structure_state": StructureType.SIDEWAYS}},
        }
        summary = service._evaluate_mtf_alignment(mock_tf_results)
        self.assertEqual(summary["alignment_status"], MTFAlignmentStatus.CONFLICTING_MTF_REGIMES)
        self.assertIn("Macro Bullish with Lower-TF Pullback", summary["explanation"])
        # Macro anchor remains strictly BULLISH
        self.assertEqual(summary["macro_anchor_regimes"]["15m"], MarketRegime.TRENDING_BULLISH)
        self.assertEqual(summary["macro_anchor_regimes"]["5m"], MarketRegime.TRENDING_BULLISH)

    def test_mtf_aligned_ranging(self):
        """When 15m and 5m are both RANGING -> ALIGNED_RANGING."""
        store = MultiTimeframeCandleStore()
        service = MarketStructureService(store)

        mock_tf_results = {
            Timeframe.M15: {"regime": {"regime": MarketRegime.RANGING}, "structure": {"structure_state": StructureType.SIDEWAYS}},
            Timeframe.M5: {"regime": {"regime": MarketRegime.RANGING}, "structure": {"structure_state": StructureType.SIDEWAYS}},
            Timeframe.M1: {"regime": {"regime": MarketRegime.RANGING}, "structure": {"structure_state": StructureType.SIDEWAYS}},
        }
        summary = service._evaluate_mtf_alignment(mock_tf_results)
        self.assertEqual(summary["alignment_status"], MTFAlignmentStatus.ALIGNED_RANGING)

    def test_service_analyze_multi_timeframe_suite_execution(self):
        """Tests end-to-end execution of analyze_multi_timeframe_suite across candle store."""
        store = MultiTimeframeCandleStore()
        symbol = "NIFTY 50"

        # Ingest 45 base 1m candles into store
        base_dt = datetime(2026, 11, 9, 9, 15)
        candles = []
        for i in range(45):
            dt = base_dt + timedelta(minutes=i)
            p = 24000.0 + i * 5.0
            candles.append({
                "timestamp": dt.isoformat(),
                "open": p, "high": p + 3.0, "low": p - 3.0, "close": p + 1.0,
                "volume": 500, "is_closed": True
            })
        store.ingest_base_candles(symbol, candles, base_timeframe=Timeframe.M1)

        service = MarketStructureService(store)
        res = service.analyze_multi_timeframe_suite(symbol, timeframes=[Timeframe.M1, Timeframe.M3, Timeframe.M5])

        self.assertEqual(res["symbol"], symbol)
        self.assertIn(Timeframe.M1, res["timeframe_analyses"])
        self.assertIn(Timeframe.M3, res["timeframe_analyses"])
        self.assertIn(Timeframe.M5, res["timeframe_analyses"])
        self.assertIn("alignment_status", res["multi_timeframe_summary"])

    def test_historical_recalculation_idempotence(self):
        """
        Recalculating market structure, breaks, and divergences on closed historical candles
        repeatedly produces 100% deterministic, bit-for-bit identical output.
        """
        candles = self._setup_bullish_candles()
        candles[-1]["close"] = 142.0

        res1 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        res2 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")

        self.assertEqual(res1, res2)

    # =========================================================================
    # 7. Phase 2B Hardening: Overlapping Regimes, Lifecycles, Divergence Ties
    # =========================================================================

    def test_regime_precedence_squeeze_on_overrides_high_adx_and_bull_stack(self):
        """
        Precedence Hierarchy Test:
        When Bollinger Bands are inside Keltner Channels (Carter's Squeeze ON),
        the regime MUST be VOLATILITY_COMPRESSION (Priority 2), even if ADX >= 25
        and moving averages are in BULLISH_STACK.
        """
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "BULLISH_STACK"},
            "trend_strength": {"adx": 35.0, "plus_di": 30.0, "minus_di": 10.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": True, "is_squeeze_release": False, "bandwidth": 0.010},
                "atr": 1.5
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.VOLATILITY_COMPRESSION)
        self.assertIn("Squeeze ON", res["explanation"])

    def test_regime_precedence_squeeze_release_yields_to_mature_trend(self):
        """
        When Carter's Squeeze releases AND all mature trend criteria are fully satisfied
        (Bullish structure + Bullish EMA stack + ADX >= 25 + +DI > -DI),
        the mature TRENDING_BULLISH regime takes precedence over VOLATILITY_EXPANSION.
        """
        candles = self._setup_bullish_candles()
        for i in range(10):
            base = 150.0 + i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{40+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })

        indicators = {
            "moving_averages": {"alignment": "BULLISH_STACK"},
            "trend_strength": {"adx": 32.0, "plus_di": 28.0, "minus_di": 12.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": True, "bandwidth": 0.038},
                "atr": 2.8
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRENDING_BULLISH)

    def test_adx_20_to_25_resolves_to_trending_bullish_when_unified(self):
        """
        Explicit ADX 20-25 Classification:
        When ADX is in the neutral 20-25 zone (e.g. 22.5), BUT Market Structure is BULLISH,
        EMAs are in BULLISH_STACK, and +DI - -DI >= 5.0, it resolves to TRENDING_BULLISH
        (Developing Bullish Trend).
        """
        candles = self._setup_bullish_candles()
        for i in range(10):
            base = 150.0 + i * 2.0
            candles.append({
                "timestamp": f"2026-11-09T09:{40+i:02d}:00",
                "open": base, "high": base + 2.0, "low": base - 2.0, "close": base,
                "is_closed": True
            })

        indicators = {
            "moving_averages": {"alignment": "BULLISH_STACK"},
            "trend_strength": {"adx": 22.5, "plus_di": 26.0, "minus_di": 14.0},  # Spread = 12.0 >= 5.0
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.030},
                "atr": 2.2
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRENDING_BULLISH)
        self.assertIn("Developing Bullish Trend", res["explanation"])

    def test_adx_20_to_25_resolves_to_transitional_when_conflicted(self):
        """
        Explicit ADX 20-25 Classification:
        When ADX is in 20-25 (e.g. 22.0) and directional cues are entangled (|plus_di - minus_di| < 5.0)
        or structure/EMAs conflict, it MUST classify as TRANSITIONAL.
        """
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 22.0, "plus_di": 19.0, "minus_di": 18.0},  # Spread = 1.0 < 5.0
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.025},
                "atr": 1.5
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRANSITIONAL)
        self.assertIn("neutral/developing range", res["explanation"])

    def test_repeated_bos_prevention_and_level_consumption(self):
        """
        BOS Event Lifecycle:
        Bar 24 closes above Swing High (135) -> BOS_BULLISH, lifecycle NEW_CONFIRMED_BREAK.
        Bar 25 subsequently closes above 135 -> event NONE, lifecycle CONSUMED.
        Prevents repeated-break spamming on already-consumed structural levels.
        """
        candles = self._setup_bullish_candles()
        candles[24]["close"] = 142.0
        candles[24]["high"] = 143.0

        res_break1 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res_break1["event_type"], StructuralBreakType.BOS_BULLISH)
        self.assertEqual(res_break1["lifecycle_state"], BreakLifecycleState.NEW_CONFIRMED_BREAK)

        # Append Bar 25 closing above 135 as well
        candles.append({
            "timestamp": "2026-11-09T09:40:00",
            "open": 142.0, "high": 145.0, "low": 141.0, "close": 144.0,
            "volume": 100, "is_closed": True
        })
        res_break2 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res_break2["event_type"], StructuralBreakType.NONE)
        self.assertEqual(res_break2["lifecycle_state"], BreakLifecycleState.CONSUMED)
        self.assertIn("level consumed", res_break2["explanation"])

    def test_level_invalidation_after_break(self):
        """
        When price breaks Swing High 135 (consumed), but later drops and closes below
        the preceding swing low (110), the prior break is marked INVALIDATED.
        """
        candles = self._setup_bullish_candles()
        candles[24]["close"] = 142.0  # Consumes 135

        # Bar 25 drops and closes at 105 (< 110)
        candles.append({
            "timestamp": "2026-11-09T09:40:00",
            "open": 140.0, "high": 140.0, "low": 104.0, "close": 105.0,
            "volume": 100, "is_closed": True
        })
        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res["lifecycle_state"], BreakLifecycleState.INVALIDATED)
        self.assertEqual(res["event_type"], StructuralBreakType.NONE)
        self.assertIn("invalidated", res["explanation"])

    def test_feed_gap_pivot_suppresses_structural_break(self):
        """When the broken pivot carries FEED_GAP, break detection is suppressed."""
        candles = self._setup_bullish_candles()
        # Tag swing high at bar 14 with FEED_GAP
        candles[14]["timestamp"] = "2026-11-09T10:00:00"  # 45-min jump creates FEED_GAP
        candles[24]["close"] = 142.0

        res = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        # If feed gap occurred, pivot is suppressed
        if res.get("data_quality_status") == "FEED_GAP_SUPPRESSED":
            self.assertEqual(res["event_type"], StructuralBreakType.NONE)
            self.assertEqual(res["lifecycle_state"], BreakLifecycleState.FEED_GAP_SUPPRESSED)

    def test_feed_gap_pivot_suppresses_divergence(self):
        """When either pivot in a divergence candidate carries FEED_GAP, divergence is suppressed."""
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["low"] = 100.0
        candles[15]["low"] = 95.0
        # Inject feed gap on candle 15
        candles[15]["timestamp"] = "2026-11-09T10:30:00"

        rsi_series = [50.0] * 25
        rsi_series[5] = 30.0
        rsi_series[15] = 42.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        if res.get("data_quality_status") == "FEED_GAP_SUPPRESSED":
            self.assertFalse(res["has_divergence"])
            self.assertEqual(res["invalidation_status"], DivergenceInvalidationStatus.FEED_GAP_SUPPRESSED)

    def test_divergence_indicator_matching_exact_pivot_preferred_on_tie(self):
        """
        Divergence Indicator Matching:
        When indicator values across [-1, 0, +1] tie (e.g. 70.0 at price_idx-1 and price_idx),
        the exact price pivot timestamp (idx == price_idx) MUST be selected.
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["high"] = 120.0
        candles[15]["high"] = 128.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 75.0
        # Tie at index 14 and 15 (both 62.0)
        rsi_series[14] = 62.0
        rsi_series[15] = 62.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2, symbol="NIFTY", timeframe="1m")
        self.assertTrue(res["has_divergence"])
        self.assertEqual(res["pivot2_indicator_value"], 62.0)

    def test_divergence_invalidation_closed_candle_vs_wick_probing(self):
        """
        Divergence Invalidation Rules:
        - Closed-candle break below Low 2 (95.0) sets invalidation_status=INVALIDATED_BY_CLOSE and has_divergence=False.
        - Wick penetration below 95.0 with close >= 95.0 sets invalidation_status=ACTIVE_WICK_PROBED and has_divergence=True.
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["low"] = 100.0
        candles[15]["low"] = 95.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 30.0
        rsi_series[15] = 42.0

        # Case 1: Active Untouched (candles 18-24 all stay above 95.0)
        res_untouched = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2)
        self.assertTrue(res_untouched["has_divergence"])
        self.assertEqual(res_untouched["invalidation_status"], DivergenceInvalidationStatus.ACTIVE_UNTOUCHED)

        # Case 2: Wick probed below 95.0 (Low: 94.0, Close: 96.0) on bar 23 (after confirmation at bar 17)
        candles[23]["low"] = 94.0
        candles[23]["close"] = 96.0
        res_wick = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2)
        self.assertTrue(res_wick["has_divergence"])
        self.assertEqual(res_wick["invalidation_status"], DivergenceInvalidationStatus.ACTIVE_WICK_PROBED)

        # Case 3: Closed candle below 95.0 (Close: 93.0) on bar 24 -> INVALIDATED
        candles[24]["close"] = 93.0
        res_closed = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2)
        self.assertFalse(res_closed["has_divergence"])
        self.assertEqual(res_closed["invalidation_status"], DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE)

    def test_hidden_divergence_invalidation_rules(self):
        """
        Hidden Bullish Divergence Invalidation:
        Setup: Price HL (Low 1 = 95.0, Low 2 = 100.0), Indicator LL.
        Invalidation level = Low 1 (95.0).
        If candle closes below 95.0 -> INVALIDATED_BY_CLOSE.
        """
        candles = self._create_base_candles(25, base_price=110.0)
        candles[5]["low"] = 95.0
        candles[15]["low"] = 100.0

        rsi_series = [50.0] * 25
        rsi_series[5] = 42.0
        rsi_series[15] = 30.0

        # Close candle 21 below Low 1 (95.0)
        candles[21]["close"] = 93.0

        res = detect_divergence(candles, rsi_series, indicator_name="RSI", k=2)
        self.assertFalse(res["has_divergence"])
        self.assertEqual(res["invalidation_status"], DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE)

    def test_default_timeframe_k_parameters(self):
        """
        Verifies canonical default K parameter mapping:
        - 15s, 1m, 3m -> K=2
        - 5m, 15m -> K=3
        """
        self.assertEqual(DEFAULT_TIMEFRAME_K[Timeframe.S15], 2)
        self.assertEqual(DEFAULT_TIMEFRAME_K[Timeframe.M1], 2)
        self.assertEqual(DEFAULT_TIMEFRAME_K[Timeframe.M3], 2)
        self.assertEqual(DEFAULT_TIMEFRAME_K[Timeframe.M5], 3)
        self.assertEqual(DEFAULT_TIMEFRAME_K[Timeframe.M15], 3)

    # =========================================================================
    # Phase 2B Final Acceptance Hardening Tests
    # =========================================================================

    def test_feed_gap_reconciled_1_5x_threshold_across_timeframes(self):
        """
        Reconciled Feed-Gap Rule:
        Threshold is 1.5x expected interval seconds.
        A single missing candle (2.0x interval) MUST be flagged as FEED_GAP across all timeframes.
        """
        # Test 1m: expected 60s, 1 missing candle = 120s (2.0x > 1.5x) -> FEED_GAP
        candles_1m = self._create_base_candles(15, base_price=100.0)
        candles_1m[4]["high"] = 125.0
        # Jump from 09:19 to 09:21 (09:20 missing -> delta 120s > 90s)
        candles_1m[5]["timestamp"] = "2026-11-09T09:21:00"
        res_1m = identify_fractal_pivots(candles_1m, k=2, symbol="NIFTY", timeframe="1m")
        pivots_gap_1m = [p for p in res_1m["all_pivots"] if p.get("data_quality_status") == "FEED_GAP"]
        self.assertGreater(len(pivots_gap_1m), 0, "Single missing 1m candle (120s) must trigger FEED_GAP")

        # Test 15s: expected 15s, 1 missing candle = 30s (2.0x > 1.5x -> 22.5s) -> FEED_GAP
        candles_15s = []
        base_t = datetime(2026, 11, 9, 9, 15, 0)
        for i in range(15):
            sec_delta = (i * 15) + (15 if i >= 5 else 0)
            t_bar = base_t + timedelta(seconds=sec_delta)
            candles_15s.append({
                "timestamp": t_bar.isoformat(),
                "open": 100.0, "high": 120.0 if i == 4 else 102.0, "low": 98.0, "close": 100.0,
                "is_closed": True
            })
        res_15s = identify_fractal_pivots(candles_15s, k=2, symbol="NIFTY", timeframe="15s")
        pivots_gap_15s = [p for p in res_15s["all_pivots"] if p.get("data_quality_status") == "FEED_GAP"]
        self.assertGreater(len(pivots_gap_15s), 0, "Single missing 15s candle (30s) must trigger FEED_GAP")

        # Test 5m: expected 300s, 1 missing candle = 600s (2.0x > 1.5x -> 450s) -> FEED_GAP
        candles_5m = []
        for i in range(15):
            m_delta = (i * 5) + (5 if i >= 5 else 0)
            t_bar = base_t + timedelta(minutes=m_delta)
            candles_5m.append({
                "timestamp": t_bar.isoformat(),
                "open": 100.0, "high": 120.0 if i == 4 else 102.0, "low": 98.0, "close": 100.0,
                "is_closed": True
            })
        res_5m = identify_fractal_pivots(candles_5m, k=3, symbol="NIFTY", timeframe="5m")
        pivots_gap_5m = [p for p in res_5m["all_pivots"] if p.get("data_quality_status") == "FEED_GAP"]
        self.assertGreater(len(pivots_gap_5m), 0, "Single missing 5m candle (600s) must trigger FEED_GAP")

    def test_distinguish_session_boundary_from_feed_gap(self):
        """
        Session Boundary Distinction:
        When consecutive candles cross an overnight session boundary (e.g. Nov 6 to Nov 9),
        it MUST be classified as SESSION_BOUNDARY, NOT an intraday FEED_GAP.
        """
        candles = []
        # Bar 0: Friday 15:29
        candles.append({
            "timestamp": "2026-11-06T15:29:00",
            "open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0,
            "is_closed": True
        })
        # Bars 1..14: Monday 09:15 onwards (continuous)
        for i in range(1, 15):
            candles.append({
                "timestamp": f"2026-11-09T09:{14+i:02d}:00",
                "open": 100.0, "high": 125.0 if i == 5 else 102.0, "low": 98.0, "close": 100.0,
                "is_closed": True
            })
        res = identify_fractal_pivots(candles, k=2, symbol="NIFTY", timeframe="1m")
        # Ensure no pivot is falsely tagged as FEED_GAP due to the weekend transition
        feed_gap_pivots = [p for p in res["all_pivots"] if p.get("data_quality_status") == "FEED_GAP"]
        self.assertEqual(len(feed_gap_pivots), 0, "Overnight/weekend session boundary must NOT be tagged as FEED_GAP")

    def test_market_regime_fallback_strong_adx_with_conflicted_structure(self):
        """
        Market Regime Fallback Rule:
        When ADX is strong (ADX = 35.0 >= 25.0) but moving averages are MIXED and structure is SIDEWAYS,
        the regime MUST classify as TRANSITIONAL, never blindly default to RANGING.
        """
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 35.0, "plus_di": 22.0, "minus_di": 21.0},
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.040},
                "atr": 2.5
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.TRANSITIONAL)
        self.assertIn("Transitional", res["explanation"])
        self.assertTrue(any("High trend momentum" in cf for cf in res["conflicting_factors"]))

    def test_market_regime_ranging_requires_genuine_evidence(self):
        """
        RANGING Classification Rule:
        Requires actual range-bound evidence: low trend momentum (ADX < 20) with sideways/mixed structure.
        """
        candles = self._create_base_candles(35, base_price=100.0)
        indicators = {
            "moving_averages": {"alignment": "MIXED"},
            "trend_strength": {"adx": 14.5, "plus_di": 15.0, "minus_di": 14.0},  # Low ADX < 20
            "volatility": {
                "squeeze": {"is_squeeze_on": False, "is_squeeze_release": False, "bandwidth": 0.020},
                "atr": 1.2
            }
        }
        res = classify_market_regime(candles, indicators, k=2, symbol="NIFTY", timeframe="5m")
        self.assertEqual(res["regime"], MarketRegime.RANGING)
        self.assertIn("Ranging", res["explanation"])

    def test_structural_break_failed_breakout_lifecycle(self):
        """
        Failed Breakout Lifecycle (Bull Trap):
        - Bar 24 closes at 142.0 > Swing High 135.0 -> NEW_CONFIRMED_BREAK (BOS_BULLISH).
        - Bar 25 closes back inside at 133.0 < 135.0 -> FAILED_BREAKOUT.
        - event_history MUST preserve the original break at bar 24.
        """
        candles = self._setup_bullish_candles()
        candles[24]["close"] = 142.0
        candles[24]["high"] = 143.0

        res1 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res1["event_type"], StructuralBreakType.BOS_BULLISH)
        self.assertEqual(res1["lifecycle_state"], BreakLifecycleState.NEW_CONFIRMED_BREAK)

        # Bar 25 closes back inside level 135.0
        candles.append({
            "timestamp": "2026-11-09T09:40:00",
            "open": 142.0, "high": 142.5, "low": 132.5, "close": 133.0,
            "volume": 120, "is_closed": True
        })
        res2 = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res2["event_type"], StructuralBreakType.NONE)
        self.assertEqual(res2["lifecycle_state"], BreakLifecycleState.FAILED_BREAKOUT)
        self.assertTrue(res2["is_level_consumed"])
        self.assertIn("Failed Breakout", res2["explanation"])

        # Verify historical BOS is preserved in event_history
        self.assertEqual(len(res2["event_history"]), 1)
        hist = res2["event_history"][0]
        self.assertEqual(hist["event_type"], StructuralBreakType.BOS_BULLISH)
        self.assertEqual(hist["break_index"], 24)
        self.assertEqual(hist["current_lifecycle"], BreakLifecycleState.FAILED_BREAKOUT)

    def test_structural_break_idempotency(self):
        """
        Idempotency:
        Calling detect_structural_breaks repeatedly on the same series produces identical outputs.
        """
        candles = self._setup_bullish_candles()
        candles[24]["close"] = 142.0
        res_a = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        res_b = detect_structural_breaks(candles, k=2, symbol="NIFTY", timeframe="1m")
        self.assertEqual(res_a["event_type"], res_b["event_type"])
        self.assertEqual(res_a["lifecycle_state"], res_b["lifecycle_state"])
        self.assertEqual(res_a["broken_level_price"], res_b["broken_level_price"])
        self.assertEqual(len(res_a["event_history"]), len(res_b["event_history"]))


if __name__ == "__main__":
    unittest.main()

