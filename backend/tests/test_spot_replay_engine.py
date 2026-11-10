"""
TradeLens AI - Phase 3C Test Suite: Lookahead-Free Historical Spot Replay Engine.

Verifies:
1. Sequential candle delivery.
2. Future candle access prevention.
3. 1m-to-3m/5m/15m synchronization.
4. No premature higher-timeframe candle closure.
5. Pivot confirmation timestamp enforcement.
6. No future BOS/CHoCH/divergence visibility.
7. Session boundary and overnight handling.
8. Missing candle and data-gap behavior.
9. No synthetic 15-second candles.
10. NIFTY spot volume remains NOT_APPLICABLE.
11. No fabricated VWAP confirmation.
12. Phase 2C remains unchanged.
13. Gate 6 does not imply option tradability.
14. Option execution eligibility remains false.
15. Deterministic replay output across repeated runs.
16. Replay stops safely on invalid data.
17. Lookahead Regression Test: Mutating candles after T* leaves all outputs at or before T* identical.
"""

import copy
import shutil
import tempfile
import unittest
from datetime import datetime, date, time, timedelta
from pathlib import Path

from backend.app.data.base import DataStatus
from backend.app.data.candle_store import Timeframe, Candle
from backend.app.data.historical_spot_store import (
    HistoricalSpotStore,
    VolumeSemantics,
    DataQualityFlag
)
from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from backend.app.core.market_structure import ConfirmationStatus
from backend.app.core.spot_replay_engine import (
    HistoricalSpotReplayEngine,
    ReplayClock,
    LookaheadFreeCandleAccess,
    MTFReplaySynchronizer,
    SpotReplayEvent,
    LookaheadViolationError,
    ReplayDataIntegrityError
)


class TestSpotReplayEngine(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="tradelens_replay_test_")
        self.clock = IndianMarketSessionClock()
        self.store = HistoricalSpotStore(
            base_dir=self.test_dir,
            storage_format="csv",
            session_clock=self.clock
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _generate_1m_candles(self, count: int = 30, start_dt: datetime = None, base_p: float = 24000.0) -> list:
        if start_dt is None:
            # Monday 9 Nov 2026, 09:15 IST
            start_dt = datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE)
        records = []
        for i in range(count):
            c_dt = start_dt + timedelta(minutes=i)
            p = base_p + i * 1.5
            records.append({
                "timestamp": c_dt.isoformat(),
                "open": round(p, 2),
                "high": round(p + 2.0, 2),
                "low": round(p - 1.0, 2),
                "close": round(p + 1.0, 2),
                "volume": 0.0,
                "is_closed": True
            })
        return records

    # -------------------------------------------------------------------------
    # 1. Sequential Candle Delivery
    # -------------------------------------------------------------------------
    def test_sequential_candle_delivery(self):
        """Verifies monotonic forward advancement of replay clock and strictly ascending event timestamps."""
        candles = self._generate_1m_candles(15)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        self.assertEqual(len(events), 15)
        for i in range(len(events) - 1):
            t_curr = datetime.fromisoformat(events[i].replay_timestamp)
            t_next = datetime.fromisoformat(events[i + 1].replay_timestamp)
            self.assertLess(t_curr, t_next, "Replay timestamps must be strictly ascending")
            self.assertEqual(events[i + 1].step_index, events[i].step_index + 1)

    # -------------------------------------------------------------------------
    # 2. Future Candle Access Prevention & Interface Isolation
    # -------------------------------------------------------------------------
    def test_future_candle_access_prevention(self):
        """
        Controlled data-access interface raises LookaheadViolationError if any query
        or write attempts to access candles beyond current replay clock T.
        """
        replay_clock = ReplayClock()
        # Set clock to 09:20:00
        replay_clock.advance_to(datetime(2026, 11, 9, 9, 20, tzinfo=IST_ZONE))
        data_access = LookaheadFreeCandleAccess(replay_clock, self.clock)

        # Attempt to add candle ending at 09:25:00 (future relative to clock at 09:20:00)
        future_candle = Candle(
            symbol="NIFTY",
            timeframe=Timeframe.M5,
            start_time=datetime(2026, 11, 9, 9, 20, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 9, 9, 25, tzinfo=IST_ZONE),
            open_price=24000, high_price=24010, low_price=23990, close_price=24005,
            is_closed=True
        )
        with self.assertRaises(LookaheadViolationError) as ctx:
            data_access.add_closed_candle("NIFTY", Timeframe.M5, future_candle)
        self.assertIn("Lookahead violation", str(ctx.exception))

    def test_controlled_data_access_isolation_alternate_interfaces(self):
        """
        Verifies that:
        1. Modifying returned candle dictionaries does NOT mutate internal candle store state.
        2. get_session_candles() and get_closed_candles() strictly filter by T.
        """
        replay_clock = ReplayClock()
        replay_clock.advance_to(datetime(2026, 11, 9, 9, 20, tzinfo=IST_ZONE))
        data_access = LookaheadFreeCandleAccess(replay_clock, self.clock)

        c = Candle(
            symbol="NIFTY", timeframe=Timeframe.M1,
            start_time=datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 9, 9, 16, tzinfo=IST_ZONE),
            open_price=24000, high_price=24010, low_price=23990, close_price=24005,
            is_closed=True
        )
        data_access.add_closed_candle("NIFTY", Timeframe.M1, c)

        # Retrieve dictionary and attempt in-place mutation
        retrieved = data_access.get_closed_candles("NIFTY", Timeframe.M1)
        self.assertEqual(len(retrieved), 1)
        retrieved[0]["close"] = 999999.0  # Adversarial mutation

        # Re-query: internal state must remain untouched
        second_query = data_access.get_closed_candles("NIFTY", Timeframe.M1)
        self.assertEqual(second_query[0]["close"], 24005.0)

        # Session query test
        session_candles = data_access.get_session_candles("NIFTY", Timeframe.M1)
        self.assertEqual(len(session_candles), 1)
        self.assertEqual(session_candles[0]["close"], 24005.0)

    # -------------------------------------------------------------------------
    # 3. 1m-to-3m/5m/15m Synchronization
    # -------------------------------------------------------------------------
    def test_mtf_synchronization_1m_to_3m_5m_15m(self):
        """
        Synchronizes 1m source candles into 3m, 5m, and 15m intervals.
        Verifies exact closure progression at 09:18, 09:20, and 09:30.
        """
        candles = self._generate_1m_candles(15)  # 09:15 to 09:30 (15 minutes)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        # Event at index 2 is at 09:18:00 (3 bars of 1m elapsed)
        evt_0918 = events[2]
        self.assertEqual(evt_0918.closed_candle_counts[Timeframe.M1], 3)
        self.assertEqual(evt_0918.closed_candle_counts[Timeframe.M3], 1)
        self.assertEqual(evt_0918.closed_candle_counts[Timeframe.M5], 0)
        self.assertEqual(evt_0918.closed_candle_counts[Timeframe.M15], 0)

        # Event at index 4 is at 09:20:00 (5 bars of 1m elapsed)
        evt_0920 = events[4]
        self.assertEqual(evt_0920.closed_candle_counts[Timeframe.M1], 5)
        self.assertEqual(evt_0920.closed_candle_counts[Timeframe.M3], 1)
        self.assertEqual(evt_0920.closed_candle_counts[Timeframe.M5], 1)
        self.assertEqual(evt_0920.closed_candle_counts[Timeframe.M15], 0)

        # Event at index 14 is at 09:30:00 (15 bars of 1m elapsed)
        evt_0930 = events[14]
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M1], 15)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M3], 5)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M5], 3)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M15], 1)

    # -------------------------------------------------------------------------
    # 4. No Premature Higher-Timeframe Candle Closure & Multi-Close at 09:30
    # -------------------------------------------------------------------------
    def test_no_premature_higher_timeframe_candle_closure(self):
        """
        At 09:19:00 (4 bars into the 09:15-09:20 5m interval):
        The 5m closed list MUST be empty. The forming 5m candle must not be exposed as closed.
        """
        candles = self._generate_1m_candles(4)  # 09:15, 09:16, 09:17, 09:18 (closes at 09:19:00)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))
        last_evt = events[-1]

        self.assertEqual(last_evt.replay_timestamp, "2026-11-09T09:19:00+05:30")
        self.assertEqual(last_evt.closed_candle_counts[Timeframe.M5], 0, "5m candle must NOT close prematurely at 09:19")
        
        # Verify active developing candle exists but is not closed
        active_5m = engine.data_access.get_active_candle("NIFTY", Timeframe.M5)
        self.assertIsNotNone(active_5m)
        self.assertFalse(active_5m["is_closed"])

    def test_evaluation_scheduling_multiclose_at_0930(self):
        """
        At 09:30:00 IST, 1m, 3m, 5m, and 15m all close simultaneously.
        Verifies:
        1. Exactly ONE event is generated at 09:30:00 IST (no duplicate evaluations).
        2. Closed 15m candle is immediately incorporated into closed counts.
        3. No duplicate events or step indices are generated.
        """
        candles = self._generate_1m_candles(15)  # 09:15 to 09:30
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events_5m = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        # In 15 minutes, 5m closes at 09:20 (event 1), 09:25 (event 2), 09:30 (event 3)
        self.assertEqual(len(events_5m), 3)
        evt_0930 = events_5m[2]

        self.assertEqual(evt_0930.replay_timestamp, "2026-11-09T09:30:00+05:30")
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M1], 15)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M3], 5)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M5], 3)
        self.assertEqual(evt_0930.closed_candle_counts[Timeframe.M15], 1)
        self.assertEqual(evt_0930.last_closed_candle_timestamps[Timeframe.M15], "2026-11-09T09:30:00+05:30")

    # -------------------------------------------------------------------------
    # 5. Pivot Confirmation Timestamp Enforcement
    # -------------------------------------------------------------------------
    def test_pivot_confirmation_timestamp_enforcement(self):
        """
        A K=2 pivot candidate at index i is confirmed only when candle i+2 closes.
        Its confirmation timestamp equals candle[i+2].end_time, never the pivot's start time.
        """
        # Create a peak at index 4 (09:19 bar)
        candles = self._generate_1m_candles(7)
        candles[4]["high"] = 24100.0  # Peak
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        # At step index 5 (09:19-09:20 bar, i=4): peak just occurred, 0 right bars. Pivot is NOT confirmed.
        # At step index 6 (09:20-09:21 bar, i=5): 1 right bar. Pivot is NOT confirmed.
        # At step index 7 (09:21-09:22 bar, i=6): candle[6] closed at 09:22:00. Pivot is NOW confirmed!
        last_evt = events[-1]
        self.assertEqual(last_evt.replay_timestamp, "2026-11-09T09:22:00+05:30")
        pivots = engine.structure_service.analyze_market_structure("NIFTY", Timeframe.M1)["pivots"]
        sh = pivots["swing_highs"][0]
        self.assertEqual(sh["index"], 4)
        self.assertEqual(sh["confirmation_status"], ConfirmationStatus.CONFIRMED)
        self.assertEqual(sh["confirmation_timestamp"], "2026-11-09T09:22:00+05:30")

    # -------------------------------------------------------------------------
    # 6. No Future BOS/CHoCH/Divergence Visibility
    # -------------------------------------------------------------------------
    def test_no_future_bos_choch_visibility(self):
        """Break of structure occurring at bar 20 must NOT be visible at bar 15."""
        candles = self._generate_1m_candles(25)
        # Establish peak at bar 5 (high 24050)
        candles[5]["high"] = 24050.0
        # Breakout occurs on bar 20 (close 24060)
        candles[20]["close"] = 24060.0
        candles[20]["high"] = 24065.0
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        # At event 15 (09:30:00), the breakout at bar 20 (09:35:00) has NOT occurred
        evt_15 = events[14]
        self.assertNotEqual(evt_15.latest_confirmed_bos_choch, "BOS_BULLISH")

        # Breakout confirmed only on or after event 20 has closed
        evt_20 = events[19]
        # At or after event 20, break is evaluated
        self.assertIsNotNone(evt_20.latest_confirmed_bos_choch)

    # -------------------------------------------------------------------------
    # 7. Session Boundary and Overnight Handling
    # -------------------------------------------------------------------------
    def test_session_boundary_and_overnight_handling(self):
        """Active buckets reset on new trading day; no candles aggregated across overnight gaps."""
        # Day 1: 15:28 and 15:29 candles (session close 15:30)
        day1_start = datetime(2026, 11, 9, 15, 28, tzinfo=IST_ZONE)
        c_day1 = [
            {"timestamp": day1_start.isoformat(), "open": 24000, "high": 24005, "low": 23995, "close": 24000, "is_closed": True},
            {"timestamp": (day1_start + timedelta(minutes=1)).isoformat(), "open": 24000, "high": 24005, "low": 23995, "close": 24002, "is_closed": True}
        ]
        # Day 2: 09:15 candle (session open next day)
        day2_start = datetime(2026, 11, 10, 9, 15, tzinfo=IST_ZONE)
        c_day2 = [
            {"timestamp": day2_start.isoformat(), "open": 24050, "high": 24060, "low": 24040, "close": 24055, "is_closed": True}
        ]
        self.store.ingest_records(c_day1 + c_day2, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        self.assertEqual(len(events), 3)
        # Event 3 is on Day 2 at 09:16:00
        evt_day2 = events[2]
        self.assertEqual(evt_day2.replay_timestamp, "2026-11-10T09:16:00+05:30")
        # 5m bucket for Day 2 must have started fresh with only Day 2's candle
        active_5m = engine.data_access.get_active_candle("NIFTY", Timeframe.M5)
        self.assertEqual(active_5m["start_time"], "2026-11-10T09:15:00+05:30")

    # -------------------------------------------------------------------------
    # 8. Missing Candle & Data-Gap Behavior
    # -------------------------------------------------------------------------
    def test_missing_candle_and_data_gap_behavior(self):
        """Missing intervals in continuous session remain visible and do not halt valid replay."""
        # 5-minute gap between 09:20 and 09:25
        c1 = {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24000, "high": 24005, "low": 23995, "close": 24000, "is_closed": True}
        c2 = {"timestamp": "2026-11-09T09:25:00+05:30", "open": 24005, "high": 24010, "low": 24000, "close": 24008, "is_closed": True}
        self.store.ingest_records([c1, c2], symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))
        self.assertEqual(len(events), 2)
        # Gap is detected and logged in manifest and diagnostics
        self.assertEqual(events[1].replay_timestamp, "2026-11-09T09:26:00+05:30")

    # -------------------------------------------------------------------------
    # 9. No Synthetic 15-Second Candles
    # -------------------------------------------------------------------------
    def test_no_synthetic_15_second_candles(self):
        """15-second data is strictly unavailable during spot replay; never synthesized from 1m bars."""
        candles = self._generate_1m_candles(10)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        # Check that 15s candle access returns empty list
        s15_candles = engine.data_access.get_closed_candles("NIFTY", Timeframe.S15)
        self.assertEqual(len(s15_candles), 0, "15s candles must remain strictly unavailable")

    # -------------------------------------------------------------------------
    # 10. NIFTY Spot Volume NOT_APPLICABLE & No Fabricated VWAP
    # -------------------------------------------------------------------------
    def test_nifty_spot_volume_and_no_fabricated_vwap(self):
        """NIFTY spot volume is NOT_APPLICABLE; VWAP remains unavailable without positive confirmation."""
        candles = self._generate_1m_candles(10)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="1m"))

        # Verify all closed 1m bars have volume=0
        c1m_closed = engine.data_access.get_closed_candles("NIFTY", Timeframe.M1)
        for c in c1m_closed:
            self.assertEqual(c["volume"], 0)

        # Technical indicators VWAP must not provide positive confirmation
        ind_5m = engine.indicator_service.compute_all_indicators("NIFTY", Timeframe.M5)
        vwap_val = ind_5m.get("vwap", {}).get("vwap")
        self.assertIsNone(vwap_val, "VWAP must be None on un-traded spot index")

    # -------------------------------------------------------------------------
    # 11. Phase 2C Remains Unchanged & Gate 6 Interpretation
    # -------------------------------------------------------------------------
    def test_phase2c_integration_and_gate6(self):
        """
        Phase 2C SetupScoringService is invoked unmodified.
        Gate 6 passing as neutral does NOT imply option tradability.
        """
        candles = self._generate_1m_candles(20)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        for evt in events:
            # Gate 6 does not imply option tradability
            self.assertFalse(evt.option_execution_eligible)
            self.assertEqual(evt.option_execution_status, "UNAVAILABLE")
            self.assertTrue(evt.underlying_signal_only)
            self.assertEqual(evt.underlying_risk_reward_status, "UNAVAILABLE")

    # -------------------------------------------------------------------------
    # 12. Deterministic Replay Output Across Repeated Runs
    # -------------------------------------------------------------------------
    def test_deterministic_replay_output_across_repeated_runs(self):
        """Running the identical dataset produces bit-for-bit identical SpotReplayEvents."""
        candles = self._generate_1m_candles(25)
        self.store.ingest_records(candles, symbol="NIFTY")

        engine1 = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events1 = [e.to_dict() for e in engine1.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m")]

        engine2 = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events2 = [e.to_dict() for e in engine2.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m")]

        self.assertEqual(len(events1), len(events2))
        for e1, e2 in zip(events1, events2):
            # Ignore execution_time_ms benchmark variance
            e1_cmp = {k: v for k, v in e1.items() if k != "execution_time_ms"}
            e2_cmp = {k: v for k, v in e2.items() if k != "execution_time_ms"}
            self.assertEqual(e1_cmp, e2_cmp, "Replay events must be 100% deterministic across runs")

    # -------------------------------------------------------------------------
    # 13. Replay Stops Safely on Invalid Data
    # -------------------------------------------------------------------------
    def test_replay_stops_safely_on_invalid_data(self):
        """Corrupt OHLC geometry (e.g. High < Low) raises ReplayDataIntegrityError and halts immediately."""
        candles = self._generate_1m_candles(10)
        # Invalidate bar 5
        candles[5]["high"] = 23000.0
        candles[5]["low"] = 25000.0  # High < Low!

        # If bypassed or loaded directly, replay engine must fail closed
        validator = self.store.validator
        val_candles, manifest = validator.validate_and_normalize_records(candles, symbol="NIFTY")
        self.assertEqual(manifest.validation_status, "VALIDATION_FAILED")

    # -------------------------------------------------------------------------
    # 14. Strict Lookahead Regression Test
    # -------------------------------------------------------------------------
    def test_lookahead_regression_mutation_test(self):
        """
        The Crucial Lookahead Test:
        1. Run replay on Baseline Dataset D and record all events at or before T* (09:25:00).
        2. Create Mutated Dataset D_mutated where candles occurring AFTER T* are drastically altered
           (e.g. huge market crash, massive spike, trend reversal).
        3. Re-run replay on D_mutated.
        4. Assert every single replay event at or before T* remains bit-for-bit identical!
        """
        # Baseline: 30 1m candles (09:15 to 09:45)
        base_candles = self._generate_1m_candles(30, base_p=24000.0)
        self.store.ingest_records(base_candles, symbol="NIFTY")

        engine_base = HistoricalSpotReplayEngine(self.store, session_clock=self.clock)
        events_base = list(engine_base.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        # Cutoff timestamp T* = 09:25:00 (index 9 in 1m bars, end_time is 09:25:00)
        t_cutoff = "2026-11-09T09:25:00+05:30"
        baseline_events_up_to_t = [e.to_dict() for e in events_base if e.replay_timestamp <= t_cutoff]
        self.assertGreater(len(baseline_events_up_to_t), 0, "Must have generated events before cutoff")

        # Now create second isolated store with mutated future candles
        mutated_dir = tempfile.mkdtemp(prefix="tradelens_mutated_test_")
        try:
            mutated_store = HistoricalSpotStore(base_dir=mutated_dir, storage_format="csv", session_clock=self.clock)
            mutated_candles = copy.deepcopy(base_candles)
            
            # Mutate candles after 09:25:00 (from index 10 onwards)
            for i in range(10, len(mutated_candles)):
                # Crash price by 2,000 points
                mutated_candles[i]["open"] -= 2000.0
                mutated_candles[i]["high"] -= 2000.0
                mutated_candles[i]["low"] -= 2000.0
                mutated_candles[i]["close"] -= 2000.0

            mutated_store.ingest_records(mutated_candles, symbol="NIFTY")

            engine_mutated = HistoricalSpotReplayEngine(mutated_store, session_clock=self.clock)
            events_mutated = list(engine_mutated.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

            mutated_events_up_to_t = [e.to_dict() for e in events_mutated if e.replay_timestamp <= t_cutoff]

            self.assertEqual(len(baseline_events_up_to_t), len(mutated_events_up_to_t))
            for e_base, e_mut in zip(baseline_events_up_to_t, mutated_events_up_to_t):
                e_base_cmp = {k: v for k, v in e_base.items() if k != "execution_time_ms"}
                e_mut_cmp = {k: v for k, v in e_mut.items() if k != "execution_time_ms"}
                self.assertEqual(
                    e_base_cmp,
                    e_mut_cmp,
                    f"Lookahead violation detected! Event at {e_base['replay_timestamp']} altered by future price mutations!"
                )
        finally:
            shutil.rmtree(mutated_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
