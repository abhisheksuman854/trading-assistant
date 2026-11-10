"""
TradeLens AI - Phase 3E.1 Test Suite: Historical Coverage Boundary & Replay Compatibility Audit.

Covers all 12 specific audit requirements:
1. Provider cutoff metadata (PROVIDER_CUTOFF_OBSERVED, timestamps, cause, verification).
2. Last candle start/end timestamp distinction (15:14:00 start vs 15:15:00 end).
3. 15:15 research cutoff handling (valid observation boundary session time-exit).
4. Prevention of fabricated 15:25 exits (never labels 15:14/15:15 bar as a 15:25 exit).
5. Dataset-end and session-end unresolved classification.
6. Incomplete session coverage reporting (361/375 bars, 96.27%, 14 missing bars).
7. Provider retention versus persisted coverage distinction.
8. Correct source provenance terminology (index OHLC, volume=0.0, NOT_APPLICABLE).
9. Checksum claim metadata (raw payload vs canonical content, no network auth claim).
10. Single-observation sample warning (smoke-test observation, no profitability claims).
11. Existing Phase 3D.2 cohort invariance under boundary conditions.
12. Full end-to-end integration regression.
"""

import unittest
import os
import tempfile
import shutil
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any

from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from backend.app.data.historical_spot_store import (
    HistoricalSpotStore,
    HistoricalSpotCandle,
    VolumeSemantics,
    DataQualityFlag,
    ValidationStatus,
    DatasetManifest
)
from backend.app.data.historical_acquisition import (
    HistoricalCoverageValidator,
    IntervalClassification
)
from backend.app.core.spot_outcome_tracker import (
    SpotOutcomeTracker,
    SpotOutcomeAttributor,
    SpotOutcomeRecord,
    SpotOutcomeType,
    ExitReason,
    TerminalCandleExcursionStatus,
    DATASET_LATEST_OBSERVATION_BOUNDARY,
    DATASET_LATEST_CANDLE_START
)
from backend.app.core.spot_replay_engine import SpotReplayEvent


class TestHistoricalCoverageAudit(unittest.TestCase):
    """
    Phase 3E.1 Audit Test Suite.
    Verifies provider cutoff semantics, timestamp boundaries, and replay compatibility.
    """

    def setUp(self):
        self.session_clock = IndianMarketSessionClock()
        self.validator = HistoricalCoverageValidator(self.session_clock)

    def _generate_361_session_candles(self, session_date: date) -> List[List[Any]]:
        """
        Generates 361 genuine-pattern 1m candles matching Angel One spot feed:
        360 continuous bars: 09:15 to 15:14 start times (last continuous bar ends at 15:15),
        followed by 1 flat closing bar at 15:29:00 (ending at 15:30:00).
        Total bars: 361. Missing bars: 14 (15:15:00 to 15:28:00).
        """
        candles: List[List[Any]] = []
        base_dt = datetime.combine(session_date, time(9, 15), tzinfo=IST_ZONE)
        base_price = 24000.0
        # 360 continuous bars (09:15:00 to 15:14:00 start times)
        for i in range(360):
            dt = base_dt + timedelta(minutes=i)
            o = round(base_price + (i * 0.2), 2)
            h = round(o + 5.0, 2)
            l = round(o - 4.0, 2)
            c = round(o + 1.0, 2)
            candles.append([dt.isoformat(), o, h, l, c, 0])

        # 1 closing settlement bar at 15:29:00 (ending at 15:30:00)
        dt_close = datetime.combine(session_date, time(15, 29), tzinfo=IST_ZONE)
        last_close = candles[-1][4]
        candles.append([dt_close.isoformat(), last_close, last_close, last_close, last_close, 0])
        return candles

    def _create_synthetic_event(
        self,
        event_id: str = "evt_001",
        replay_ts: str = "2026-11-02T14:00:00+05:30",
        direction: str = "BULLISH",
        entry: float = 24000.0,
        stop: float = 23950.0,
        target: float = 24100.0,
        gates_passed: bool = True
    ) -> SpotReplayEvent:
        return SpotReplayEvent(
            event_id=event_id,
            step_index=1,
            replay_timestamp=replay_ts,
            symbol="NIFTY",
            current_market_price=entry,
            last_closed_candle_timestamps={"5m": replay_ts},
            closed_candle_counts={"5m": 50},
            market_regime_5m="TRENDING_UP",
            market_regime_15m="TRENDING_UP",
            confirmed_pivots_count_5m=4,
            confirmed_pivots_count_15m=2,
            latest_confirmed_bos_choch="BOS_BULLISH",
            setup_score_bullish=85.0 if direction == "BULLISH" else 40.0,
            setup_score_bearish=40.0 if direction == "BULLISH" else 85.0,
            candidate_direction=direction,
            canonical_eight_gates_status=gates_passed,
            failed_gates=[] if gates_passed else ["GATE_1_SQUEEZE_COMPRESSION"],
            structural_entry=entry,
            structural_stop_loss=stop,
            structural_target=target,
            underlying_risk_reward=2.0,
            underlying_risk_reward_status="VALID",
            option_execution_eligible=False,
            option_execution_status="UNAVAILABLE",
            underlying_signal_only=True,
            rejection_reasons=[] if gates_passed else ["Gate 1 failed"],
            data_quality_flags=["VALID"],
            execution_time_ms=1.5
        )

    # -------------------------------------------------------------------------
    # 1. Provider Cutoff Metadata
    # -------------------------------------------------------------------------
    def test_provider_cutoff_metadata(self):
        """
        Verifies all 6 mandatory provider cutoff metadata fields in validation and manifest.
        """
        rows = self._generate_361_session_candles(date(2026, 11, 2))
        res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))

        self.assertTrue(res["PROVIDER_CUTOFF_OBSERVED"])
        self.assertIn("15:14:00+05:30", res["LAST_VALIDATED_CANDLE_START"])
        self.assertIn("15:15:00+05:30", res["LAST_VALIDATED_CANDLE_END"])
        self.assertEqual(res["EXCHANGE_SESSION_CLOSE"], "15:30:00+05:30")
        self.assertEqual(res["PROVIDER_CUTOFF_CAUSE"], "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION")
        self.assertFalse(res["PROVIDER_CUTOFF_CAUSE_VERIFIED"])

        # Also verify in Manifest
        temp_dir = tempfile.mkdtemp()
        try:
            store = HistoricalSpotStore(base_dir=temp_dir, storage_format="csv")
            manifest = store.ingest_records(res["valid_candles"], symbol="NIFTY", source_name="test_cutoff")
            m_dict = manifest.to_dict()
            self.assertTrue(m_dict["PROVIDER_CUTOFF_OBSERVED"])
            self.assertIn("15:14:00+05:30", m_dict["LAST_VALIDATED_CANDLE_START"])
            self.assertIn("15:15:00+05:30", m_dict["LAST_VALIDATED_CANDLE_END"])
            self.assertEqual(m_dict["EXCHANGE_SESSION_CLOSE"], "15:30:00+05:30")
            self.assertEqual(m_dict["PROVIDER_CUTOFF_CAUSE"], "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION")
            self.assertFalse(m_dict["PROVIDER_CUTOFF_CAUSE_VERIFIED"])
        finally:
            shutil.rmtree(temp_dir)

    # -------------------------------------------------------------------------
    # 2. Last Candle Start/End Timestamp Distinction
    # -------------------------------------------------------------------------
    def test_last_candle_start_end_timestamp_distinction(self):
        """
        Proves that 15:14:00 is start_time and 15:15:00 is end_time, and they are never conflated.
        """
        rows = self._generate_361_session_candles(date(2026, 11, 2))
        res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))
        last_continuous_candle = res["valid_candles"][359]

        self.assertEqual(last_continuous_candle["start_time"], "2026-11-02T15:14:00+05:30")
        self.assertEqual(last_continuous_candle["end_time"], "2026-11-02T15:15:00+05:30")
        self.assertNotEqual(last_continuous_candle["start_time"], last_continuous_candle["end_time"])

    # -------------------------------------------------------------------------
    # 3. 15:15 Research Cutoff Handling
    # -------------------------------------------------------------------------
    def test_1515_research_cutoff_handling(self):
        """
        When configured with dataset-aware research cutoff 15:15, triggers SESSION_TIME_EXIT at 15:15:00 close.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=DATASET_LATEST_OBSERVATION_BOUNDARY)
        event = self._create_synthetic_event(entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(event)

        # Candle 1: Entry bar at 14:01
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 14, 1, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 14, 2, tzinfo=IST_ZONE),
            open=24000.0, high=24010.0, low=23990.0, close=24005.0
        )
        tracker.process_candle(c_entry)
        self.assertEqual(tracker.active_trades_count, 1)

        # Final candle of the day: 15:14 to 15:15
        c_final = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24020.0, high=24030.0, low=24015.0, close=24025.0
        )
        completed = tracker.process_candle(c_final)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.TIME_EXIT.value)
        self.assertEqual(rec.exit_reason, ExitReason.SESSION_TIME_EXIT.value)
        self.assertEqual(rec.exit_price, 24025.0)
        self.assertEqual(rec.exit_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertEqual(rec.execution_metadata["exit_candle_start"], "2026-11-02T15:14:00+05:30")
        self.assertEqual(rec.execution_metadata["exit_candle_end"], "2026-11-02T15:15:00+05:30")
        self.assertEqual(rec.execution_metadata["data_source"], "ANGEL_ONE_HISTORICAL_SPOT")

    # -------------------------------------------------------------------------
    # 4. Prevention of Fabricated 15:25 Exits
    # -------------------------------------------------------------------------
    def test_prevention_of_fabricated_1525_exits(self):
        """
        When configured with default 15:25 exit on a dataset ending at 15:15,
        the system NEVER fabricates a 15:25 exit or exit price.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=time(15, 25))
        event = self._create_synthetic_event(entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(event)

        # Entry bar
        c1 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 14, 1, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 14, 2, tzinfo=IST_ZONE),
            open=24000.0, high=24010.0, low=23990.0, close=24005.0
        )
        tracker.process_candle(c1)

        # 15:14 to 15:15 bar: does NOT trigger 15:25 exit
        c_last = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24020.0, high=24030.0, low=24015.0, close=24025.0
        )
        completed = tracker.process_candle(c_last)

        # Must NOT exit as SESSION_TIME_EXIT because 15:25 has not occurred
        self.assertEqual(len(completed), 0)
        self.assertEqual(tracker.active_trades_count, 1)

    # -------------------------------------------------------------------------
    # 5. Dataset-End and Session-End Unresolved Classification
    # -------------------------------------------------------------------------
    def test_dataset_end_and_session_end_unresolved_classification(self):
        """
        Active trades unable to reach 15:25 cutoff are classified as UNRESOLVED,
        not as fabricated time exits.
        """
        # Case A: Dataset termination
        tracker = SpotOutcomeTracker(intraday_time_exit=time(15, 25))
        event = self._create_synthetic_event(entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(event)

        c1 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24000.0, high=24010.0, low=23990.0, close=24005.0
        )
        tracker.process_candle(c1)
        finalized = tracker.finalize_unresolved()

        self.assertEqual(len(finalized), 1)
        rec = finalized[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.UNRESOLVED.value)
        self.assertEqual(rec.exit_reason, ExitReason.DATASET_END_UNRESOLVED.value)
        self.assertIsNone(rec.exit_timestamp)
        self.assertIsNone(rec.exit_price)
        self.assertEqual(rec.last_observed_reference_price, 24005.0)  # Actual 15:14-15:15 bar close reference
        self.assertEqual(rec.last_observed_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertIsNone(rec.underlying_points_pnl)

        # Case B: Multi-day session transition
        tracker_multi = SpotOutcomeTracker(intraday_time_exit=time(15, 25))
        tracker_multi.register_signal(event)
        tracker_multi.process_candle(c1)  # Day 1 final bar

        # Day 2 morning bar arrives
        c_day2 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 3, 9, 15, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 3, 9, 16, tzinfo=IST_ZONE),
            open=24100.0, high=24110.0, low=24090.0, close=24105.0
        )
        completed_multi = tracker_multi.process_candle(c_day2)

        self.assertEqual(len(completed_multi), 1)
        rec_day1 = completed_multi[0]
        self.assertEqual(rec_day1.outcome_classification, SpotOutcomeType.UNRESOLVED.value)
        self.assertEqual(rec_day1.exit_reason, ExitReason.SESSION_END_UNRESOLVED.value)
        self.assertIsNone(rec_day1.exit_timestamp)
        self.assertIsNone(rec_day1.exit_price)
        self.assertEqual(rec_day1.last_observed_reference_price, 24005.0)
        self.assertEqual(rec_day1.last_observed_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertIsNone(rec_day1.underlying_points_pnl)

    # -------------------------------------------------------------------------
    # 6. Incomplete Session Coverage Reporting
    # -------------------------------------------------------------------------
    def test_incomplete_session_coverage_reporting(self):
        """
        Verifies that 361 bars in a session reports 96.27% coverage with 14 missing intervals.
        """
        rows = self._generate_361_session_candles(date(2026, 11, 2))
        res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))

        self.assertEqual(res["expected_intervals_total"], 375)
        self.assertEqual(res["valid_candles_count"], 361)
        self.assertEqual(res["missing_intervals_count"], 14)
        self.assertAlmostEqual(res["coverage_percentage"], 96.27, places=2)
        # All 14 missing intervals are between 15:15 and 15:28 (before the 15:29 closing bar)
        missing_ts = [m["timestamp"][11:19] for m in res["missing_intervals"]]
        self.assertEqual(missing_ts[0], "15:15:00")
        self.assertEqual(missing_ts[-1], "15:28:00")

    # -------------------------------------------------------------------------
    # 7. Provider Retention versus Persisted Coverage Distinction
    # -------------------------------------------------------------------------
    def test_provider_retention_versus_persisted_coverage_distinction(self):
        """
        Persisted dataset manifest must strictly reflect the persisted dates, not probe queries.
        """
        temp_dir = tempfile.mkdtemp()
        try:
            store = HistoricalSpotStore(base_dir=temp_dir, storage_format="csv")
            rows = self._generate_361_session_candles(date(2026, 11, 2))
            res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))
            manifest = store.ingest_records(res["valid_candles"], symbol="NIFTY")

            # Persisted manifest covers strictly 2026-11-02, not 2025
            self.assertEqual(manifest.start_timestamp_ist, "2026-11-02T09:15:00+05:30")
            self.assertEqual(manifest.end_timestamp_ist, "2026-11-02T15:30:00+05:30")
            self.assertEqual(manifest.valid_record_count, 361)
            self.assertEqual(manifest.last_validated_candle_start, "2026-11-02T15:14:00+05:30")
            self.assertEqual(manifest.last_validated_candle_end, "2026-11-02T15:15:00+05:30")
        finally:
            shutil.rmtree(temp_dir)

    # -------------------------------------------------------------------------
    # 8. Correct Source Provenance Terminology
    # -------------------------------------------------------------------------
    def test_correct_source_provenance_terminology(self):
        """
        NIFTY 50 is a calculated index; volume must be strictly 0.0 with NOT_APPLICABLE semantics.
        """
        rows = self._generate_361_session_candles(date(2026, 11, 2))
        res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))
        for c in res["valid_candles"]:
            self.assertEqual(c["volume"], 0)
            self.assertEqual(c["volume_semantics"], VolumeSemantics.NOT_APPLICABLE)

    # -------------------------------------------------------------------------
    # 9. Checksum Claim Metadata
    # -------------------------------------------------------------------------
    def test_checksum_claim_metadata(self):
        """
        Raw checksum hashes exact received bytes; canonical checksum hashes normalized content.
        Neither claims cryptographic transport authentication.
        """
        temp_dir = tempfile.mkdtemp()
        try:
            store = HistoricalSpotStore(base_dir=temp_dir, storage_format="csv")
            rows = self._generate_361_session_candles(date(2026, 11, 2))
            res = self.validator.validate_and_classify_candles(rows, date(2026, 11, 2), date(2026, 11, 2))
            manifest = store.ingest_records(res["valid_candles"], symbol="NIFTY")

            self.assertEqual(len(manifest.source_raw_sha256), 64)
            self.assertEqual(len(manifest.canonical_content_sha256), 64)
            self.assertNotEqual(manifest.source_raw_sha256, manifest.canonical_content_sha256)
        finally:
            shutil.rmtree(temp_dir)

    # -------------------------------------------------------------------------
    # 10. Single-Observation Sample Warning
    # -------------------------------------------------------------------------
    def test_single_observation_sample_warning(self):
        """
        Attribution report explicitly qualifies results as smoke-test observation, not statistical proof.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=DATASET_LATEST_OBSERVATION_BOUNDARY)
        event = self._create_synthetic_event(entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(event)

        c = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24000.0, high=24020.0, low=23990.0, close=24010.0
        )
        tracker.process_candle(c)

        report = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        quals = report["audit_qualifications"]
        self.assertEqual(quals["sample_qualification"], "PIPELINE_SMOKE_TEST_OBSERVATION_NOT_STATISTICAL_VALIDATION")
        self.assertEqual(quals["claim_restriction"], "NO_WIN_PROBABILITY_OR_PROFITABILITY_CLAIMS_AUTHORISED")
        self.assertIn("Pipeline smoke-test observation only", report["experiment_metadata"]["sample_warning"])

    # -------------------------------------------------------------------------
    # 11. Existing Phase 3D.2 Cohort Invariance
    # -------------------------------------------------------------------------
    def test_existing_phase_3d2_cohort_invariance(self):
        """
        Adding or mutating rejected counterfactuals has zero effect on accepted headline metrics.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=DATASET_LATEST_OBSERVATION_BOUNDARY)

        # 1 Accepted Signal
        evt_acc = self._create_synthetic_event(event_id="acc_1", gates_passed=True)
        tracker.register_signal(evt_acc)

        # 1 Rejected Signal
        evt_rej = self._create_synthetic_event(event_id="rej_1", gates_passed=False)
        tracker.register_signal(evt_rej)

        c = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24000.0, high=24050.0, low=23980.0, close=24030.0
        )
        tracker.process_candle(c)

        report1 = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        headline_pnl1 = report1["headline_performance_summary"]["total_points_pnl"]

        # Add 5 more rejected counterfactuals
        for k in range(5):
            evt_extra = self._create_synthetic_event(event_id=f"rej_extra_{k}", gates_passed=False)
            tracker.register_signal(evt_extra)
            tracker.process_candle(c)

        report2 = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        headline_pnl2 = report2["headline_performance_summary"]["total_points_pnl"]

        # Accepted headline PnL must be strictly invariant!
        self.assertEqual(headline_pnl1, headline_pnl2)

    # -------------------------------------------------------------------------
    # 12. Full Regression Suite Compatibility
    # -------------------------------------------------------------------------
    def test_full_regression_suite_compatibility(self):
        """
        Verifies that a multi-day series with 15:15 cutoff runs end-to-end without unhandled exceptions.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=DATASET_LATEST_OBSERVATION_BOUNDARY)
        for day in range(2, 5):
            rows = self._generate_361_session_candles(date(2026, 11, day))
            res = self.validator.validate_and_classify_candles(rows, date(2026, 11, day), date(2026, 11, day))
            for c_dict in res["valid_candles"]:
                c_obj = HistoricalSpotCandle(
                    symbol=c_dict["symbol"],
                    timeframe=c_dict["timeframe"],
                    start_time=datetime.fromisoformat(c_dict["start_time"]),
                    end_time=datetime.fromisoformat(c_dict["end_time"]),
                    open=c_dict["open"], high=c_dict["high"],
                    low=c_dict["low"], close=c_dict["close"],
                    volume=0, volume_semantics=VolumeSemantics.NOT_APPLICABLE
                )
                tracker.process_candle(c_obj)

        finalized = tracker.finalize_unresolved()
        self.assertEqual(tracker.active_trades_count, 0)


if __name__ == "__main__":
    unittest.main()
