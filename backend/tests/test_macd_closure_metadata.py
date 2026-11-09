"""
TradeLens AI - Unit Tests: MACD Explicit Candle Closure Metadata.
Proves that MACD crossover confirmation is governed by explicit candle closure
metadata (is_closed, start_time, end_time, timestamp) rather than naive array positions.
"""
import unittest
from app.core.technicals import calculate_macd


class TestMacdClosureMetadata(unittest.TestCase):

    def test_crossover_when_last_bar_is_closed(self):
        """
        When candles represent completed historical bars (e.g. from historical candle API),
        last_bar_is_closed=True. A crossover on index [-1] MUST be immediately evaluated as CONFIRMED.
        """
        # Price series turning positive on the last bar
        prices = [100.0 - i * 0.5 for i in range(35)] + [106.0]
        candles_metadata = [
            {"timestamp": f"2026-11-09T09:{i:02d}:00+05:30", "is_closed": True}
            for i in range(len(prices))
        ]

        # Call with explicit metadata where last candle is closed
        result = calculate_macd(prices, candles_metadata=candles_metadata)

        self.assertTrue(result["last_bar_is_closed"])
        self.assertTrue(result["is_confirmed_bullish_cross"])
        self.assertFalse(result["is_developing_bullish_cross"])
        self.assertEqual(result["crossover_status"], "CONFIRMED_BULLISH")
        self.assertEqual(result["confirmed_candle_index"], len(prices) - 1)
        self.assertEqual(result["confirmed_candle_timestamp"], candles_metadata[-1]["timestamp"])

    def test_crossover_when_last_bar_is_actively_forming(self):
        """
        When streaming live ticks into an open candle, last_bar_is_closed=False.
        A crossover occurring on index [-1] MUST be marked DEVELOPING, NOT confirmed!
        """
        prices = [100.0 - i * 0.5 for i in range(35)] + [106.0]
        candles_metadata = [
            {"timestamp": f"2026-11-09T09:{i:02d}:00+05:30", "is_closed": True}
            for i in range(len(prices) - 1)
        ] + [
            # Last candle is active / forming
            {"timestamp": "2026-11-09T09:35:00+05:30", "is_closed": False}
        ]

        result = calculate_macd(prices, candles_metadata=candles_metadata)

        self.assertFalse(result["last_bar_is_closed"])
        self.assertFalse(result["is_confirmed_bullish_cross"])
        self.assertTrue(result["is_developing_bullish_cross"])
        self.assertEqual(result["crossover_status"], "DEVELOPING_BULLISH")

    def test_confirmed_crossover_on_penultimate_bar_when_live_bar_present(self):
        """
        When an unclosed live candle [-1] exists, a crossover that occurred on the
        last closed bar [-2] relative to [-3] MUST be tagged as CONFIRMED.
        """
        # Bar [-2] crossed over, Bar [-1] is an active follow-through bar
        prices = [100.0 - i * 0.5 for i in range(35)] + [106.0, 107.0]
        candles_metadata = [
            {"timestamp": f"2026-11-09T09:{i:02d}:00+05:30", "is_closed": True}
            for i in range(len(prices) - 1)
        ] + [
            {"timestamp": "2026-11-09T09:36:00+05:30", "is_closed": False}
        ]

        result = calculate_macd(prices, candles_metadata=candles_metadata)

        self.assertFalse(result["last_bar_is_closed"])
        self.assertTrue(result["is_confirmed_bullish_cross"])
        self.assertEqual(result["crossover_status"], "CONFIRMED_BULLISH")
        self.assertEqual(result["confirmed_candle_index"], len(prices) - 2)
        self.assertEqual(result["confirmed_candle_timestamp"], candles_metadata[-2]["timestamp"])


if __name__ == "__main__":
    unittest.main()

