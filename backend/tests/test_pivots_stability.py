"""
TradeLens AI - Phase 0 Test Suite: Support & Resistance Pivot Stability.
Proves that pivot values remain invariant and stable throughout the current session
because they are derived strictly from completed prior session/bar data.
"""
import unittest
from app.core.technicals import calculate_pivots, evaluate_technicals


class TestPivotsStability(unittest.TestCase):

    def test_classic_pivot_formulas(self):
        """Verifies classic floor pivot mathematical formulas."""
        high, low, close = 25000.0, 24800.0, 24900.0
        p = (high + low + close) / 3.0 # 24900.0
        r1 = 2 * p - low               # 25000.0
        s1 = 2 * p - high              # 24800.0
        r2 = p + (high - low)          # 25100.0
        s2 = p - (high - low)          # 24700.0
        
        pivots = calculate_pivots(high, low, close)
        self.assertEqual(pivots["pivot"], 24900.0)
        self.assertEqual(pivots["r1"], 25000.0)
        self.assertEqual(pivots["s1"], 24800.0)
        self.assertEqual(pivots["r2"], 25100.0)
        self.assertEqual(pivots["s2"], 24700.0)

    def test_pivots_stability_during_intraday_tick_movements(self):
        """
        Simulates a live session where candle [-1] (the active candle) moves wildly
        (high increases, low drops, close changes).
        Proves that calculated pivots remain 100% IDENTICAL and stable.
        """
        historical_candles = [
            {"open": 24800.0, "high": 24850.0, "low": 24780.0, "close": 24820.0, "volume": 1000}
            for _ in range(28)
        ]
        # Bar [-2] is yesterday's completed bar: High=25000, Low=24800, Close=24900
        prior_completed_bar = {"open": 24820.0, "high": 25000.0, "low": 24800.0, "close": 24900.0, "volume": 2000}
        
        # Tick 1: Live candle starts at 24910
        active_candle_tick1 = {"open": 24900.0, "high": 24920.0, "low": 24890.0, "close": 24910.0, "volume": 50}
        candles_tick1 = historical_candles + [prior_completed_bar, active_candle_tick1]
        res1 = evaluate_technicals(candles_tick1)
        
        # Tick 2: Live candle surges upward to 25150
        active_candle_tick2 = {"open": 24900.0, "high": 25150.0, "low": 24890.0, "close": 25140.0, "volume": 350}
        candles_tick2 = historical_candles + [prior_completed_bar, active_candle_tick2]
        res2 = evaluate_technicals(candles_tick2)
        
        # Tick 3: Live candle dumps to 24750
        active_candle_tick3 = {"open": 24900.0, "high": 25150.0, "low": 24750.0, "close": 24760.0, "volume": 800}
        candles_tick3 = historical_candles + [prior_completed_bar, active_candle_tick3]
        res3 = evaluate_technicals(candles_tick3)
        
        # Verify that pivot levels did NOT move by even 1 paisa despite wild intra-bar price action!
        self.assertEqual(res1["pivots"]["pivot"], 24900.0)
        self.assertEqual(res2["pivots"]["pivot"], 24900.0)
        self.assertEqual(res3["pivots"]["pivot"], 24900.0)
        
        self.assertEqual(res1["pivots"]["r1"], 25000.0)
        self.assertEqual(res2["pivots"]["r1"], 25000.0)
        self.assertEqual(res3["pivots"]["r1"], 25000.0)
        
        self.assertEqual(res1["pivots"]["s1"], 24800.0)
        self.assertEqual(res2["pivots"]["s1"], 24800.0)
        self.assertEqual(res3["pivots"]["s1"], 24800.0)

    def test_explicit_prior_session_ohlc_parameter(self):
        """Verifies that an explicitly provided prior session OHLC takes precedence."""
        prior_session = {"high": 25200.0, "low": 24900.0, "close": 25100.0}
        candles = [
            {"open": 25000.0, "high": 25050.0, "low": 24980.0, "close": 25020.0, "volume": 1000},
            {"open": 25020.0, "high": 25080.0, "low": 25010.0, "close": 25060.0, "volume": 1500}
        ]
        res = evaluate_technicals(candles, prior_session_ohlc=prior_session)
        expected_pivot = round((25200.0 + 24900.0 + 25100.0) / 3.0, 2)
        self.assertEqual(res["pivots"]["pivot"], expected_pivot)
        self.assertEqual(res["pivot_source"], "PRIOR_SESSION_OHLC")


if __name__ == "__main__":
    unittest.main()
