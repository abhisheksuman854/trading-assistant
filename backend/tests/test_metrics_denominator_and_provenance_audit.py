"""
TradeLens AI - Phase 3E.4 Dedicated Audit Test Suite:
Realized Metrics Denominator & Dataset Provenance Audit.

Tests:
1. ENTRY_INVALIDATED excluded from resolved denominator.
2. MISSED_ENTRY excluded from resolved denominator.
3. EXCLUDED_NON_ELIGIBLE excluded from resolved denominator.
4. Genuine target/stop/time exits included.
5. Unresolved records excluded even when reference P&L is large.
6. Metadata/classification contradictions rejected and reported.
7. Counterfactual mutations cannot change accepted headline metrics.
8. Correct session date and candle-count provenance (2026-11-02 to 2026-11-06, 1805 candles).
9. Special Muhurat session calendar handling (80m on 2026-11-08, not 375m).
10. Deterministic replay and full regression compatibility.
"""

import json
import csv
import unittest
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any

from backend.app.data.session_clock import IST_ZONE, IndianMarketSessionClock
from backend.app.data.candle_store import Candle, Timeframe
from backend.app.data.historical_spot_store import (
    HistoricalSpotStore,
    HistoricalSpotCandle,
    DataQualityFlag
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


class TestMetricsDenominatorAndProvenanceAudit(unittest.TestCase):

    def _create_synthetic_record(
        self,
        record_id: str,
        classification: SpotOutcomeType,
        actual_exit_occurred: bool,
        exit_price: Optional[float] = None,
        exit_timestamp: Optional[str] = None,
        underlying_points_pnl: Optional[float] = None,
        last_observed_reference_price: Optional[float] = None,
        last_observed_timestamp: Optional[str] = None,
        unrealized_reference_points_pnl: Optional[float] = None,
        exit_reason: str = "TARGET_HIT",
        is_counterfactual: bool = False,
        canonical_gates_passed: bool = True
    ) -> SpotOutcomeRecord:
        return SpotOutcomeRecord(
            record_id=record_id,
            signal_id=f"sig_{record_id}",
            signal_timestamp="2026-11-02T09:30:00+05:30",
            direction="BULLISH",
            entry_reference_price=24000.0 if classification != SpotOutcomeType.ENTRY_INVALIDATED else None,
            entry_timestamp="2026-11-02T09:30:00+05:30" if classification != SpotOutcomeType.ENTRY_INVALIDATED else None,
            entry_convention="NEXT_BAR_OPEN",
            slippage_points=0.0,
            structural_stop_loss=23950.0,
            structural_target=24100.0,
            initial_risk_points=50.0 if classification != SpotOutcomeType.ENTRY_INVALIDATED else None,
            first_eligible_candle_timestamp="2026-11-02T09:30:00+05:30",
            exit_timestamp=exit_timestamp,
            exit_price=exit_price,
            exit_reason=exit_reason,
            outcome_classification=classification.value,
            mfe_points=0.0,
            mae_points=0.0,
            mfe_r_multiple=0.0,
            mae_r_multiple=0.0,
            terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
            terminal_candle_max_favourable_bound=None,
            terminal_candle_max_adverse_bound=None,
            underlying_points_pnl=underlying_points_pnl,
            bars_held=2 if actual_exit_occurred else 0,
            setup_score=80.0,
            canonical_gates_passed=canonical_gates_passed,
            is_counterfactual=is_counterfactual,
            failed_gates=[] if canonical_gates_passed else ["GATE_1"],
            gate_rejection_reasons=[] if canonical_gates_passed else ["Gate 1 failed"],
            data_quality_flags=[],
            execution_metadata={
                "underlying_signal_only": True,
                "actual_exit_occurred": actual_exit_occurred,
                "outcome_type": classification.value
            },
            last_observed_reference_price=last_observed_reference_price,
            last_observed_timestamp=last_observed_timestamp,
            unrealized_reference_points_pnl=unrealized_reference_points_pnl
        )

    # ------------------------------------------------------------------------
    # Test 1: ENTRY_INVALIDATED Excluded from Resolved Denominator
    # ------------------------------------------------------------------------
    def test_01_entry_invalidated_excluded_from_resolved_denominator(self):
        rec_resolved = self._create_synthetic_record(
            record_id="r_res",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=True,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0
        )
        rec_inv1 = self._create_synthetic_record(
            record_id="r_inv1",
            classification=SpotOutcomeType.ENTRY_INVALIDATED,
            actual_exit_occurred=False,
            exit_reason=ExitReason.OPEN_BEYOND_STOP.value
        )
        rec_inv2 = self._create_synthetic_record(
            record_id="r_inv2",
            classification=SpotOutcomeType.ENTRY_INVALIDATED,
            actual_exit_occurred=False,
            exit_reason=ExitReason.INVALID_RISK_GEOMETRY.value
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_resolved, rec_inv1, rec_inv2])

        self.assertEqual(metrics["total_observations"], 3)
        self.assertEqual(metrics["eligible_observations"], 1)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["actual_resolved_exits_count"], 1)
        self.assertEqual(metrics["entry_invalidated_count"], 2)
        self.assertEqual(metrics["denominators"]["resolved_denominator"], 1)
        self.assertEqual(metrics["denominators"]["eligible_denominator"], 1)
        self.assertEqual(metrics["denominators"]["observed_denominator"], 3)
        self.assertEqual(metrics["total_realized_points_pnl"], 100.0)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], 100.0)
        self.assertEqual(metrics["expectancy_per_eligible_pts"], 100.0)

    # ------------------------------------------------------------------------
    # Test 2: MISSED_ENTRY Excluded from Resolved Denominator
    # ------------------------------------------------------------------------
    def test_02_missed_entry_excluded_from_resolved_denominator(self):
        rec_resolved = self._create_synthetic_record(
            record_id="r_res",
            classification=SpotOutcomeType.STOP_LOSS_HIT,
            actual_exit_occurred=True,
            exit_price=23950.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=-50.0,
            exit_reason=ExitReason.STOP_LOSS_HIT.value
        )
        rec_miss1 = self._create_synthetic_record(
            record_id="r_miss1",
            classification=SpotOutcomeType.MISSED_ENTRY,
            actual_exit_occurred=False,
            exit_reason=ExitReason.OPEN_BEYOND_TARGET.value
        )
        rec_miss2 = self._create_synthetic_record(
            record_id="r_miss2",
            classification=SpotOutcomeType.MISSED_ENTRY,
            actual_exit_occurred=False,
            exit_reason=ExitReason.SLIPPAGE_BREACH_TARGET.value
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_resolved, rec_miss1, rec_miss2])

        self.assertEqual(metrics["total_observations"], 3)
        self.assertEqual(metrics["eligible_observations"], 1)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["missed_entry_count"], 2)
        self.assertEqual(metrics["denominators"]["resolved_denominator"], 1)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], -50.0)

    # ------------------------------------------------------------------------
    # Test 3: EXCLUDED_NON_ELIGIBLE Excluded from Resolved Denominator
    # ------------------------------------------------------------------------
    def test_03_excluded_non_eligible_excluded_from_resolved_denominator(self):
        rec_resolved = self._create_synthetic_record(
            record_id="r_res",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=True,
            exit_price=24050.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=50.0
        )
        rec_excl1 = self._create_synthetic_record(
            record_id="r_excl1",
            classification=SpotOutcomeType.EXCLUDED_NON_ELIGIBLE,
            actual_exit_occurred=False,
            exit_reason=ExitReason.SKIPPED_DUE_TO_ACTIVE_POSITION.value
        )
        rec_excl2 = self._create_synthetic_record(
            record_id="r_excl2",
            classification=SpotOutcomeType.EXCLUDED_NON_ELIGIBLE,
            actual_exit_occurred=False,
            exit_reason=ExitReason.MISSING_STRUCTURAL_LEVELS.value
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_resolved, rec_excl1, rec_excl2])

        self.assertEqual(metrics["total_observations"], 3)
        self.assertEqual(metrics["eligible_observations"], 1)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["excluded_non_eligible_count"], 2)
        self.assertEqual(metrics["denominators"]["resolved_denominator"], 1)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], 50.0)

    # ------------------------------------------------------------------------
    # Test 4: Genuine Target, Stop, and Time Exits Included
    # ------------------------------------------------------------------------
    def test_04_genuine_target_stop_time_exits_included(self):
        rec_target = self._create_synthetic_record(
            record_id="r_tgt",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=True,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=60.0
        )
        rec_stop = self._create_synthetic_record(
            record_id="r_stp",
            classification=SpotOutcomeType.STOP_LOSS_HIT,
            actual_exit_occurred=True,
            exit_price=23950.0,
            exit_timestamp="2026-11-02T10:00:00+05:30",
            underlying_points_pnl=-30.0,
            exit_reason=ExitReason.STOP_LOSS_HIT.value
        )
        rec_time = self._create_synthetic_record(
            record_id="r_time",
            classification=SpotOutcomeType.TIME_EXIT,
            actual_exit_occurred=True,
            exit_price=24010.0,
            exit_timestamp="2026-11-02T15:25:00+05:30",
            underlying_points_pnl=10.0,
            exit_reason=ExitReason.SESSION_TIME_EXIT.value
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_target, rec_stop, rec_time])

        self.assertEqual(metrics["total_observations"], 3)
        self.assertEqual(metrics["eligible_observations"], 3)
        self.assertEqual(metrics["resolved_observations"], 3)
        self.assertEqual(metrics["actual_resolved_exits_count"], 3)
        self.assertEqual(metrics["target_reached_count"], 1)
        self.assertEqual(metrics["stop_loss_hit_count"], 1)
        self.assertEqual(metrics["time_exit_count"], 1)
        self.assertEqual(metrics["total_realized_points_pnl"], 40.0)  # 60 - 30 + 10
        self.assertEqual(metrics["expectancy_per_resolved_pts"], round(40.0 / 3, 2))  # 13.33

    # ------------------------------------------------------------------------
    # Test 5: Unresolved Records Excluded Even When Reference P&L is Large
    # ------------------------------------------------------------------------
    def test_05_unresolved_records_excluded_even_when_reference_pnl_is_large(self):
        rec_resolved = self._create_synthetic_record(
            record_id="r_res",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=True,
            exit_price=24050.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=50.0
        )
        rec_unres_bull = self._create_synthetic_record(
            record_id="r_unres1",
            classification=SpotOutcomeType.UNRESOLVED,
            actual_exit_occurred=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            last_observed_reference_price=24500.0,
            last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=500.0,  # Huge unrealized +500 pts
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value
        )
        rec_unres_bear = self._create_synthetic_record(
            record_id="r_unres2",
            classification=SpotOutcomeType.UNRESOLVED,
            actual_exit_occurred=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            last_observed_reference_price=23600.0,
            last_observed_timestamp="2026-11-03T15:15:00+05:30",
            unrealized_reference_points_pnl=-400.0,  # Huge unrealized -400 pts
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_resolved, rec_unres_bull, rec_unres_bear])

        self.assertEqual(metrics["total_observations"], 3)
        self.assertEqual(metrics["eligible_observations"], 3)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["actual_resolved_exits_count"], 1)
        self.assertEqual(metrics["unresolved_count"], 2)
        self.assertEqual(metrics["session_end_unresolved_count"], 2)
        self.assertEqual(metrics["dataset_end_unresolved_count"], 0)
        self.assertEqual(metrics["total_realized_points_pnl"], 50.0)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], 50.0)  # NOT diluted by 3
        self.assertEqual(metrics["total_unrealized_reference_points_pnl"], 100.0)  # 500 - 400

    # ------------------------------------------------------------------------
    # Test 6: Metadata / Classification Contradictions Rejected & Reported
    # ------------------------------------------------------------------------
    def test_06_metadata_classification_contradictions_rejected_and_reported(self):
        # Contradiction A: TARGET_REACHED but actual_exit_occurred is False
        rec_bad1 = self._create_synthetic_record(
            record_id="r_bad1",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=False,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0
        )
        # Contradiction B: TARGET_REACHED but exit_price is None
        rec_bad2 = self._create_synthetic_record(
            record_id="r_bad2",
            classification=SpotOutcomeType.TARGET_REACHED,
            actual_exit_occurred=True,
            exit_price=None,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0
        )
        # Contradiction C: UNRESOLVED but exit_price is populated
        rec_bad3 = self._create_synthetic_record(
            record_id="r_bad3",
            classification=SpotOutcomeType.UNRESOLVED,
            actual_exit_occurred=False,
            exit_price=24000.0,  # Must be None for UNRESOLVED!
            exit_timestamp=None,
            underlying_points_pnl=None
        )
        # Contradiction D: ENTRY_INVALIDATED but underlying_points_pnl is populated
        rec_bad4 = self._create_synthetic_record(
            record_id="r_bad4",
            classification=SpotOutcomeType.ENTRY_INVALIDATED,
            actual_exit_occurred=False,
            underlying_points_pnl=50.0  # Must be None for invalid!
        )

        # Check contradictions directly
        self.assertGreater(len(SpotOutcomeAttributor.check_record_contradictions(rec_bad1)), 0)
        self.assertGreater(len(SpotOutcomeAttributor.check_record_contradictions(rec_bad2)), 0)
        self.assertGreater(len(SpotOutcomeAttributor.check_record_contradictions(rec_bad3)), 0)
        self.assertGreater(len(SpotOutcomeAttributor.check_record_contradictions(rec_bad4)), 0)

        # In calculate_metrics, none of these can be treated as resolved simulated exits!
        metrics = SpotOutcomeAttributor.calculate_metrics([rec_bad1, rec_bad2, rec_bad3, rec_bad4])
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["actual_resolved_exits_count"], 0)
        self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], 0.0)
        self.assertGreaterEqual(metrics["contradiction_count"], 4)
        self.assertGreaterEqual(len(metrics["contradictions"]), 4)

    # ------------------------------------------------------------------------
    # Test 7: Counterfactual Mutations Cannot Change Accepted Headline Metrics
    # ------------------------------------------------------------------------
    def test_07_counterfactual_mutations_cannot_change_accepted_headline_metrics(self):
        accepted = [
            self._create_synthetic_record(
                record_id="rec_acc_1",
                classification=SpotOutcomeType.TARGET_REACHED,
                actual_exit_occurred=True,
                exit_price=24100.0,
                exit_timestamp="2026-11-05T12:37:00+05:30",
                underlying_points_pnl=67.6,
                is_counterfactual=False,
                canonical_gates_passed=True
            )
        ]

        # Scenario A: Baseline with 1 counterfactual stop loss
        cf_base = [
            self._create_synthetic_record(
                record_id="cf_base_1",
                classification=SpotOutcomeType.STOP_LOSS_HIT,
                actual_exit_occurred=True,
                exit_price=23950.0,
                exit_timestamp="2026-11-04T10:00:00+05:30",
                underlying_points_pnl=-50.0,
                is_counterfactual=True,
                canonical_gates_passed=False
            )
        ]
        rep_base = SpotOutcomeAttributor.generate_full_attribution_report(accepted + cf_base)

        # Scenario B: Mutated with 10 huge counterfactual losses (-500 pts)
        cf_loss = [
            self._create_synthetic_record(
                record_id=f"cf_loss_{i}",
                classification=SpotOutcomeType.STOP_LOSS_HIT,
                actual_exit_occurred=True,
                exit_price=23950.0,
                exit_timestamp="2026-11-04T10:00:00+05:30",
                underlying_points_pnl=-50.0,
                is_counterfactual=True,
                canonical_gates_passed=False
            ) for i in range(10)
        ]
        rep_loss = SpotOutcomeAttributor.generate_full_attribution_report(accepted + cf_loss)

        # Scenario C: Mutated with 10 massive counterfactual wins (+1000 pts)
        cf_win = [
            self._create_synthetic_record(
                record_id=f"cf_win_{i}",
                classification=SpotOutcomeType.TARGET_REACHED,
                actual_exit_occurred=True,
                exit_price=24100.0,
                exit_timestamp="2026-11-04T10:00:00+05:30",
                underlying_points_pnl=100.0,
                is_counterfactual=True,
                canonical_gates_passed=False
            ) for i in range(10)
        ]
        rep_win = SpotOutcomeAttributor.generate_full_attribution_report(accepted + cf_win)

        # Assert Headline performance summary is 100% BIT-FOR-BIT IDENTICAL across all scenarios
        self.assertEqual(rep_base["headline_performance_summary"], rep_loss["headline_performance_summary"])
        self.assertEqual(rep_base["headline_performance_summary"], rep_win["headline_performance_summary"])
        self.assertEqual(rep_base["headline_performance_summary"]["expectancy_per_resolved_pts"], 67.6)
        self.assertEqual(rep_base["headline_performance_summary"]["total_realized_points_pnl"], 67.6)

    # ------------------------------------------------------------------------
    # Test 8: Dataset Date-Range & Session Candle-Count Provenance
    # ------------------------------------------------------------------------
    def test_08_dataset_date_range_and_session_candle_count_provenance(self):
        # 1. Inspect raw checkpoint
        with open("data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json") as f:
            cp = json.load(f)
        self.assertEqual(cp["requested_start"], "2026-11-02")
        self.assertEqual(cp["requested_end"], "2026-11-06")
        self.assertEqual(cp["chunks"][0]["records_count"], 1805)

        # 2. Inspect manifest
        with open("data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json") as f:
            mf = json.load(f)
        self.assertEqual(mf["start_timestamp_ist"], "2026-11-02T09:15:00+05:30")
        self.assertEqual(mf["end_timestamp_ist"], "2026-11-06T15:30:00+05:30")
        self.assertEqual(mf["record_count"], 1805)
        self.assertEqual(mf["isolated_post_gap_count"], 5)

        # 3. Inspect partitioned CSV counts per session date
        csv_path = "data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv"
        date_counts: Dict[str, int] = {}
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = row["start_time"][:10]
                date_counts[d] = date_counts.get(d, 0) + 1

        expected_counts = {
            "2026-11-02": 361,
            "2026-11-03": 361,
            "2026-11-04": 361,
            "2026-11-05": 361,
            "2026-11-06": 361
        }
        self.assertEqual(date_counts, expected_counts)
        self.assertEqual(sum(date_counts.values()), 1805)
        self.assertNotIn("2026-11-07", date_counts)
        self.assertNotIn("2026-11-08", date_counts)

    # ------------------------------------------------------------------------
    # Test 9: Special Muhurat Session Calendar Handling
    # ------------------------------------------------------------------------
    def test_09_special_muhurat_session_calendar_handling(self):
        clock = IndianMarketSessionClock()

        # Regular trading on Diwali (2026-11-08) is a holiday
        self.assertTrue(clock.is_holiday(date(2026, 11, 8)))

        # Morning hours (10:00 IST) are strictly market closed
        dt_morning = datetime(2026, 11, 8, 10, 0, tzinfo=IST_ZONE)
        self.assertFalse(clock.is_market_open(dt_morning))

        # Muhurat trading window (14:20 to 15:40 IST) is market open
        dt_muhurat = datetime(2026, 11, 8, 14, 30, tzinfo=IST_ZONE)
        self.assertTrue(clock.is_market_open(dt_muhurat))

        # Expected intervals on 2026-11-08 must be exactly 80 minutes, NOT 375 minutes
        validator = HistoricalCoverageValidator(clock)
        expected = validator.generate_expected_session_intervals(date(2026, 11, 8), date(2026, 11, 8), interval_minutes=1)
        self.assertEqual(len(expected), 80)
        first_candle = min(expected.keys())
        last_candle = max(expected.keys())
        self.assertIn("14:20:00", first_candle)
        self.assertIn("15:39:00", last_candle)

    # ------------------------------------------------------------------------
    # Test 10: Deterministic Replay & Full Regression Compatibility
    # ------------------------------------------------------------------------
    def test_10_deterministic_replay_and_full_regression_compatibility(self):
        store = HistoricalSpotStore(base_dir="data/historical")
        engine1 = HistoricalSpotReplayEngine(store)

        # Run 1
        tracker1 = SpotOutcomeTracker(track_rejected_signals=True)
        list(engine1.run_replay(symbol="NIFTY", outcome_tracker=tracker1, evaluate_on_timeframe="5m"))

        # Run 2 (independent engine instance)
        engine2 = HistoricalSpotReplayEngine(store)
        tracker2 = SpotOutcomeTracker(track_rejected_signals=True)
        list(engine2.run_replay(symbol="NIFTY", outcome_tracker=tracker2, evaluate_on_timeframe="5m"))

        # Both runs must generate exactly 14 records with bit-for-bit identical IDs and values
        self.assertEqual(len(tracker1.completed_records), 14)
        self.assertEqual(len(tracker2.completed_records), 14)
        for r1, r2 in zip(tracker1.completed_records, tracker2.completed_records):
            self.assertEqual(r1.to_dict(), r2.to_dict())

        rep = SpotOutcomeAttributor.generate_full_attribution_report(tracker1.completed_records)
        acc = rep["headline_performance_summary"]
        rej = rep["rejected_counterfactual_summary"]

        # Accepted Strategy Summary
        self.assertEqual(acc["total_observations"], 1)
        self.assertEqual(acc["eligible_observations"], 1)
        self.assertEqual(acc["resolved_observations"], 1)
        self.assertEqual(acc["actual_resolved_exits_count"], 1)
        self.assertEqual(acc["unresolved_count"], 0)
        self.assertEqual(acc["session_end_unresolved_count"], 0)
        self.assertEqual(acc["dataset_end_unresolved_count"], 0)
        self.assertEqual(acc["entry_invalidated_count"], 0)
        self.assertEqual(acc["missed_entry_count"], 0)
        self.assertEqual(acc["excluded_non_eligible_count"], 0)
        self.assertEqual(acc["total_realized_points_pnl"], 67.6)
        self.assertEqual(acc["expectancy_per_resolved_pts"], 67.6)
        self.assertEqual(acc["contradiction_count"], 0)

        # Rejected Counterfactual Summary
        self.assertEqual(rej["total_observations"], 13)
        self.assertEqual(rej["eligible_observations"], 13)
        self.assertEqual(rej["resolved_observations"], 10)
        self.assertEqual(rej["actual_resolved_exits_count"], 10)
        self.assertEqual(rej["unresolved_count"], 3)
        self.assertEqual(rej["session_end_unresolved_count"], 3)
        self.assertEqual(rej["dataset_end_unresolved_count"], 0)
        self.assertEqual(rej["total_realized_points_pnl"], 421.41)
        self.assertEqual(rej["expectancy_per_resolved_pts"], 42.14)
        self.assertEqual(rej["total_unrealized_reference_points_pnl"], -53.65)
        self.assertEqual(rej["contradiction_count"], 0)


if __name__ == "__main__":
    unittest.main()
