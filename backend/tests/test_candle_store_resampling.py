"""
TradeLens AI - Unit Tests: Multi-Timeframe Candle Resampling.
Proves that OHLCV aggregation mathematically preserves:
Open = first, High = max, Low = min, Close = last, Volume = sum,
and aligns strictly with the 09:15:00 IST session opening clock.
"""
import unittest
from datetime import datetime
from app.data.candle_store import (
    Timeframe,
    resample_candles,
    get_candle_interval_bounds,
    MultiTimeframeCandleStore
)
from app.data.session_clock import IST_ZONE


class TestCandleStoreResampling(unittest.TestCase):

    def test_interval_bounds_alignment_with_session_open(self):
        """Verifies interval start/end times at 09:15:00 IST session start."""
        t_open = datetime(2026, 11, 9, 9, 15, 0, tzinfo=IST_ZONE)

        # 15s
        s15_start, s15_end = get_candle_interval_bounds(t_open, Timeframe.S15)
        self.assertEqual(s15_start.second, 0)
        self.assertEqual(s15_end.second, 15)

        # 1m
        m1_start, m1_end = get_candle_interval_bounds(t_open, Timeframe.M1)
        self.assertEqual(m1_start.minute, 15)
        self.assertEqual(m1_end.minute, 16)

        # 3m (09:15 to 09:18)
        m3_start, m3_end = get_candle_interval_bounds(t_open, Timeframe.M3)
        self.assertEqual(m3_start.minute, 15)
        self.assertEqual(m3_end.minute, 18)

        # 5m (09:15 to 09:20)
        m5_start, m5_end = get_candle_interval_bounds(t_open, Timeframe.M5)
        self.assertEqual(m5_start.minute, 15)
        self.assertEqual(m5_end.minute, 20)

        # 15m (09:15 to 09:30)
        m15_start, m15_end = get_candle_interval_bounds(t_open, Timeframe.M15)
        self.assertEqual(m15_start.minute, 15)
        self.assertEqual(m15_end.minute, 30)

    def test_resample_1m_to_3m_mathematical_preservation(self):
        """
        Three 1m bars from 09:15 to 09:17 must resample to one 3m bar with:
        Open=Bar1.Open, High=max(Highs), Low=min(Lows), Close=Bar3.Close, Vol=sum(Vols).
        """
        source_1m = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24800.0, "high": 24830.0, "low": 24790.0, "close": 24820.0, "volume": 1000},
            {"timestamp": "2026-11-09T09:16:00+05:30", "open": 24820.0, "high": 24850.0, "low": 24810.0, "close": 24840.0, "volume": 1500},
            {"timestamp": "2026-11-09T09:17:00+05:30", "open": 24840.0, "high": 24860.0, "low": 24830.0, "close": 24855.0, "volume": 2000}
        ]

        resampled_3m = resample_candles(source_1m, Timeframe.M3, symbol="NIFTY 50")
        self.assertEqual(len(resampled_3m), 1)

        c3m = resampled_3m[0]
        self.assertEqual(c3m["open"], 24800.0)    # Open of 1st bar
        self.assertEqual(c3m["high"], 24860.0)    # Max of (24830, 24850, 24860)
        self.assertEqual(c3m["low"], 24790.0)     # Min of (24790, 24810, 24830)
        self.assertEqual(c3m["close"], 24855.0)   # Close of 3rd bar
        self.assertEqual(c3m["volume"], 4500)     # Sum of (1000 + 1500 + 2000)
        self.assertEqual(c3m["timeframe"], "3m")
        self.assertEqual(c3m["start_time"], "2026-11-09T09:15:00+05:30")
        self.assertEqual(c3m["end_time"], "2026-11-09T09:18:00+05:30")

    def test_resample_1m_to_5m_and_15m(self):
        """
        15 one-minute bars from 09:15 to 09:29 must produce:
        - Exactly three 5m bars
        - Exactly one 15m bar
        """
        source_15m = [
            {
                "timestamp": f"2026-11-09T09:{15+i:02d}:00+05:30",
                "open": 24800.0 + i * 2,
                "high": 24810.0 + i * 2,
                "low": 24795.0 + i * 2,
                "close": 24805.0 + i * 2,
                "volume": 1000
            }
            for i in range(15)
        ]

        # Resample to 5m
        res_5m = resample_candles(source_15m, Timeframe.M5, symbol="NIFTY 50")
        self.assertEqual(len(res_5m), 3)
        self.assertEqual(res_5m[0]["volume"], 5000)
        self.assertEqual(res_5m[1]["volume"], 5000)
        self.assertEqual(res_5m[2]["volume"], 5000)

        # Resample to 15m
        res_15m = resample_candles(source_15m, Timeframe.M15, symbol="NIFTY 50")
        self.assertEqual(len(res_15m), 1)
        self.assertEqual(res_15m[0]["open"], source_15m[0]["open"])
        self.assertEqual(res_15m[0]["close"], source_15m[-1]["close"])
        self.assertEqual(res_15m[0]["volume"], 15000)


if __name__ == "__main__":
    unittest.main()
