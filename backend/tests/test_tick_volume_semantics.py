"""
TradeLens AI - Unit Tests: Tick Volume Semantics (Task 3).
Verifies:
1. Cumulative session volume handling via valid positive deltas (V_t - V_{t-1}).
2. Prevention of multi-million candle volume on baseline initialization.
3. Duplicate tick rejection without volume inflation.
4. Out-of-order tick rejection.
5. Volume counter reset / reconnect detection.
6. Incremental volume aggregation.
7. 15s candle construction under both Cumulative and Incremental volume semantics.
"""
import unittest
from datetime import datetime, timedelta
from app.data.candle_store import (
    MultiTimeframeCandleStore,
    VolumeSemantics,
    Timeframe,
    build_15s_candles_from_ticks
)
from app.data.session_clock import IST_ZONE


class TestTickVolumeSemantics(unittest.TestCase):

    def test_cumulative_volume_positive_deltas_and_baseline(self):
        """
        When connected to a broker quote stream reporting cumulative traded volume:
        - First tick sets the baseline without attributing the entire session volume to the current bar.
        - Subsequent ticks advance volume by positive delta (V_curr - V_prev).
        """
        store = MultiTimeframeCandleStore(volume_semantics=VolumeSemantics.CUMULATIVE)
        sym = "NIFTY 50"
        t0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE)

        # Tick 1: Broker reports cumulative volume 1,500,000
        store.on_tick(sym, price=24800.0, volume=1_500_000, timestamp=t0)
        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 0, "First tick in cumulative feed must establish baseline (delta=0)")

        # Tick 2: 50 contracts traded -> cumulative volume 1,500,050
        store.on_tick(sym, price=24805.0, volume=1_500_050, timestamp=t0 + timedelta(seconds=10))
        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 50, "Second tick should add delta of 50")

        # Tick 3: 75 contracts traded -> cumulative volume 1,500,125
        store.on_tick(sym, price=24810.0, volume=1_500_125, timestamp=t0 + timedelta(seconds=20))
        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 125, "Cumulative delta should sum to 125 (50 + 75)")

    def test_cumulative_volume_duplicate_tick_rejection(self):
        """
        Duplicate ticks with identical timestamp and volume (e.g. quote refresh with no trade)
        must contribute 0 volume.
        """
        store = MultiTimeframeCandleStore(volume_semantics=VolumeSemantics.CUMULATIVE)
        sym = "NIFTY 50"
        t0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE)

        store.on_tick(sym, price=24800.0, volume=5000, timestamp=t0)
        store.on_tick(sym, price=24802.0, volume=5030, timestamp=t0 + timedelta(seconds=5))
        # Duplicate tick with same volume 5030
        store.on_tick(sym, price=24802.0, volume=5030, timestamp=t0 + timedelta(seconds=6))
        store.on_tick(sym, price=24802.5, volume=5030, timestamp=t0 + timedelta(seconds=7))

        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 30, "Duplicate ticks must contribute 0 additional volume")

    def test_cumulative_volume_out_of_order_tick_rejection(self):
        """
        Out-of-order packets (arriving with an older timestamp) must be rejected
        without subtracting volume or corrupting the volume baseline.
        """
        store = MultiTimeframeCandleStore(volume_semantics=VolumeSemantics.CUMULATIVE)
        sym = "NIFTY 50"
        t0 = datetime(2026, 11, 9, 10, 0, 10, tzinfo=IST_ZONE)

        store.on_tick(sym, price=24800.0, volume=1000, timestamp=t0)
        store.on_tick(sym, price=24805.0, volume=1050, timestamp=t0 + timedelta(seconds=5))  # vol=50

        # Out-of-order tick with timestamp t0 + 2s (older than t0 + 5s)
        store.on_tick(sym, price=24802.0, volume=1020, timestamp=t0 + timedelta(seconds=2))

        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 50, "Out-of-order tick must not alter candle volume")

        # Next valid tick
        store.on_tick(sym, price=24810.0, volume=1080, timestamp=t0 + timedelta(seconds=8))
        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 80, "Volume should progress from 1050 to 1080 (+30 -> 80 total)")

    def test_cumulative_volume_counter_reset_and_reconnect(self):
        """
        If the broker reconnects or resets the session volume counter (e.g. from 50,000 down to 10),
        the system must recognize the reset and re-establish the baseline without negative volume.
        """
        store = MultiTimeframeCandleStore(volume_semantics=VolumeSemantics.CUMULATIVE)
        sym = "NIFTY 50"
        t0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE)

        store.on_tick(sym, price=24800.0, volume=10000, timestamp=t0)
        store.on_tick(sym, price=24805.0, volume=10040, timestamp=t0 + timedelta(seconds=5))  # +40
        self.assertEqual(store.get_active_candle(sym, Timeframe.M1)["volume"], 40)

        # Counter reset / reconnect: volume drops from 10040 to 15
        store.on_tick(sym, price=24806.0, volume=15, timestamp=t0 + timedelta(seconds=10))
        self.assertEqual(store.get_active_candle(sym, Timeframe.M1)["volume"], 40, "Reset tick must not decrease volume")

        # Subsequent tick in new counter stream: 15 -> 35 (+20)
        store.on_tick(sym, price=24808.0, volume=35, timestamp=t0 + timedelta(seconds=15))
        self.assertEqual(store.get_active_candle(sym, Timeframe.M1)["volume"], 60, "Volume should resume adding deltas: 40 + 20 = 60")

    def test_incremental_volume_semantics(self):
        """
        Under incremental volume semantics, every tick represents trade size and is summed directly.
        Negative trade volumes are clamped to 0.
        """
        store = MultiTimeframeCandleStore(volume_semantics=VolumeSemantics.INCREMENTAL)
        sym = "NIFTY 50"
        t0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=IST_ZONE)

        store.on_tick(sym, price=24800.0, volume=100, timestamp=t0)
        store.on_tick(sym, price=24805.0, volume=150, timestamp=t0 + timedelta(seconds=5))
        store.on_tick(sym, price=24810.0, volume=-20, timestamp=t0 + timedelta(seconds=10))  # invalid negative clamped to 0
        store.on_tick(sym, price=24815.0, volume=50, timestamp=t0 + timedelta(seconds=15))

        active = store.get_active_candle(sym, Timeframe.M1)
        self.assertEqual(active["volume"], 300, "100 + 150 + 0 + 50 = 300")

    def test_build_15s_candles_with_cumulative_volume(self):
        """
        Verifies build_15s_candles_from_ticks with cumulative volume semantics.
        Deltas are calculated across sorted ticks before aggregating into 15s bins.
        """
        ticks = [
            {"timestamp": "2026-11-09T09:15:02+05:30", "price": 24800.0, "volume": 1000},
            {"timestamp": "2026-11-09T09:15:05+05:30", "price": 24805.0, "volume": 1025},  # +25
            {"timestamp": "2026-11-09T09:15:10+05:30", "price": 24802.0, "volume": 1050},  # +25
            # End of first 15s interval (09:15:00 - 09:15:15) -> total vol = 50

            # Second 15s interval (09:15:15 - 09:15:30)
            {"timestamp": "2026-11-09T09:15:18+05:30", "price": 24810.0, "volume": 1080},  # +30
            {"timestamp": "2026-11-09T09:15:25+05:30", "price": 24815.0, "volume": 1120},  # +40
            # Total vol in second bucket = 70
        ]
        candles = build_15s_candles_from_ticks(ticks, symbol="NIFTY 50", volume_semantics=VolumeSemantics.CUMULATIVE)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["volume"], 50)
        self.assertEqual(candles[1]["volume"], 70)


if __name__ == "__main__":
    unittest.main()

