"""
TradeLens AI - Phase 0 Test Suite: Market Data Integrity & Fallback Prevention.
Verifies that:
1. AngelOneSmartAPIProvider does not silently generate synthetic candles.
2. DataStatus is explicitly marked (LIVE, HISTORICAL, SIMULATED, UNAVAILABLE).
3. Last update timestamps are present across quotes and indices.
4. Spot index volume is marked NOT_APPLICABLE_FOR_SPOT_INDEX.
"""
import unittest
from app.data.base import DataStatus
from app.data.angel_one import AngelOneSmartAPIProvider
from app.data.sample_universe import SampleUniverseProvider


class TestDataIntegrity(unittest.TestCase):

    def test_angel_one_provider_does_not_fabricate_synthetic_candles(self):
        """
        When Angel One is not connected or returns no candles, it must return
        an empty list with DataStatus.UNAVAILABLE, rather than silently calling generate_candle_series.
        """
        provider = AngelOneSmartAPIProvider()
        # Unauthenticated instance
        candles = provider.get_candles("RELIANCE", limit=60)
        self.assertIsInstance(candles, list)
        # Must NOT contain fabricated candles
        self.assertEqual(len(candles), 0, "Unauthenticated broker provider must return empty list, not synthetic candles")

    def test_data_status_and_timestamps_in_quotes(self):
        """Quotes must provide data_status and last_updated_at."""
        sample_provider = SampleUniverseProvider()
        quote = sample_provider.get_quote("RELIANCE")
        self.assertIn("data_status", quote)
        self.assertEqual(quote["data_status"], DataStatus.SIMULATED)
        self.assertIn("last_updated_at", quote)
        self.assertIn("volume_status", quote)

    def test_spot_index_volume_marked_not_applicable(self):
        """Spot indices do not have direct traded volume; this must be explicitly labeled."""
        sample_provider = SampleUniverseProvider()
        indices = sample_provider.get_indices()
        for idx in indices:
            self.assertEqual(idx["volume_status"], "NOT_APPLICABLE_FOR_SPOT_INDEX")
            self.assertEqual(idx["data_status"], DataStatus.SIMULATED)
            self.assertIn("last_updated_at", idx)


if __name__ == "__main__":
    unittest.main()
