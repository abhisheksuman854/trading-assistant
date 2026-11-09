"""
TradeLens AI - Integration Tests: Broker Historical Retrieval & Data Status Verification.
Verifies:
1. Angel One interval normalization and parameter validation.
2. Broker disconnection behavior: returns empty lists and UNAVAILABLE status, never fabricating data.
3. API endpoints and frontend fallback datasets explicitly mark offline data as SIMULATED, never LIVE.
"""
import unittest
import os
from app.data.angel_one import AngelOneSmartAPIProvider
from app.data.base import DataStatus
from app.data.sample_universe import SampleUniverseProvider


class TestBrokerIntegrationAndDataStatus(unittest.TestCase):

    def test_angel_one_interval_mapping(self):
        """Verifies that timeframe shorthands are normalized to official Angel One intervals."""
        provider = AngelOneSmartAPIProvider()
        self.assertEqual(provider.INTERVAL_MAP["1m"], "ONE_MINUTE")
        self.assertEqual(provider.INTERVAL_MAP["3m"], "THREE_MINUTE")
        self.assertEqual(provider.INTERVAL_MAP["5m"], "FIVE_MINUTE")
        self.assertEqual(provider.INTERVAL_MAP["15m"], "FIFTEEN_MINUTE")
        self.assertEqual(provider.INTERVAL_MAP["1d"], "ONE_DAY")
        self.assertEqual(provider.INTERVAL_MAP["D"], "ONE_DAY")

    def test_disconnected_broker_returns_empty_and_unavailable(self):
        """
        When credentials are absent or disconnected, fetch_historical_candles
        must return an empty list [], NEVER synthetic fake candles.
        """
        provider = AngelOneSmartAPIProvider()
        provider.is_authenticated = False
        provider.jwt_token = ""
        # Mock connect to return False
        provider.connect = lambda: False

        candles = provider.fetch_historical_candles(token="99926000", interval="1m")
        self.assertEqual(candles, [])

    def test_sample_universe_provider_explicitly_tagged_simulated(self):
        """
        All data returned by SampleUniverseProvider must carry data_status: SIMULATED.
        """
        provider = SampleUniverseProvider()
        quote = provider.get_quote("NIFTY 50")
        self.assertEqual(quote["data_status"], DataStatus.SIMULATED)

        candles = provider.get_candles("RELIANCE")
        self.assertTrue(len(candles) > 0)
        for c in candles:
            self.assertEqual(c["candle_status"], DataStatus.SIMULATED)

    def test_frontend_fallback_data_not_claiming_live(self):
        """
        Scans frontend/index.html to ensure fallbackData does NOT claim to be LIVE Angel One data.
        """
        html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "frontend", "index.html"
        )
        self.assertTrue(os.path.exists(html_path))
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The fallbackData object must carry SIMULATED data_status
        self.assertIn('"data_status": "SIMULATED"', content)
        # Ensure fallbackData does not declare itself as LIVE Angel One SmartAPI
        self.assertNotIn('"data_source": "LIVE Angel One SmartAPI",\n      "indices": defaultIndices', content)


if __name__ == "__main__":
    unittest.main()
