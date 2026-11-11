"""
TradeLens AI - Phase 3E.4.1 Dedicated Adversarial Test Suite:
Canonical Exit-State Consistency & Classification Audit.

Tests all 12 adversarial conditions plus tracker observation-timestamp semantics
and dataset invariance:
1.  Record-level actual-exit flag True, metadata False.
2.  Record-level actual-exit flag False, metadata True.
3.  Both representations missing (plus non-boolean coercion rejection).
4.  Genuine terminal classification with missing exit timestamp.
5.  Terminal classification with missing realized P&L.
6.  Non-execution classification with exit price (or exit timestamp) populated.
7.  Non-execution classification with realized P&L populated.
8.  Unresolved classification with an actual-exit flag.
9.  Unknown / unsupported outcome classification.
10. Contradictory records excluded from both realized numerator and denominator.
11. Contradictions reported separately from ordinary unresolved observations.
12. Existing accepted headline metrics remain invariant to rejected counterfactual mutations.
13. Tracker emits observation_timestamp (with exit_timestamp=None) on non-execution observations.
14. Nov 2-6, 2026 NIFTY replay & manifest provenance remain unchanged.
"""

import json
import unittest
from datetime import datetime
from typing import Any, Dict, Optional
from unittest.mock import PropertyMock, patch

from backend.app.data.session_clock import IST_ZONE
from backend.app.data.historical_spot_store import HistoricalSpotStore, HistoricalSpotCandle
from backend.app.core.spot_replay_engine import HistoricalSpotReplayEngine, SpotReplayEvent
from backend.app.core.spot_outcome_tracker import (
    SpotOutcomeTracker,
    SpotOutcomeAttributor,
    SpotOutcomeRecord,
    SpotOutcomeType,
    OverlappingSignalPolicy,
    TerminalCandleExcursionStatus,
    ExitReason,
)


class _DualStateRecordWrapper:
    """
    Duck-typed / subclassed adversarial record proxy allowing independent control
    of the record-level `actual_exit_occurred` attribute and `execution_metadata`.
    Used to prove SpotOutcomeAttributor never resolves conflicts via boolean OR.
    """

    def __init__(self, base: SpotOutcomeRecord, prop_override: Any):
        self._base = base
        self.actual_exit_occurred = prop_override

    def __getattr__(self, item: str) -> Any:
        return getattr(self._base, item)


class TestCanonicalExitStateConsistencyAudit(unittest.TestCase):

    def _make_record(
        self,
        record_id: str = "rec_test",
        classification: str = SpotOutcomeType.TARGET_REACHED.value,
        meta_actual_exit: Any = True,
        include_meta_key: bool = True,
        exit_price: Optional[float] = 24100.0,
        exit_timestamp: Optional[str] = "2026-11-02T09:40:00+05:30",
        underlying_points_pnl: Optional[float] = 100.0,
        last_observed_reference_price: Optional[float] = None,
        last_observed_timestamp: Optional[str] = None,
        unrealized_reference_points_pnl: Optional[float] = None,
        observation_timestamp: Optional[str] = None,
        exit_reason: str = ExitReason.TARGET_HIT.value,
        is_counterfactual: bool = False,
        canonical_gates_passed: bool = True,
        meta_outcome_type: Optional[str] = None,
    ) -> SpotOutcomeRecord:
        exec_meta: Dict[str, Any] = {"underlying_signal_only": True}
        if include_meta_key:
            exec_meta["actual_exit_occurred"] = meta_actual_exit
        if meta_outcome_type is not None:
            exec_meta["outcome_type"] = meta_outcome_type

        is_entered = classification not in (
            SpotOutcomeType.ENTRY_INVALIDATED.value,
            SpotOutcomeType.MISSED_ENTRY.value,
            SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value,
        )

        return SpotOutcomeRecord(
            record_id=record_id,
            signal_id=f"sig_{record_id}",
            signal_timestamp="2026-11-02T09:30:00+05:30",
            direction="BULLISH",
            entry_reference_price=24000.0 if is_entered else None,
            entry_timestamp="2026-11-02T09:30:00+05:30" if is_entered else None,
            entry_convention="NEXT_BAR_OPEN",
            slippage_points=0.0,
            structural_stop_loss=23950.0,
            structural_target=24100.0,
            initial_risk_points=50.0 if is_entered else None,
            first_eligible_candle_timestamp="2026-11-02T09:30:00+05:30",
            exit_timestamp=exit_timestamp,
            exit_price=exit_price,
            exit_reason=exit_reason,
            outcome_classification=classification,
            mfe_points=10.0,
            mae_points=5.0,
            mfe_r_multiple=0.2,
            mae_r_multiple=0.1,
            terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
            terminal_candle_max_favourable_bound=10.0,
            terminal_candle_max_adverse_bound=5.0,
            underlying_points_pnl=underlying_points_pnl,
            bars_held=2 if is_entered else 0,
            setup_score=80.0,
            canonical_gates_passed=canonical_gates_passed,
            is_counterfactual=is_counterfactual,
            failed_gates=[] if canonical_gates_passed else ["GATE_1"],
            gate_rejection_reasons=[] if canonical_gates_passed else ["Gate 1 failed"],
            data_quality_flags=[],
            execution_metadata=exec_meta,
            last_observed_reference_price=last_observed_reference_price,
            last_observed_timestamp=last_observed_timestamp,
            unrealized_reference_points_pnl=unrealized_reference_points_pnl,
            observation_timestamp=observation_timestamp,
        )

    def _make_event(
        self,
        event_id: str = "evt_001",
        timestamp: str = "2026-11-02T09:29:00+05:30",
        direction: str = "BULLISH",
        entry: Optional[float] = 24000.0,
        stop: Optional[float] = 23950.0,
        target: Optional[float] = 24100.0,
        gates_passed: bool = True,
    ) -> SpotReplayEvent:
        return SpotReplayEvent(
            event_id=event_id,
            step_index=1,
            replay_timestamp=timestamp,
            symbol="NIFTY",
            current_market_price=entry or 24000.0,
            last_closed_candle_timestamps={},
            closed_candle_counts={},
            market_regime_5m="BULLISH_TREND",
            market_regime_15m="BULLISH_TREND",
            confirmed_pivots_count_5m=10,
            confirmed_pivots_count_15m=5,
            latest_confirmed_bos_choch="VALID",
            setup_score_bullish=80.0 if direction == "BULLISH" else 0.0,
            setup_score_bearish=80.0 if direction == "BEARISH" else 0.0,
            candidate_direction=direction,
            canonical_eight_gates_status=gates_passed,
            failed_gates=[] if gates_passed else ["GATE_1"],
            structural_entry=entry,
            structural_stop_loss=stop,
            structural_target=target,
            underlying_risk_reward=2.0,
            underlying_risk_reward_status="AVAILABLE",
            option_execution_eligible=False,
            option_execution_status="UNAVAILABLE",
            underlying_signal_only=True,
            rejection_reasons=[] if gates_passed else ["Gate 1 failed"],
            data_quality_flags=["VALID"],
            execution_time_ms=1.0,
        )

    # ------------------------------------------------------------------------
    # Source Single-Source-of-Truth Guarantee Check
    # ------------------------------------------------------------------------
    def test_00_spot_outcome_record_property_is_read_only_single_source_of_truth(self):
        rec = self._make_record(meta_actual_exit=True)
        self.assertIs(rec.actual_exit_occurred, True)
        with self.assertRaises(AttributeError):
            rec.actual_exit_occurred = False  # type: ignore[misc]

        rec_false = self._make_record(
            classification=SpotOutcomeType.UNRESOLVED.value,
            meta_actual_exit=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
        )
        self.assertIs(rec_false.actual_exit_occurred, False)

    # ------------------------------------------------------------------------
    # Adversarial Test 1: Record-level actual-exit flag True, metadata False
    # ------------------------------------------------------------------------
    def test_01_record_flag_true_metadata_false_rejected(self):
        base = self._make_record(
            record_id="adv_01",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit=False,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0,
        )
        # Case A: Wrapper / duck-typed record with record-level True vs metadata False
        conflict_rec = _DualStateRecordWrapper(base, prop_override=True)

        canonical, state_issues = SpotOutcomeAttributor.resolve_canonical_actual_exit_state(conflict_rec)
        self.assertIsNone(canonical)
        self.assertTrue(any("conflicting actual_exit_occurred state" in msg for msg in state_issues))

        issues = SpotOutcomeAttributor.check_record_contradictions(conflict_rec)  # type: ignore[arg-type]
        self.assertTrue(any("conflicting actual_exit_occurred state" in msg for msg in issues))

        metrics = SpotOutcomeAttributor.calculate_metrics([conflict_rec])  # type: ignore[list-item]
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
        self.assertEqual(metrics["contradictory_records_count"], 1)
        self.assertGreaterEqual(metrics["contradiction_count"], 1)

        # Case B: PropertyMock patching SpotOutcomeRecord.actual_exit_occurred directly
        with patch.object(SpotOutcomeRecord, "actual_exit_occurred", new_callable=PropertyMock, return_value=True):
            metrics_mocked = SpotOutcomeAttributor.calculate_metrics([base])
            self.assertEqual(metrics_mocked["resolved_observations"], 0)
            self.assertEqual(metrics_mocked["contradictory_records_count"], 1)
            self.assertTrue(any("conflicting actual_exit_occurred state" in m for m in metrics_mocked["contradictions"]))

    # ------------------------------------------------------------------------
    # Adversarial Test 2: Record-level actual-exit flag False, metadata True
    # ------------------------------------------------------------------------
    def test_02_record_flag_false_metadata_true_rejected(self):
        base = self._make_record(
            record_id="adv_02",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit=True,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0,
        )
        conflict_rec = _DualStateRecordWrapper(base, prop_override=False)

        canonical, state_issues = SpotOutcomeAttributor.resolve_canonical_actual_exit_state(conflict_rec)
        self.assertIsNone(canonical)
        self.assertTrue(any("conflicting actual_exit_occurred state" in msg for msg in state_issues))

        metrics = SpotOutcomeAttributor.calculate_metrics([conflict_rec])  # type: ignore[list-item]
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["denominators"]["resolved_denominator"], 0)
        self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
        self.assertEqual(metrics["contradictory_records_count"], 1)

        with patch.object(SpotOutcomeRecord, "actual_exit_occurred", new_callable=PropertyMock, return_value=False):
            metrics_mocked = SpotOutcomeAttributor.calculate_metrics([base])
            self.assertEqual(metrics_mocked["resolved_observations"], 0)
            self.assertEqual(metrics_mocked["contradictory_records_count"], 1)

    # ------------------------------------------------------------------------
    # Adversarial Test 3: Both representations missing (and non-boolean coercion)
    # ------------------------------------------------------------------------
    def test_03_both_representations_missing_or_non_boolean_rejected(self):
        # Missing on a terminal record
        rec_term_missing = self._make_record(
            record_id="adv_03_term",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            include_meta_key=False,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0,
        )
        self.assertIsNone(rec_term_missing.actual_exit_occurred)
        canonical_t, issues_t = SpotOutcomeAttributor.resolve_canonical_actual_exit_state(rec_term_missing)
        self.assertIsNone(canonical_t)
        self.assertTrue(any("missing" in msg for msg in issues_t))

        # Missing on an UNRESOLVED record (must NOT silently default to False!)
        rec_unres_missing = self._make_record(
            record_id="adv_03_unres",
            classification=SpotOutcomeType.UNRESOLVED.value,
            include_meta_key=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
        )
        self.assertIsNone(rec_unres_missing.actual_exit_occurred)
        issues_u = SpotOutcomeAttributor.check_record_contradictions(rec_unres_missing)
        self.assertTrue(any("missing" in msg for msg in issues_u))

        # Non-boolean string value "false" or int 1 must be rejected, not truthy-coerced
        rec_non_bool = self._make_record(
            record_id="adv_03_str",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit="false",
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0,
        )
        self.assertIsNone(rec_non_bool.actual_exit_occurred)
        issues_nb = SpotOutcomeAttributor.check_record_contradictions(rec_non_bool)
        self.assertTrue(any("must be bool" in msg for msg in issues_nb))

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_term_missing, rec_unres_missing, rec_non_bool])
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["unresolved_count"], 0)
        self.assertEqual(metrics["contradictory_records_count"], 3)

    # ------------------------------------------------------------------------
    # Adversarial Test 4: Terminal classification with missing exit timestamp
    # ------------------------------------------------------------------------
    def test_04_terminal_classification_missing_exit_timestamp(self):
        for cls_type, reason in [
            (SpotOutcomeType.TARGET_REACHED.value, ExitReason.TARGET_HIT.value),
            (SpotOutcomeType.STOP_LOSS_HIT.value, ExitReason.STOP_LOSS_HIT.value),
            (SpotOutcomeType.TIME_EXIT.value, ExitReason.SESSION_TIME_EXIT.value),
        ]:
            rec_none = self._make_record(
                record_id=f"adv_04_none_{cls_type}",
                classification=cls_type,
                meta_actual_exit=True,
                exit_price=24100.0,
                exit_timestamp=None,
                underlying_points_pnl=100.0,
                exit_reason=reason,
            )
            rec_blank = self._make_record(
                record_id=f"adv_04_blank_{cls_type}",
                classification=cls_type,
                meta_actual_exit=True,
                exit_price=24100.0,
                exit_timestamp="   ",
                underlying_points_pnl=100.0,
                exit_reason=reason,
            )
            self.assertTrue(any("exit_timestamp" in i for i in SpotOutcomeAttributor.check_record_contradictions(rec_none)))
            self.assertTrue(any("exit_timestamp" in i for i in SpotOutcomeAttributor.check_record_contradictions(rec_blank)))

            metrics = SpotOutcomeAttributor.calculate_metrics([rec_none, rec_blank])
            self.assertEqual(metrics["resolved_observations"], 0)
            self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
            self.assertEqual(metrics["contradictory_records_count"], 2)

    # ------------------------------------------------------------------------
    # Adversarial Test 5: Terminal classification with missing realized P&L
    # ------------------------------------------------------------------------
    def test_05_terminal_classification_missing_realized_pnl(self):
        for cls_type, reason in [
            (SpotOutcomeType.TARGET_REACHED.value, ExitReason.TARGET_HIT.value),
            (SpotOutcomeType.STOP_LOSS_HIT.value, ExitReason.STOP_LOSS_HIT.value),
            (SpotOutcomeType.TIME_EXIT.value, ExitReason.SESSION_TIME_EXIT.value),
        ]:
            rec = self._make_record(
                record_id=f"adv_05_{cls_type}",
                classification=cls_type,
                meta_actual_exit=True,
                exit_price=24100.0,
                exit_timestamp="2026-11-02T09:40:00+05:30",
                underlying_points_pnl=None,
                exit_reason=reason,
            )
            issues = SpotOutcomeAttributor.check_record_contradictions(rec)
            self.assertTrue(any("underlying_points_pnl is None" in i for i in issues))
            metrics = SpotOutcomeAttributor.calculate_metrics([rec])
            self.assertEqual(metrics["resolved_observations"], 0)
            self.assertEqual(metrics["contradictory_records_count"], 1)

    # ------------------------------------------------------------------------
    # Adversarial Test 6: Non-execution classification with exit price (or exit timestamp) populated
    # ------------------------------------------------------------------------
    def test_06_non_execution_with_exit_price_or_exit_timestamp_populated(self):
        non_exec_types = [
            (SpotOutcomeType.ENTRY_INVALIDATED.value, ExitReason.OPEN_BEYOND_STOP.value),
            (SpotOutcomeType.MISSED_ENTRY.value, ExitReason.OPEN_BEYOND_TARGET.value),
            (SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value, ExitReason.MISSING_STRUCTURAL_LEVELS.value),
            (SpotOutcomeType.UNRESOLVED.value, ExitReason.SESSION_END_UNRESOLVED.value),
        ]
        for cls_type, reason in non_exec_types:
            rec_price = self._make_record(
                record_id=f"adv_06_price_{cls_type}",
                classification=cls_type,
                meta_actual_exit=False,
                exit_price=24000.0,
                exit_timestamp=None,
                underlying_points_pnl=None,
                exit_reason=reason,
            )
            issues_p = SpotOutcomeAttributor.check_record_contradictions(rec_price)
            self.assertTrue(any("exit_price is not None" in i for i in issues_p), f"Failed for {cls_type}")

            rec_ts = self._make_record(
                record_id=f"adv_06_ts_{cls_type}",
                classification=cls_type,
                meta_actual_exit=False,
                exit_price=None,
                exit_timestamp="2026-11-02T09:30:00+05:30",
                underlying_points_pnl=None,
                exit_reason=reason,
            )
            issues_ts = SpotOutcomeAttributor.check_record_contradictions(rec_ts)
            self.assertTrue(any("must not contain exit_timestamp" in i for i in issues_ts), f"Failed for {cls_type}")

            metrics = SpotOutcomeAttributor.calculate_metrics([rec_price, rec_ts])
            self.assertEqual(metrics["resolved_observations"], 0)
            self.assertEqual(metrics["eligible_observations"], 0)
            self.assertEqual(metrics["contradictory_records_count"], 2)

    # ------------------------------------------------------------------------
    # Adversarial Test 7: Non-execution classification with realized P&L populated
    # ------------------------------------------------------------------------
    def test_07_non_execution_with_realized_pnl_populated(self):
        non_exec_types = [
            (SpotOutcomeType.ENTRY_INVALIDATED.value, ExitReason.OPEN_BEYOND_STOP.value),
            (SpotOutcomeType.MISSED_ENTRY.value, ExitReason.OPEN_BEYOND_TARGET.value),
            (SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value, ExitReason.SKIPPED_DUE_TO_ACTIVE_POSITION.value),
            (SpotOutcomeType.UNRESOLVED.value, ExitReason.DATASET_END_UNRESOLVED.value),
        ]
        for cls_type, reason in non_exec_types:
            rec = self._make_record(
                record_id=f"adv_07_{cls_type}",
                classification=cls_type,
                meta_actual_exit=False,
                exit_price=None,
                exit_timestamp=None,
                underlying_points_pnl=75.0,
                exit_reason=reason,
            )
            issues = SpotOutcomeAttributor.check_record_contradictions(rec)
            self.assertTrue(any("underlying_points_pnl is not None" in i for i in issues), f"Failed for {cls_type}")

            metrics = SpotOutcomeAttributor.calculate_metrics([rec])
            self.assertEqual(metrics["resolved_observations"], 0)
            self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
            self.assertEqual(metrics["contradictory_records_count"], 1)

    # ------------------------------------------------------------------------
    # Adversarial Test 8: Unresolved classification with an actual-exit flag
    # ------------------------------------------------------------------------
    def test_08_unresolved_with_actual_exit_flag_rejected(self):
        rec_unres_exit_true = self._make_record(
            record_id="adv_08",
            classification=SpotOutcomeType.UNRESOLVED.value,
            meta_actual_exit=True,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            last_observed_reference_price=24050.0,
            last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=50.0,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
        )
        issues = SpotOutcomeAttributor.check_record_contradictions(rec_unres_exit_true)
        self.assertTrue(any("actual_exit_occurred is True" in i for i in issues))

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_unres_exit_true])
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["unresolved_count"], 0)
        self.assertEqual(metrics["total_unrealized_reference_points_pnl"], 0.0)
        self.assertEqual(metrics["contradictory_records_count"], 1)

    # ------------------------------------------------------------------------
    # Adversarial Test 9: Unknown outcome classification
    # ------------------------------------------------------------------------
    def test_09_unknown_outcome_classification_rejected(self):
        rec_unknown = self._make_record(
            record_id="adv_09_unknown",
            classification="PARTIAL_FILL_SIMULATED",
            meta_actual_exit=True,
            exit_price=24100.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=100.0,
        )
        rec_empty = self._make_record(
            record_id="adv_09_empty",
            classification="",
            meta_actual_exit=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
        )
        self.assertTrue(any("unsupported outcome_classification" in i for i in SpotOutcomeAttributor.check_record_contradictions(rec_unknown)))
        self.assertTrue(any("unsupported outcome_classification" in i for i in SpotOutcomeAttributor.check_record_contradictions(rec_empty)))

        metrics = SpotOutcomeAttributor.calculate_metrics([rec_unknown, rec_empty])
        self.assertEqual(metrics["resolved_observations"], 0)
        self.assertEqual(metrics["eligible_observations"], 0)
        self.assertEqual(metrics["total_realized_points_pnl"], 0.0)
        self.assertEqual(metrics["contradictory_records_count"], 2)

    # ------------------------------------------------------------------------
    # Adversarial Test 10: Contradictory records excluded from numerator & denominator
    # ------------------------------------------------------------------------
    def test_10_contradictory_records_excluded_from_realized_numerator_and_denominator(self):
        valid_win = self._make_record(
            record_id="valid_win",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit=True,
            exit_price=24060.0,
            exit_timestamp="2026-11-02T09:40:00+05:30",
            underlying_points_pnl=60.0,
        )
        # Contradictory records carrying huge +/- P&L that must NOT enter numerator or denominator
        bad_huge_win = self._make_record(
            record_id="bad_huge_win",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit=False,
            exit_price=25000.0,
            exit_timestamp="2026-11-02T09:45:00+05:30",
            underlying_points_pnl=1000.0,
        )
        bad_huge_loss = self._make_record(
            record_id="bad_huge_loss",
            classification=SpotOutcomeType.STOP_LOSS_HIT.value,
            meta_actual_exit=True,
            exit_price=23500.0,
            exit_timestamp=None,  # missing exit timestamp
            underlying_points_pnl=-500.0,
            exit_reason=ExitReason.STOP_LOSS_HIT.value,
        )
        bad_unknown = self._make_record(
            record_id="bad_unknown",
            classification="UNKNOWN_CLASSIFICATION",
            meta_actual_exit=True,
            exit_price=24500.0,
            exit_timestamp="2026-11-02T10:00:00+05:30",
            underlying_points_pnl=500.0,
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([valid_win, bad_huge_win, bad_huge_loss, bad_unknown])

        self.assertEqual(metrics["total_observations"], 4)
        self.assertEqual(metrics["contradictory_records_count"], 3)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["actual_resolved_exits_count"], 1)
        self.assertEqual(metrics["eligible_observations"], 1)
        self.assertEqual(metrics["denominators"]["resolved_denominator"], 1)
        self.assertEqual(metrics["denominators"]["eligible_denominator"], 1)
        self.assertEqual(metrics["denominators"]["observed_denominator"], 4)
        self.assertEqual(metrics["total_realized_points_pnl"], 60.0)
        self.assertEqual(metrics["expectancy_per_resolved_pts"], 60.0)
        self.assertEqual(metrics["expectancy_per_eligible_pts"], 60.0)
        self.assertEqual(metrics["target_rate_pct_of_resolved"], 100.0)

    # ------------------------------------------------------------------------
    # Adversarial Test 11: Contradictions reported separately from ordinary unresolved observations
    # ------------------------------------------------------------------------
    def test_11_contradictions_reported_separately_from_ordinary_unresolved(self):
        valid_resolved = self._make_record(
            record_id="valid_res",
            classification=SpotOutcomeType.TARGET_REACHED.value,
            meta_actual_exit=True,
            exit_price=24080.0,
            exit_timestamp="2026-11-02T10:00:00+05:30",
            underlying_points_pnl=80.0,
        )
        ordinary_unresolved = self._make_record(
            record_id="ord_unres",
            classification=SpotOutcomeType.UNRESOLVED.value,
            meta_actual_exit=False,
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            last_observed_reference_price=24015.0,
            last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=15.0,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
        )
        contradictory_unresolved_1 = self._make_record(
            record_id="bad_unres_1",
            classification=SpotOutcomeType.UNRESOLVED.value,
            meta_actual_exit=True,  # Invalid for UNRESOLVED
            exit_price=None,
            exit_timestamp=None,
            underlying_points_pnl=None,
            last_observed_reference_price=24500.0,
            last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=500.0,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
        )
        contradictory_unresolved_2 = self._make_record(
            record_id="bad_unres_2",
            classification=SpotOutcomeType.UNRESOLVED.value,
            meta_actual_exit=False,
            exit_price=23900.0,  # Invalid for UNRESOLVED
            exit_timestamp="2026-11-02T15:15:00+05:30",  # Invalid for UNRESOLVED
            underlying_points_pnl=-100.0,  # Invalid for UNRESOLVED
            last_observed_reference_price=23900.0,
            last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=-100.0,
            exit_reason=ExitReason.DATASET_END_UNRESOLVED.value,
        )

        metrics = SpotOutcomeAttributor.calculate_metrics([
            valid_resolved,
            ordinary_unresolved,
            contradictory_unresolved_1,
            contradictory_unresolved_2,
        ])

        self.assertEqual(metrics["unresolved_count"], 1)
        self.assertEqual(metrics["session_end_unresolved_count"], 1)
        self.assertEqual(metrics["dataset_end_unresolved_count"], 0)
        self.assertEqual(metrics["total_unrealized_reference_points_pnl"], 15.0)
        self.assertEqual(metrics["contradictory_records_count"], 2)
        self.assertGreaterEqual(metrics["contradiction_count"], 4)
        self.assertEqual(metrics["resolved_observations"], 1)
        self.assertEqual(metrics["eligible_observations"], 2)
        self.assertEqual(metrics["total_realized_points_pnl"], 80.0)

    # ------------------------------------------------------------------------
    # Adversarial Test 12: Accepted headline metrics invariant to counterfactual mutations
    # ------------------------------------------------------------------------
    def test_12_accepted_headline_metrics_invariant_to_counterfactual_contradictions_and_mutations(self):
        accepted = [
            self._make_record(
                record_id="acc_1",
                classification=SpotOutcomeType.TARGET_REACHED.value,
                meta_actual_exit=True,
                exit_price=24067.6,
                exit_timestamp="2026-11-05T12:37:00+05:30",
                underlying_points_pnl=67.6,
                is_counterfactual=False,
                canonical_gates_passed=True,
            )
        ]

        cf_clean = [
            self._make_record(
                record_id="cf_1",
                classification=SpotOutcomeType.STOP_LOSS_HIT.value,
                meta_actual_exit=True,
                exit_price=23950.0,
                exit_timestamp="2026-11-04T10:00:00+05:30",
                underlying_points_pnl=-50.0,
                exit_reason=ExitReason.STOP_LOSS_HIT.value,
                is_counterfactual=True,
                canonical_gates_passed=False,
            )
        ]

        cf_corrupted = [
            self._make_record(
                record_id="cf_bad_1",
                classification="BOGUS_CLASS",
                meta_actual_exit=True,
                exit_price=25000.0,
                exit_timestamp="2026-11-04T10:00:00+05:30",
                underlying_points_pnl=9999.0,
                is_counterfactual=True,
                canonical_gates_passed=False,
            ),
            _DualStateRecordWrapper(
                self._make_record(
                    record_id="cf_bad_2",
                    classification=SpotOutcomeType.TARGET_REACHED.value,
                    meta_actual_exit=False,
                    exit_price=24500.0,
                    exit_timestamp="2026-11-04T11:00:00+05:30",
                    underlying_points_pnl=500.0,
                    is_counterfactual=True,
                    canonical_gates_passed=False,
                ),
                prop_override=True,
            ),
        ]

        rep_clean = SpotOutcomeAttributor.generate_full_attribution_report(accepted + cf_clean)
        rep_corrupted = SpotOutcomeAttributor.generate_full_attribution_report(accepted + cf_corrupted)  # type: ignore[arg-type]

        self.assertEqual(rep_clean["headline_performance_summary"], rep_corrupted["headline_performance_summary"])
        self.assertEqual(rep_clean["accepted_signal_observations_summary"], rep_corrupted["accepted_signal_observations_summary"])
        self.assertEqual(rep_corrupted["headline_performance_summary"]["contradiction_count"], 0)
        self.assertEqual(rep_corrupted["headline_performance_summary"]["total_realized_points_pnl"], 67.6)
        self.assertGreater(rep_corrupted["rejected_counterfactual_summary"]["contradiction_count"], 0)

    # ------------------------------------------------------------------------
    # Test 13: Tracker separates observation_timestamp from exit_timestamp on non-execution records
    # ------------------------------------------------------------------------
    def test_13_tracker_non_execution_uses_observation_timestamp_with_none_exit_timestamp(self):
        # A. ENTRY_INVALIDATED & MISSED_ENTRY
        tracker = SpotOutcomeTracker()
        evt_inv = self._make_event(event_id="evt_inv", timestamp="2026-11-02T09:29:00+05:30", entry=24000.0, stop=23950.0, target=24100.0)
        evt_miss = self._make_event(event_id="evt_miss", timestamp="2026-11-02T09:29:00+05:30", entry=24000.0, stop=23950.0, target=24020.0)
        tracker.register_signal(evt_inv)
        # Candle opens at 23940 (below evt_inv stop 23950 -> ENTRY_INVALIDATED)
        c_inv = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 9, 30, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 9, 31, tzinfo=IST_ZONE),
            open=23940.0, high=23960.0, low=23930.0, close=23950.0,
        )
        recs_inv = tracker.process_candle(c_inv)
        self.assertEqual(len(recs_inv), 1)
        r_inv = recs_inv[0]
        self.assertEqual(r_inv.outcome_classification, SpotOutcomeType.ENTRY_INVALIDATED.value)
        self.assertIsNone(r_inv.exit_timestamp)
        self.assertIsNone(r_inv.exit_price)
        self.assertIsNone(r_inv.underlying_points_pnl)
        self.assertEqual(r_inv.observation_timestamp, "2026-11-02T09:30:00+05:30")
        self.assertEqual(r_inv.execution_metadata["observation_timestamp"], "2026-11-02T09:30:00+05:30")
        self.assertEqual(SpotOutcomeAttributor.check_record_contradictions(r_inv), [])

        tracker.register_signal(evt_miss)
        c_miss = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 9, 31, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 9, 32, tzinfo=IST_ZONE),
            open=24030.0, high=24040.0, low=24025.0, close=24035.0,
        )
        recs_miss = tracker.process_candle(c_miss)
        self.assertEqual(len(recs_miss), 1)
        r_miss = recs_miss[0]
        self.assertEqual(r_miss.outcome_classification, SpotOutcomeType.MISSED_ENTRY.value)
        self.assertIsNone(r_miss.exit_timestamp)
        self.assertEqual(r_miss.observation_timestamp, "2026-11-02T09:31:00+05:30")
        self.assertEqual(SpotOutcomeAttributor.check_record_contradictions(r_miss), [])

        # B. EXCLUDED_NON_ELIGIBLE (MISSING_STRUCTURAL_LEVELS & SKIPPED_DUE_TO_ACTIVE_POSITION)
        tracker_seq = SpotOutcomeTracker(overlapping_policy=OverlappingSignalPolicy.SINGLE_ACTIVE_SEQUENCE)
        evt_bad_levels = self._make_event(
            event_id="evt_bad_lvl",
            timestamp="2026-11-02T09:35:00+05:30",
            entry=24000.0, stop=24050.0, target=24100.0,  # Invalid bullish stop > entry
            gates_passed=False,
        )
        tracker_seq.register_signal(evt_bad_levels)
        r_bad_lvl = tracker_seq.completed_records[0]
        self.assertEqual(r_bad_lvl.outcome_classification, SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value)
        self.assertIsNone(r_bad_lvl.exit_timestamp)
        self.assertEqual(r_bad_lvl.observation_timestamp, "2026-11-02T09:35:00+05:30")
        self.assertEqual(SpotOutcomeAttributor.check_record_contradictions(r_bad_lvl), [])

        evt_ok = self._make_event(event_id="evt_ok", timestamp="2026-11-02T09:40:00+05:30")
        evt_skip = self._make_event(event_id="evt_skip", timestamp="2026-11-02T09:41:00+05:30")
        tracker_seq.register_signal(evt_ok)
        tracker_seq.register_signal(evt_skip)
        r_skip = tracker_seq.completed_records[-1]
        self.assertEqual(r_skip.outcome_classification, SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value)
        self.assertEqual(r_skip.exit_reason, ExitReason.SKIPPED_DUE_TO_ACTIVE_POSITION.value)
        self.assertIsNone(r_skip.exit_timestamp)
        self.assertEqual(r_skip.observation_timestamp, "2026-11-02T09:41:00+05:30")
        self.assertEqual(SpotOutcomeAttributor.check_record_contradictions(r_skip), [])

    # ------------------------------------------------------------------------
    # Test 14: Nov 2-6 NIFTY Replay & Manifest Provenance Unchanged
    # ------------------------------------------------------------------------
    def test_14_nov2_to_nov6_replay_and_manifest_unchanged(self):
        with open("data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json") as f:
            mf = json.load(f)
        self.assertEqual(mf["start_timestamp_ist"], "2026-11-02T09:15:00+05:30")
        self.assertEqual(mf["end_timestamp_ist"], "2026-11-06T15:30:00+05:30")
        self.assertEqual(mf["record_count"], 1805)
        self.assertEqual(mf["isolated_post_gap_count"], 5)

        store = HistoricalSpotStore(base_dir="data/historical")
        engine = HistoricalSpotReplayEngine(store)
        tracker = SpotOutcomeTracker(track_rejected_signals=True)
        list(engine.run_replay(symbol="NIFTY", outcome_tracker=tracker, evaluate_on_timeframe="5m"))

        rep = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        acc = rep["headline_performance_summary"]
        rej = rep["rejected_counterfactual_summary"]

        self.assertEqual(acc["total_observations"], 1)
        self.assertEqual(acc["resolved_observations"], 1)
        self.assertEqual(acc["total_realized_points_pnl"], 67.6)
        self.assertEqual(acc["contradiction_count"], 0)
        self.assertEqual(acc["contradictory_records_count"], 0)

        self.assertEqual(rej["total_observations"], 13)
        self.assertEqual(rej["resolved_observations"], 10)
        self.assertEqual(rej["unresolved_count"], 3)
        self.assertEqual(rej["total_realized_points_pnl"], 421.41)
        self.assertEqual(rej["total_unrealized_reference_points_pnl"], -53.65)
        self.assertEqual(rej["contradiction_count"], 0)
        self.assertEqual(rej["contradictory_records_count"], 0)


if __name__ == "__main__":
    unittest.main()

