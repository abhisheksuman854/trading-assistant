"""
TradeLens AI - Integration Tests: Broker Historical Retrieval & Data Status Verification.
Verifies:
1. Angel One interval normalization and parameter validation.
2. Broker disconnection behavior: returns empty lists and UNAVAILABLE status, never fabricating data.
3. API endpoints and frontend fallback datasets explicitly mark offline data as SIMULATED, never LIVE.
"""
import os
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.data.angel_one import AngelOneSmartAPIProvider
from app.data.base import DataStatus
from app.data.sample_universe import SampleUniverseProvider
from app.main import TradeLensAppRuntime


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

    def test_scan_sentiment_unavailable_when_market_data_empty(self):
        """
        When market data is disconnected or empty (allow_sample_fallback=False),
        get_scan_payload() must report data_status: UNAVAILABLE and
        market_sentiment: UNAVAILABLE, never claiming bullish or bearish sentiment.
        """
        rt = TradeLensAppRuntime(allow_sample_fallback=False)
        payload = rt.get_scan_payload()
        self.assertEqual(payload["data_status"], "UNAVAILABLE")
        self.assertEqual(payload["market_sentiment"], "UNAVAILABLE")
        self.assertEqual(payload["top_movers"], [])
        self.assertEqual(payload["top_swing_setups"], [])
        self.assertEqual(payload["top_options_setups"], [])

    def test_scan_sentiment_fail_closed_cannot_be_overridden_when_data_unavailable(self):
        """
        Even when a caller explicitly passes sentiment='BULLISH' or 'BEARISH'
        to get_scan_payload, if market data is unavailable, market_sentiment must
        strictly fail-closed to 'UNAVAILABLE'.
        """
        rt = TradeLensAppRuntime(allow_sample_fallback=False)
        for requested_sentiment in ("BULLISH", "BEARISH", "NEUTRAL"):
            payload = rt.get_scan_payload(sentiment=requested_sentiment)
            self.assertEqual(payload["data_status"], "UNAVAILABLE")
            self.assertEqual(
                payload["market_sentiment"],
                "UNAVAILABLE",
                f"Market sentiment must fail closed to UNAVAILABLE when data is missing, got {payload['market_sentiment']}",
            )

    def test_scan_sentiment_preserved_when_universe_active(self):
        """
        When active stock data is present (e.g. simulated fallback or live universe),
        get_scan_payload must preserve the requested sentiment in the scan engine.
        """
        rt = TradeLensAppRuntime(allow_sample_fallback=True)
        payload = rt.get_scan_payload(sentiment="BEARISH")
        self.assertEqual(payload["data_status"], "SIMULATED")
        self.assertEqual(payload["market_sentiment"], "BEARISH")


if __name__ == "__main__":
    unittest.main()
