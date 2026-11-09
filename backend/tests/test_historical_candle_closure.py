"""
TradeLens AI - Unit Tests: Historical Candle Closure & Deduplication (Task 4).
Verifies:
1. determine_candle_closure correctly flags completed vs active forming intervals based on exchange time.
2. Angel One historical candle ingestion sets is_closed=False and DataStatus.LIVE on incomplete forming bars during market hours.
3. Angel One historical candle ingestion sets is_closed=True and DataStatus.HISTORICAL on all bars after market close.
4. CandleStore.ingest_base_candles never places is_closed=False bars into _closed_candles.
5. Overlap and duplicate timestamp prevention in CandleStore.
"""
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

from app.data.candle_store import (
    determine_candle_closure,
    Timeframe,
    MultiTimeframeCandleStore
)
from app.data.base import DataStatus
from app.data.angel_one import AngelOneSmartAPIProvider
from app.data.session_clock import IST_ZONE


class TestHistoricalCandleClosure(unittest.TestCase):

    def test_determine_candle_closure_during_market_hours(self):
        """
        At 10:02:30 IST:
        - 5-minute candle for 09:55:00 (ends 10:00:00) is CLOSED.
        - 5-minute candle for 10:00:00 (ends 10:05:00) is ACTIVE (is_closed=False).
        """
        simulated_now = datetime(2026, 11, 9, 10, 2, 30, tzinfo=IST_ZONE)

        # 09:55 bar: completed
        bar_0955 = datetime(2026, 11, 9, 9, 55, 0, tzinfo=IST_ZONE)
        is_closed_0955, end_0955 = determine_candle_closure(bar_0955, Timeframe.M5, current_exchange_time=simulated_now)
        self.assertTrue(is_closed_0955)
        self.assertEqual(end_0955, datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE))

        # 10:00 bar: still developing
        bar_1000 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE)
        is_closed_1000, end_1000 = determine_candle_closure(bar_1000, Timeframe.M5, current_exchange_time=simulated_now)
        self.assertFalse(is_closed_1000)
        self.assertEqual(end_1000, datetime(2026, 11, 9, 10, 5, 0, tzinfo=IST_ZONE))

    def test_determine_candle_closure_after_market_close(self):
        """
        At 16:05:00 IST (after session close):
        All intraday candles from today are marked is_closed=True.
        """
        simulated_post_market = datetime(2026, 11, 9, 16, 5, 0, tzinfo=IST_ZONE)
        bar_1525 = datetime(2026, 11, 9, 15, 25, 0, tzinfo=IST_ZONE)
        is_closed, _ = determine_candle_closure(bar_1525, Timeframe.M5, current_exchange_time=simulated_post_market)
        self.assertTrue(is_closed)

    @patch("urllib.request.urlopen")
    def test_angel_one_historical_candles_segregates_live_forming_bar(self, mock_urlopen):
        """
        When Angel One getCandleData returns the current forming bar during active session:
        - Completed bars have is_closed=True, candle_status=HISTORICAL.
        - The latest uncompleted bar has is_closed=False, candle_status=LIVE.
        """
        provider = AngelOneSmartAPIProvider(api_key="mock", client_code="mock", password="mock", totp_secret="mock")
        provider.jwt_token = "mock_jwt"
        provider.is_authenticated = True

        # Mock response: 3 5-min candles returned at 10:03 IST
        mock_resp_data = {
            "status": True,
            "data": [
                ["2026-11-09T09:50:00+05:30", 24800, 24820, 24790, 24810, 15000],
                ["2026-11-09T09:55:00+05:30", 24810, 24830, 24805, 24825, 18000],
                ["2026-11-09T10:00:00+05:30", 24825, 24840, 24820, 24835, 6000]   # Developing bar!
            ]
        }
        mock_response = MagicMock()
        mock_response.read.return_value = str(mock_resp_data).replace("'", '"').replace("True", "true").encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        simulated_now = datetime(2026, 11, 9, 10, 3, 0, tzinfo=IST_ZONE)
        candles = provider.fetch_historical_candles("NSE", "99926000", "5m", current_exchange_time=simulated_now)

        self.assertEqual(len(candles), 3)
        # Bar 1 & 2: completed
        self.assertTrue(candles[0]["is_closed"])
        self.assertEqual(candles[0]["candle_status"], DataStatus.HISTORICAL)
        self.assertTrue(candles[1]["is_closed"])
        self.assertEqual(candles[1]["candle_status"], DataStatus.HISTORICAL)

        # Bar 3: developing bar
        self.assertFalse(candles[2]["is_closed"])
        self.assertEqual(candles[2]["candle_status"], DataStatus.LIVE)

    def test_candle_store_ingest_base_candles_never_saves_active_bar_to_closed_store(self):
        """
        When ingesting candles containing an unclosed forming bar:
        - Closed bars enter _closed_candles.
        - The unclosed bar is set as _active_candle and NEVER appears in get_closed_candles.
        """
        store = MultiTimeframeCandleStore()
        sym = "NIFTY 50"

        mixed_candles = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24800, "high": 24810, "low": 24795, "close": 24805, "volume": 1000, "is_closed": True},
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 24805, "high": 24820, "low": 24800, "close": 24815, "volume": 1200, "is_closed": True},
            {"timestamp": "2026-11-09T09:17:00+05:30", "open": 24815, "high": 24825, "low": 24810, "close": 24820, "volume": 500, "is_closed": False}  # Active!
        ]

        store.ingest_base_candles(sym, mixed_candles, base_timeframe=Timeframe.M1)

        closed = store.get_closed_candles(sym, Timeframe.M1)
        self.assertEqual(len(closed), 2, "Only the 2 closed bars must be in closed store")
        self.assertTrue(all(c["is_closed"] for c in closed))

        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertIsNotNone(active)
        self.assertFalse(active["is_closed"])
        self.assertEqual(active["close"], 24820.0)

    def test_candle_store_overlap_and_duplicate_timestamp_prevention(self):
        """
        Ingesting overlapping batches of historical candles must not produce duplicate bars.
        The candle store must deduplicate by start_time and maintain chronological order.
        """
        store = MultiTimeframeCandleStore()
        sym = "NIFTY 50"

        batch_1 = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24800, "high": 24810, "low": 24795, "close": 24805, "volume": 1000, "is_closed": True},
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 24805, "high": 24820, "low": 24800, "close": 24815, "volume": 1200, "is_closed": True}
        ]
        batch_2 = [
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 24805, "high": 24820, "low": 24800, "close": 24815, "volume": 1200, "is_closed": True},  # Duplicate
            {"timestamp": "2026-11-09T09:17:00+05:30", "open": 24815, "high": 24830, "low": 24810, "close": 24825, "volume": 1400, "is_closed": True}
        ]

        store.ingest_base_candles(sym, batch_1, base_timeframe=Timeframe.M1)
        store.ingest_base_candles(sym, batch_2, base_timeframe=Timeframe.M1)

        closed = store.get_closed_candles(sym, Timeframe.M1)
        self.assertEqual(len(closed), 3, "Duplicate bar for 09:16 must be merged, resulting in exactly 3 bars")
        timestamps = [c["start_time"] for c in closed]
        self.assertEqual(len(timestamps), len(set(timestamps)), "All timestamps in closed store must be strictly unique")


if __name__ == "__main__":
    unittest.main()

