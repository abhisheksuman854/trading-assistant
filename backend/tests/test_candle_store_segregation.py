"""
TradeLens AI - Unit Tests: Candle Store Closed vs Developing Segregation & Non-Repainting.
Proves that:
1. Closed candles in MultiTimeframeCandleStore are immutable and never mutate on new ticks (Zero Repainting).
2. Live streaming ticks update only active_candle.
3. Interval expiration seals the active candle, moves it to closed_candles, and starts a fresh active candle.
4. 15s candles are constructed from timestamped live ticks.
"""
import unittest
from datetime import datetime, timedelta
from app.data.candle_store import (
    Timeframe,
    MultiTimeframeCandleStore,
    build_15s_candles_from_ticks
)
from app.data.session_clock import IST_ZONE


class TestCandleStoreSegregation(unittest.TestCase):

    def test_closed_candles_immutability_zero_repainting(self):
        """
        Proves that once a candle is closed, subsequent ticks NEVER mutate it.
        Signals calculated on closed candles are 100% frozen.
        """
        store = MultiTimeframeCandleStore()
        sym = "NIFTY 50"

        # Minute 1: 09:15:00 to 09:15:59
        t1 = datetime(2026, 11, 9, 9, 15, 10, tzinfo=IST_ZONE)
        store.on_tick(sym, price=24800.0, volume=100, timestamp=t1)
        store.on_tick(sym, price=24820.0, volume=150, timestamp=t1 + timedelta(seconds=20))
        store.on_tick(sym, price=24790.0, volume=200, timestamp=t1 + timedelta(seconds=40))

        # At this point, 09:15 candle is ACTIVE, closed list is empty
        self.assertEqual(len(store.get_closed_candles(sym, Timeframe.M1)), 0)
        active_m1 = store.get_active_candle(sym, Timeframe.M1)
        self.assertIsNotNone(active_m1)
        self.assertEqual(active_m1["open"], 24800.0)
        self.assertEqual(active_m1["high"], 24820.0)
        self.assertEqual(active_m1["low"], 24790.0)
        self.assertEqual(active_m1["close"], 24790.0)
        self.assertEqual(active_m1["volume"], 450)
        self.assertFalse(active_m1["is_closed"])

        # Minute 2: 09:16:05 (crosses into new minute!)
        t2 = datetime(2026, 11, 9, 9, 16, 5, tzinfo=IST_ZONE)
        store.on_tick(sym, price=24810.0, volume=300, timestamp=t2)

        # Minute 1 candle is now CLOSED and frozen!
        closed_m1 = store.get_closed_candles(sym, Timeframe.M1)
        self.assertEqual(len(closed_m1), 1)
        bar1 = closed_m1[0]
        self.assertTrue(bar1["is_closed"])
        self.assertEqual(bar1["open"], 24800.0)
        self.assertEqual(bar1["high"], 24820.0)
        self.assertEqual(bar1["low"], 24790.0)
        self.assertEqual(bar1["close"], 24790.0)
        self.assertEqual(bar1["volume"], 450)

        # Minute 2 candle is now ACTIVE
        active_m2 = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active_m2["open"], 24810.0)

        # Wild price moves during minute 2 do NOT mutate closed bar 1!
        store.on_tick(sym, price=25100.0, volume=5000, timestamp=t2 + timedelta(seconds=15))
        store.on_tick(sym, price=24500.0, volume=6000, timestamp=t2 + timedelta(seconds=30))

        frozen_bar1 = store.get_closed_candles(sym, Timeframe.M1)[0]
        self.assertEqual(frozen_bar1["high"], 24820.0)  # NOT mutated by 25100!
        self.assertEqual(frozen_bar1["low"], 24790.0)   # NOT mutated by 24500!
        self.assertEqual(frozen_bar1["volume"], 450)    # NOT mutated by 11000 new volume!

    def test_build_15s_candles_from_ticks(self):
        """
        Tests building 15-second candles from timestamped live tick data.
        Ticks spanning 09:15:02 to 09:15:28 should produce:
        - One 15s candle from 09:15:00 to 09:15:15
        - One 15s candle from 09:15:15 to 09:15:30
        """
        ticks = [
            {"timestamp": "2026-11-09T09:15:02+05:30", "price": 24800.0, "volume": 50},
            {"timestamp": "2026-11-09T09:15:08+05:30", "price": 24815.0, "volume": 120},
            {"timestamp": "2026-11-09T09:15:14+05:30", "price": 24805.0, "volume": 80},
            # Second 15s bracket (09:15:15 to 09:15:30)
            {"timestamp": "2026-11-09T09:15:17+05:30", "price": 24810.0, "volume": 150},
            {"timestamp": "2026-11-09T09:15:25+05:30", "price": 24825.0, "volume": 200}
        ]

        candles_15s = build_15s_candles_from_ticks(ticks, symbol="NIFTY 50")
        self.assertEqual(len(candles_15s), 2)

        # First 15s candle
        c1 = candles_15s[0]
        self.assertEqual(c1["open"], 24800.0)
        self.assertEqual(c1["high"], 24815.0)
        self.assertEqual(c1["low"], 24800.0)
        self.assertEqual(c1["close"], 24805.0)
        self.assertEqual(c1["volume"], 250)
        self.assertEqual(c1["start_time"], "2026-11-09T09:15:00+05:30")
        self.assertEqual(c1["end_time"], "2026-11-09T09:15:15+05:30")

        # Second 15s candle
        c2 = candles_15s[1]
        self.assertEqual(c2["open"], 24810.0)
        self.assertEqual(c2["high"], 24825.0)
        self.assertEqual(c2["low"], 24810.0)
        self.assertEqual(c2["close"], 24825.0)
        self.assertEqual(c2["volume"], 350)
        self.assertEqual(c2["start_time"], "2026-11-09T09:15:15+05:30")
        self.assertEqual(c2["end_time"], "2026-11-09T09:15:30+05:30")

    def test_ingest_base_candles_populates_higher_timeframes(self):
        """
        Ingesting fifteen 1m candles into store must automatically populate:
        - 15 closed 1m candles
        - 5 closed 3m candles
        - 3 closed 5m candles
        - 1 closed 15m candle
        """
        store = MultiTimeframeCandleStore()
        sym = "NIFTY 50"

        base_1m = [
            {
                "timestamp": f"2026-11-09T09:{15+i:02d}:00+05:30",
                "open": 24800.0 + i,
                "high": 24805.0 + i,
                "low": 24798.0 + i,
                "close": 24802.0 + i,
                "volume": 1000
            }
            for i in range(15)
        ]

        store.ingest_base_candles(sym, base_1m, base_timeframe=Timeframe.M1)

        self.assertEqual(len(store.get_closed_candles(sym, Timeframe.M1)), 15)
        self.assertEqual(len(store.get_closed_candles(sym, Timeframe.M3)), 5)
        self.assertEqual(len(store.get_closed_candles(sym, Timeframe.M5)), 3)
        self.assertEqual(len(store.get_closed_candles(sym, Timeframe.M15)), 1)


if __name__ == "__main__":
    unittest.main()

