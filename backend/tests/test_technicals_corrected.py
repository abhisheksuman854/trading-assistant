"""
TradeLens AI - Phase 0 Test Suite: Technical Indicator Calculations & Precision.
Validates EMA floating-point precision, RSI Wilder's smoothing & warmup,
MACD confirmed vs developing crossover separation, and Volume/RVOL handling.
"""
import unittest
import math
from app.core.technicals import (
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    get_rsi_warmup_status,
    calculate_macd,
    calculate_atr,
    evaluate_technicals
)


class TestTechnicalsCorrected(unittest.TestCase):

    def test_ema_floating_point_precision_no_intermediate_rounding(self):
        """
        Verifies EMA maintains full 64-bit IEEE float precision without intermediate loop rounding.
        Compares against exact mathematical recurrence.
        """
        prices = [100.0 + i * 0.3333333333333333 for i in range(50)]
        period = 9
        multiplier = 2.0 / (period + 1.0)
        
        # Calculate analytical EMA manually using double precision
        first_sma = sum(prices[:period]) / period
        expected_ema_last = first_sma
        for p in prices[period:]:
            expected_ema_last = (p - expected_ema_last) * multiplier + expected_ema_last
            
        ema_result_raw = calculate_ema(prices, period, round_output=False)
        self.assertLess(abs(ema_result_raw[-1] - expected_ema_last), 1e-12, "EMA should maintain full IEEE float precision")
        
        # Output rounded for display
        ema_result_rounded = calculate_ema(prices, period, round_output=True)
        self.assertEqual(ema_result_rounded[-1], round(expected_ema_last, 2))

    def test_ema_constant_price_initialization(self):
        """Constant price series must produce exact constant EMA with zero drift."""
        prices = [24850.50] * 35
        ema = calculate_ema(prices, 20)
        self.assertEqual(ema[:19], [None] * 19)
        self.assertEqual(ema[19], 24850.50)
        self.assertEqual(ema[-1], 24850.50)

    def test_rsi_wilders_smoothing_flat_rising_falling(self):
        """
        Verifies Wilder's RMA formula on canonical test price series:
        1. Flat series -> 50.0
        2. Monotonically rising -> 100.0
        3. Monotonically falling -> 0.0
        """
        # 1. Flat series (both gain and loss are 0)
        flat_prices = [100.0] * 30
        rsi_flat = calculate_rsi(flat_prices, 14)
        self.assertEqual(rsi_flat[-1], 50.0, "Flat series must evaluate to neutral 50.0 RSI")
        
        # 2. Rising series (zero loss)
        rising_prices = [100.0 + i * 2.0 for i in range(30)]
        rsi_rising = calculate_rsi(rising_prices, 14)
        self.assertEqual(rsi_rising[-1], 100.0, "Purely rising series must evaluate to 100.0 RSI")
        
        # 3. Falling series (zero gain)
        falling_prices = [200.0 - i * 2.0 for i in range(30)]
        rsi_falling = calculate_rsi(falling_prices, 14)
        self.assertEqual(rsi_falling[-1], 0.0, "Purely falling series must evaluate to 0.0 RSI")

    def test_rsi_insufficient_data_and_warmup_status(self):
        """Verifies that RSI returns None when bars <= period and properly exposes warmup status."""
        prices_short = [100.0, 101.0, 102.0]
        rsi_short = calculate_rsi(prices_short, 14)
        self.assertEqual(rsi_short, [None, None, None])
        
        status_short = get_rsi_warmup_status(len(prices_short), 14)
        self.assertEqual(status_short["status"], "INSUFFICIENT_DATA")
        self.assertFalse(status_short["is_warmup_complete"])
        
        # 25 bars: enough to compute initial RSI, but partial Wilder warmup (< 3*N = 42)
        status_partial = get_rsi_warmup_status(25, 14)
        self.assertEqual(status_partial["status"], "PARTIAL_WARMUP")
        self.assertFalse(status_partial["is_warmup_complete"])
        
        # 50 bars: full warmup complete
        status_complete = get_rsi_warmup_status(50, 14)
        self.assertEqual(status_complete["status"], "WARMUP_COMPLETE")
        self.assertTrue(status_complete["is_warmup_complete"])

    def test_macd_confirmed_vs_developing_crossover_separation(self):
        """
        Verifies that a crossover on the active unclosed candle [-1] is marked strictly
        as DEVELOPING, and NEVER as CONFIRMED.
        """
        # Create price series where macd histogram turns positive on the very last bar
        prices = [100.0 - i * 0.5 for i in range(35)]
        macd_prev = calculate_macd(prices)
        self.assertLessEqual(macd_prev["hist"], 0)
        
        # Add one bar that causes developing cross on the active candle
        prices_developing = prices + [105.0]
        macd_dev = calculate_macd(prices_developing)
        
        # Must be DEVELOPING, NOT confirmed!
        self.assertTrue(macd_dev["is_developing_bullish_cross"])
        self.assertFalse(macd_dev["is_confirmed_bullish_cross"])
        self.assertEqual(macd_dev["crossover_status"], "DEVELOPING_BULLISH")
        
        # Append another confirmed bar maintaining the trend
        prices_confirmed = prices_developing + [106.0]
        macd_conf = calculate_macd(prices_confirmed)
        self.assertTrue(macd_conf["is_confirmed_bullish_cross"])
        self.assertEqual(macd_conf["crossover_status"], "CONFIRMED_BULLISH")

    def test_volume_and_rvol_handling_for_spot_index(self):
        """Spot indices (NIFTY 50) have 0 direct volume."""
        index_candles = [
            {"open": 24800, "high": 24850, "low": 24780, "close": 24820, "volume": 0}
            for _ in range(30)
        ]
        res = evaluate_technicals(index_candles)
        self.assertIsNone(res["rvol"])
        self.assertEqual(res["rvol_status"], "NOT_APPLICABLE_FOR_SPOT_INDEX")


if __name__ == "__main__":
    unittest.main()
