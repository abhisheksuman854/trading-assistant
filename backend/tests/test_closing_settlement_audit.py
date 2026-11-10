"""
TradeLens AI - Phase 3E.2 Dedicated Test Suite: Closing Settlement Candle Integrity Audit.

Validates that the isolated 15:29-15:30 NIFTY 50 closing candle returned by the Angel One API:
1. Is conservatively classified as ISOLATED_POST_GAP_UNVERIFIED.
2. Has settlement_semantics = "UNVERIFIED" in the absence of explicit broker tags.
3. Is excluded from ordinary intraday technical indicators (RSI, EMA, MACD, ATR).
4. Is excluded from higher-timeframe candle aggregation (no bridging across the 14-min gap).
5. Is excluded from stop-loss and target triggers across the gap.
6. Is excluded from MFE/MAE excursion calculations.
7. Is excluded from sequential bars_held counts.
8. Cannot confirm structural pivots (i+k pivots).
9. Does not synthetically bridge the 14-minute missing interval (15:15-15:28).
10. Preserves unresolved status for positions open at 15:15, with exit price strictly at 15:15 close.
11. Preserves Phase 3D.2 accepted vs counterfactual cohort separation.
12. Produces deterministic, reproducible replay events.
"""

import unittest
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any
import tempfile
import shutil

from backend.app.data.session_clock import IST_ZONE, IndianMarketSessionClock
from backend.app.data.candle_store import Candle, Timeframe
from backend.app.data.historical_spot_store import (
    HistoricalSpotStore,
    HistoricalSpotCandle,
    DataQualityFlag,
    HistoricalSpotDataValidator
)
from backend.app.data.historical_acquisition import (
    HistoricalCoverageValidator,
    IntervalClassification
)
from backend.app.core.spot_replay_engine import (
    HistoricalSpotReplayEngine,
    SpotReplayEvent
)
from backend.app.core.spot_outcome_tracker import (
    SpotOutcomeTracker,
    SpotOutcomeAttributor,
    SpotOutcomeRecord,
    SpotOutcomeType,
    EntryExecutionConvention,
    OverlappingSignalPolicy,
    TerminalCandleExcursionStatus,
    ExitReason
)


class TestClosingSettlementAudit(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.session_clock = IndianMarketSessionClock()
        self.store = HistoricalSpotStore(base_dir=self.temp_dir, storage_format="csv")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _generate_session_with_isolated_close(
        self,
        session_date: date = date(2026, 11, 2),
        base_price: float = 24000.0,
        settlement_price: float = 24150.0  # +150 points jump
    ) -> List[Dict[str, Any]]:
        """
        Generates 360 continuous 1m bars from 09:15 to 15:14 start times (ending 15:15),
        followed by a 14-minute gap, and 1 isolated flat bar at 15:29:00 (ending 15:30:00).
        Total = 361 bars.
        """
        records = []
        curr_dt = datetime.combine(session_date, time(9, 15), tzinfo=IST_ZONE)
        p = base_price

        # 360 continuous candles (09:15 to 15:14 start times)
        for i in range(360):
            records.append({
                "symbol": "NIFTY",
                "timeframe": "1m",
                "timestamp": curr_dt.isoformat(),
                "start_time": curr_dt.isoformat(),
                "end_time": (curr_dt + timedelta(minutes=1)).isoformat(),
                "open": round(p, 2),
                "high": round(p + 2.0, 2),
                "low": round(p - 2.0, 2),
                "close": round(p + 0.5, 2),
                "volume": 0,
                "is_closed": True
            })
            p += 0.5
            curr_dt += timedelta(minutes=1)

        # 1 isolated flat candle at 15:29:00
        dt_1529 = datetime.combine(session_date, time(15, 29), tzinfo=IST_ZONE)
        records.append({
            "symbol": "NIFTY",
            "timeframe": "1m",
            "timestamp": dt_1529.isoformat(),
            "start_time": dt_1529.isoformat(),
            "end_time": (dt_1529 + timedelta(minutes=1)).isoformat(),
            "open": round(settlement_price, 2),
            "high": round(settlement_price, 2),
            "low": round(settlement_price, 2),
            "close": round(settlement_price, 2),
            "volume": 0,
            "is_closed": True
        })
        return records

    def _create_synthetic_event(
        self,
        event_id: str = "evt_audit_001",
        timestamp: str = "2026-11-02T15:00:00+05:30",
        direction: str = "BULLISH",
        entry: Optional[float] = 24175.0,
        stop: Optional[float] = 24100.0,
        target: Optional[float] = 24300.0,
        score: float = 75.0,
        gates_passed: bool = True
    ) -> SpotReplayEvent:
        return SpotReplayEvent(
            event_id=event_id,
            step_index=1,
            replay_timestamp=timestamp,
            symbol="NIFTY",
            current_market_price=entry or 24175.0,
            last_closed_candle_timestamps={},
            closed_candle_counts={},
            market_regime_5m="BULLISH_TREND",
            market_regime_15m="BULLISH_TREND",
            confirmed_pivots_count_5m=10,
            confirmed_pivots_count_15m=5,
            latest_confirmed_bos_choch="VALID",
            setup_score_bullish=score if direction == "BULLISH" else 0.0,
            setup_score_bearish=score if direction == "BEARISH" else 0.0,
            candidate_direction=direction,
            canonical_eight_gates_status=gates_passed,
            failed_gates=[] if gates_passed else ["GATE_1"],
            structural_entry=entry,
            structural_stop_loss=stop,
            structural_target=target,
            underlying_risk_reward=round((target - entry) / (entry - stop), 2) if entry and stop and target and (entry - stop) > 0 else 2.0,
            underlying_risk_reward_status="AVAILABLE",
            option_execution_eligible=False,
            option_execution_status="UNAVAILABLE",
            underlying_signal_only=True,
            rejection_reasons=[] if gates_passed else ["Gate 1 failed"],
            data_quality_flags=["VALID"],
            execution_time_ms=1.5
        )

    # ------------------------------------------------------------------------
    # 1. Flat Settlement-Like Candle Classification
    # ------------------------------------------------------------------------
    def test_01_flat_settlement_candle_classification(self):
        records = self._generate_session_with_isolated_close()
        validator = HistoricalSpotDataValidator(self.session_clock)
        valid_candles, manifest = validator.validate_and_normalize_records(records, symbol="NIFTY")

        self.assertEqual(len(valid_candles), 361)
        last_c = valid_candles[-1]
        self.assertEqual(last_c.start_time.strftime("%H:%M:%S"), "15:29:00")
        self.assertIn(DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED, last_c.validation_flags)
        self.assertTrue(last_c.is_isolated_post_gap)

        # First 360 candles must NOT have the isolated post-gap flag
        for c in valid_candles[:360]:
            self.assertNotIn(DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED, c.validation_flags)
            self.assertFalse(c.is_isolated_post_gap)

    # ------------------------------------------------------------------------
    # 2. Unverified Settlement Semantics
    # ------------------------------------------------------------------------
    def test_02_unverified_settlement_semantics(self):
        records = self._generate_session_with_isolated_close()
        cov_validator = HistoricalCoverageValidator(self.session_clock)
        val_res = cov_validator.validate_and_classify_candles(
            records,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 2),
            symbol="NIFTY"
        )

        self.assertEqual(val_res["ISOLATED_POST_GAP_COUNT"], 1)
        self.assertEqual(val_res["SETTLEMENT_SEMANTICS"], "UNVERIFIED")

        # Ingest and check DatasetManifest
        manifest = self.store.ingest_records(val_res["valid_candles"], symbol="NIFTY", provider="ANGEL_ONE")
        self.assertEqual(manifest.isolated_post_gap_count, 1)
        self.assertEqual(manifest.settlement_semantics, "UNVERIFIED")
        self.assertEqual(len(manifest.isolated_post_gap_records), 1)
        self.assertEqual(manifest.isolated_post_gap_records[0]["timestamp"], "2026-11-02T15:29:00+05:30")

    # ------------------------------------------------------------------------
    # 3. Exclusion from Intraday Technical Indicators
    # ------------------------------------------------------------------------
    def test_03_exclusion_from_intraday_indicators(self):
        records = self._generate_session_with_isolated_close(settlement_price=25000.0)  # Huge 1000-point spike
        manifest = self.store.ingest_records(records, symbol="NIFTY", provider="ANGEL_ONE")

        engine = HistoricalSpotReplayEngine(self.store, self.session_clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        # The candle access layer must NOT contain the 15:29 candle in its closed store
        closed_1m = engine.data_access.get_closed_candles("NIFTY", Timeframe.M1)
        self.assertEqual(len(closed_1m), 360)
        self.assertEqual(closed_1m[-1]["start_time"], "2026-11-02T15:14:00+05:30")
        self.assertEqual(closed_1m[-1]["end_time"], "2026-11-02T15:15:00+05:30")

        # Technical indicators must calculate purely on continuous pre-gap candles
        vol = engine.indicator_service.compute_volatility("NIFTY", Timeframe.M1)
        self.assertEqual(vol["calculation_status"], "VALID")
        # Bollinger upper would spike to > 24500 if the 25000.0 bar was included.
        # Here prices ended near 24180.0, so Bollinger upper remains well below 24300.0.
        self.assertLess(vol["bollinger"]["upper"], 24300.0)

        mom = engine.indicator_service.compute_momentum("NIFTY", Timeframe.M1)
        self.assertEqual(mom["calculation_status"], "VALID")
        self.assertIsNotNone(mom["rsi"])

    # ------------------------------------------------------------------------
    # 4. Exclusion from Higher Timeframe Resampling
    # ------------------------------------------------------------------------
    def test_04_exclusion_from_higher_timeframe_resampling(self):
        records = self._generate_session_with_isolated_close()
        manifest = self.store.ingest_records(records, symbol="NIFTY", provider="ANGEL_ONE")

        engine = HistoricalSpotReplayEngine(self.store, self.session_clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        closed_5m = engine.data_access.get_closed_candles("NIFTY", Timeframe.M5)
        closed_15m = engine.data_access.get_closed_candles("NIFTY", Timeframe.M15)

        # 360 1m candles = 72 5m candles (09:15 to 15:15)
        self.assertEqual(len(closed_5m), 72)
        self.assertEqual(closed_5m[-1]["end_time"], "2026-11-02T15:15:00+05:30")

        # 360 1m candles = 24 15m candles (09:15 to 15:15)
        self.assertEqual(len(closed_15m), 24)
        self.assertEqual(closed_15m[-1]["end_time"], "2026-11-02T15:15:00+05:30")

        # No synthetic 15:30 15m candle spanning across the 14-min gap!
        for c in closed_15m:
            self.assertNotEqual(c["end_time"], "2026-11-02T15:30:00+05:30")

    # ------------------------------------------------------------------------
    # 5. Exclusion from Stop-Loss and Target Triggers
    # ------------------------------------------------------------------------
    def test_05_exclusion_from_stop_and_target_triggers(self):
        tracker = SpotOutcomeTracker()
        # Create active trade entered at 15:00
        event = self._create_synthetic_event(
            event_id="evt_trade_stop_test",
            timestamp="2026-11-02T15:00:00+05:30",
            direction="BULLISH",
            entry=24180.0,
            stop=24100.0,
            target=24300.0
        )
        # Candle 15:00-15:01 triggers entry at open
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 1, tzinfo=IST_ZONE),
            open=24180.0, high=24185.0, low=24178.0, close=24182.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)
        self.assertEqual(tracker.active_trades_count, 1)

        # Feed candles up to 15:14 (end 15:15)
        for m in range(1, 15):
            c_m = HistoricalSpotCandle(
                symbol="NIFTY", timeframe="1m",
                start_time=datetime(2026, 11, 2, 15, m, tzinfo=IST_ZONE),
                end_time=datetime(2026, 11, 2, 15, m + 1, tzinfo=IST_ZONE),
                open=24182.0, high=24185.0, low=24175.0, close=24180.0
            )
            tracker.process_candle(c_m)
        self.assertEqual(tracker.active_trades_count, 1)

        # Now feed 15:29 isolated candle with price 23900.0 (below stop of 24100.0!)
        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=23900.0, high=23900.0, low=23900.0, close=23900.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        completed = tracker.process_candle(c_1529)

        # Must NOT trigger STOP LOSS! Trade remains ACTIVE and UNRESOLVED
        self.assertEqual(len(completed), 0)
        self.assertEqual(tracker.active_trades_count, 1)
        active_trade = tracker._active_trades[0]
        self.assertIn("isolated_post_gap_bypassed", active_trade.metadata)

    # ------------------------------------------------------------------------
    # 6. Exclusion from MFE/MAE Calculations
    # ------------------------------------------------------------------------
    def test_06_exclusion_from_mfe_mae_calculations(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_mfe_mae_test",
            timestamp="2026-11-02T15:10:00+05:30",
            direction="BULLISH",
            entry=24180.0, stop=24000.0, target=24400.0
        )
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 10, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 11, tzinfo=IST_ZONE),
            open=24180.0, high=24190.0, low=24175.0, close=24185.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)

        # Prior to 15:29, confirmed pre-exit MFE and MAE are 0.0 (entry bar is pre-exit only after subsequent bar)
        c_next = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 11, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 12, tzinfo=IST_ZONE),
            open=24185.0, high=24195.0, low=24170.0, close=24185.0
        )
        tracker.process_candle(c_next)
        pre_mfe = tracker._active_trades[0].confirmed_pre_exit_mfe  # 15.0
        pre_mae = tracker._active_trades[0].confirmed_pre_exit_mae  # 10.0

        # Feed 15:29 with massive spike to 24900.0
        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24900.0, high=24900.0, low=24900.0, close=24900.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_1529)

        # MFE and MAE must NOT be updated or inflated by the 15:29 bar
        post_trade = tracker._active_trades[0]
        self.assertEqual(post_trade.confirmed_pre_exit_mfe, pre_mfe)
        self.assertEqual(post_trade.confirmed_pre_exit_mae, pre_mae)

    # ------------------------------------------------------------------------
    # 7. Exclusion from Sequential bars_held Increment
    # ------------------------------------------------------------------------
    def test_07_exclusion_from_bars_held_increment(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_bars_held_test",
            timestamp="2026-11-02T15:12:00+05:30",
            direction="BULLISH", entry=24180.0, stop=24000.0, target=24400.0
        )
        tracker.register_signal(event)

        c1 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 12, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 13, tzinfo=IST_ZONE),
            open=24180.0, high=24185.0, low=24178.0, close=24182.0
        )
        tracker.process_candle(c1)

        c2 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 13, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            open=24182.0, high=24185.0, low=24178.0, close=24182.0
        )
        tracker.process_candle(c2)

        c3 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24182.0, high=24185.0, low=24178.0, close=24182.0
        )
        tracker.process_candle(c3)
        bars_held_pre = tracker._active_trades[0].bars_held  # 2

        # 15:29 isolated post-gap bar
        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24182.0, high=24182.0, low=24182.0, close=24182.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_1529)

        # bars_held must NOT increment
        self.assertEqual(tracker._active_trades[0].bars_held, bars_held_pre)

    # ------------------------------------------------------------------------
    # 8. Exclusion from Pivot Confirmation
    # ------------------------------------------------------------------------
    def test_08_exclusion_from_pivot_confirmation(self):
        records = self._generate_session_with_isolated_close()
        manifest = self.store.ingest_records(records, symbol="NIFTY", provider="ANGEL_ONE")

        engine = HistoricalSpotReplayEngine(self.store, self.session_clock)
        events = list(engine.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        # Lookahead-free candle access only holds closed bars up to 15:15
        struct_5m = engine.structure_service.analyze_market_structure(symbol="NIFTY", timeframe=Timeframe.M5)
        pivots = struct_5m["pivots"].get("all_pivots", [])
        if pivots:
            for p in pivots:
                # Pivot confirmation timestamp must be <= 15:15:00
                conf_time = p.get("confirmation_timestamp") or p.get("pivot_timestamp")
                if conf_time:
                    self.assertLessEqual(conf_time[:19], "2026-11-02T15:15:00")

    # ------------------------------------------------------------------------
    # 9. No Synthetic Bridging Across Missing Interval
    # ------------------------------------------------------------------------
    def test_09_no_synthetic_bridging_across_missing_interval(self):
        records = self._generate_session_with_isolated_close()
        manifest = self.store.ingest_records(records, symbol="NIFTY", provider="ANGEL_ONE")

        self.assertEqual(manifest.missing_intervals_count, 1)
        m_gap = manifest.missing_intervals[0]
        self.assertEqual(m_gap["from_time"], "15:15:00")
        self.assertEqual(m_gap["to_time"], "15:29:00")
        self.assertEqual(m_gap["missing_candles"], 14)
        self.assertEqual(m_gap["gap_seconds"], 900.0)

        # Verify streamed candles from store contain exactly 361 bars with no synthetic interpolation
        streamed = list(self.store.stream_candles("NIFTY"))
        self.assertEqual(len(streamed), 361)
        self.assertEqual(streamed[359].end_time.strftime("%H:%M:%S"), "15:15:00")
        self.assertEqual(streamed[360].start_time.strftime("%H:%M:%S"), "15:29:00")

    # ------------------------------------------------------------------------
    # 10. Unresolved Position at 15:15 Remains Unresolved (Exit at 15:15 Close)
    # ------------------------------------------------------------------------
    def test_10_unresolved_at_1515_remains_unresolved(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_unresolved_test",
            timestamp="2026-11-02T15:00:00+05:30",
            direction="BULLISH", entry=24180.0, stop=24100.0, target=24300.0
        )
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 1, tzinfo=IST_ZONE),
            open=24180.0, high=24185.0, low=24178.0, close=24182.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)

        # Continuous bars through 15:14 (close = 24190.0)
        c_last_cont = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24188.0, high=24192.0, low=24186.0, close=24190.0
        )
        tracker.process_candle(c_last_cont)

        # Isolated 15:29 bar with jump to 24350.0 (would falsely hit target if evaluated)
        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24350.0, high=24350.0, low=24350.0, close=24350.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_1529)

        # Trade must still be unresolved
        self.assertEqual(tracker.active_trades_count, 1)

        # Finalize unresolved at dataset end
        finalized = tracker.finalize_unresolved()
        self.assertEqual(len(finalized), 1)
        rec = finalized[0]

        # Critical integrity assertions:
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.UNRESOLVED.value)
        self.assertEqual(rec.exit_reason, ExitReason.DATASET_END_UNRESOLVED.value)
        # Under Phase 3E.3: No actual exit occurred, so exit_price and exit_timestamp must be None
        self.assertIsNone(rec.exit_price)
        self.assertIsNone(rec.exit_timestamp)
        # Reference price MUST be the continuous 15:15 close (24190.0), NOT 24350.0!
        self.assertEqual(rec.last_observed_reference_price, 24190.0)
        self.assertEqual(rec.last_observed_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertIsNone(rec.underlying_points_pnl)
        self.assertIsNotNone(rec.unrealized_reference_points_pnl)
        self.assertTrue(rec.execution_metadata.get("isolated_post_gap_excluded"))
        self.assertFalse(rec.execution_metadata.get("actual_exit_occurred"))

    # ------------------------------------------------------------------------
    # 11. Preservation of Phase 3D.2 Cohort Separation
    # ------------------------------------------------------------------------
    def test_11_preservation_of_cohort_separation(self):
        tracker = SpotOutcomeTracker(track_rejected_signals=True)

        # Signal 1: Accepted
        evt_acc = self._create_synthetic_event(
            event_id="evt_acc_01", timestamp="2026-11-02T15:00:00+05:30",
            gates_passed=True
        )
        # Signal 2: Rejected counterfactual
        evt_rej = self._create_synthetic_event(
            event_id="evt_rej_01", timestamp="2026-11-02T15:00:00+05:30",
            gates_passed=False
        )

        c_bar = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 1, tzinfo=IST_ZONE),
            open=24175.0, high=24180.0, low=24170.0, close=24178.0
        )
        tracker.register_signal(evt_acc)
        tracker.register_signal(evt_rej)
        tracker.process_candle(c_bar)

        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24178.0, high=24178.0, low=24178.0, close=24178.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_1529)
        tracker.finalize_unresolved()

        report = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        acc_summary = report["headline_performance_summary"]
        cf_summary = report["rejected_counterfactual_summary"]

        self.assertEqual(acc_summary["total_observations"], 1)
        self.assertEqual(cf_summary["total_observations"], 1)
        self.assertEqual(acc_summary["unresolved_count"], 1)
        self.assertEqual(cf_summary["unresolved_count"], 1)
        self.assertEqual(report["cohort_breakdown"]["ACCEPTED_SIGNAL_OBSERVATIONS"]["unresolved_count"], 1)
        self.assertEqual(report["cohort_breakdown"]["HYPOTHETICAL_REJECTED_COUNTERFACTUALS"]["unresolved_count"], 1)

    # ------------------------------------------------------------------------
    # 12. Deterministic Replay Reproducibility
    # ------------------------------------------------------------------------
    def test_12_deterministic_replay_reproducibility(self):
        records = self._generate_session_with_isolated_close()
        manifest = self.store.ingest_records(records, symbol="NIFTY", provider="ANGEL_ONE")

        engine1 = HistoricalSpotReplayEngine(self.store, self.session_clock)
        events1 = list(engine1.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        engine2 = HistoricalSpotReplayEngine(self.store, self.session_clock)
        events2 = list(engine2.run_replay(symbol="NIFTY", evaluate_on_timeframe="5m"))

        self.assertEqual(len(events1), len(events2))
        self.assertEqual(engine1.candles_processed, engine2.candles_processed)
        self.assertEqual(engine1.candles_processed, 361)

        for e1, e2 in zip(events1, events2):
            self.assertEqual(e1.replay_timestamp, e2.replay_timestamp)
            self.assertEqual(e1.current_market_price, e2.current_market_price)
            self.assertEqual(e1.setup_score_bullish, e2.setup_score_bullish)
            self.assertEqual(e1.canonical_eight_gates_status, e2.canonical_eight_gates_status)


if __name__ == "__main__":
    unittest.main()
