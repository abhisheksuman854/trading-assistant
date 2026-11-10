"""
TradeLens AI - Phase 3D.1 Comprehensive Test Suite: Outcome Integrity & Targeted Corrections.

Verifies:
1. Audit Item 1: Terminal-candle MFE/MAE integrity (intrabar sequence unknown, ambiguous bounds, stop-first rule).
2. Audit Item 2: Next-bar entry geometry validation (open beyond stop -> ENTRY_INVALIDATED, open beyond target -> MISSED_ENTRY, slippage breaches).
3. Audit Item 3: Rejected-signal counterfactuals (valid levels tracked as counterfactual, missing levels excluded, multi-gate attribution).
4. Audit Item 4: Overlapping signal policies (INDEPENDENT_SIGNAL_EVENTS vs SINGLE_ACTIVE_SEQUENCE, same and opposite direction).
5. Audit Item 5: Attribution denominators (explicit counts for observed, eligible, resolved, invalidated, missed; no zero-point pollution).
6. Audit Item 6: Time-exit cutoff correctness (15:25 IST fully closed candle, configurable research assumption).
7. Audit Item 7: Fill-model disclaimers and data quality handling.
8. End-to-end integration with HistoricalSpotReplayEngine.
"""

import unittest
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Dict, Any, Tuple
import tempfile
import shutil

from backend.app.data.session_clock import IST_ZONE
from backend.app.data.candle_store import Candle, Timeframe
from backend.app.data.historical_spot_store import HistoricalSpotStore, HistoricalSpotCandle, DataQualityFlag
from backend.app.core.spot_replay_engine import HistoricalSpotReplayEngine, SpotReplayEvent
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


class TestSpotOutcomeTrackerAudit(unittest.TestCase):

    def _create_synthetic_event(
        self,
        event_id: str = "evt_001",
        timestamp: str = "2026-11-09T09:30:00+05:30",
        direction: str = "BULLISH",
        entry: Optional[float] = 24000.0,
        stop: Optional[float] = 23950.0,
        target: Optional[float] = 24100.0,
        score: float = 75.0,
        gates_passed: bool = True,
        failed_gates: list = None,
        rejection_reasons: list = None,
        quality_flags: list = None
    ) -> SpotReplayEvent:
        rr = None
        if entry is not None and stop is not None and target is not None and abs(entry - stop) > 0:
            rr = round(abs(target - entry) / abs(entry - stop), 2)
        return SpotReplayEvent(
            event_id=event_id,
            step_index=15,
            replay_timestamp=timestamp,
            symbol="NIFTY",
            current_market_price=entry or 24000.0,
            last_closed_candle_timestamps={"5m": timestamp},
            closed_candle_counts={"5m": 3},
            market_regime_5m="TRENDING_BULLISH",
            market_regime_15m="TRENDING_BULLISH",
            confirmed_pivots_count_5m=2,
            confirmed_pivots_count_15m=1,
            latest_confirmed_bos_choch="VALID",
            setup_score_bullish=score if direction == "BULLISH" else 40.0,
            setup_score_bearish=score if direction == "BEARISH" else 40.0,
            candidate_direction=direction,
            canonical_eight_gates_status=gates_passed,
            failed_gates=failed_gates or [],
            structural_entry=entry,
            structural_stop_loss=stop,
            structural_target=target,
            underlying_risk_reward=rr,
            underlying_risk_reward_status="VALID" if rr is not None else "UNAVAILABLE",
            option_execution_eligible=False,
            option_execution_status="UNAVAILABLE",
            underlying_signal_only=True,
            rejection_reasons=rejection_reasons or [],
            data_quality_flags=quality_flags or [],
            execution_time_ms=15.0
        )

    def _create_1m_candle(
        self,
        start_str: str,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float
    ) -> Candle:
        start_dt = datetime.fromisoformat(start_str)
        end_dt = start_dt + timedelta(minutes=1)
        return Candle(
            symbol="NIFTY",
            timeframe=Timeframe.M1,
            start_time=start_dt,
            end_time=end_dt,
            open_price=open_p,
            high_price=high_p,
            low_price=low_p,
            close_price=close_p,
            volume=0,
            is_closed=True
        )

    # =========================================================================
    # AUDIT ITEM 1: Terminal-Candle MFE/MAE Integrity & Excursion Status
    # =========================================================================

    def test_terminal_candle_target_only_long(self):
        """
        Long target reached on terminal candle:
        - Confirmed pre-exit MFE reaches target distance.
        - Confirmed pre-exit MAE is strictly from preceding completed bars.
        - Terminal excursion status is marked INTRABAR_SEQUENCE_UNKNOWN.
        - Raw bar overshoot is stored in terminal_candle_max_favourable_bound.
        """
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        # Preceding bar (Bar 1): Open=24000, High=24040, Low=23980, Close=24030
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24040.0, 23980.0, 24030.0)
        tracker.process_candle(c1)
        self.assertEqual(tracker.active_trades_count, 1)

        # Terminal bar (Bar 2): Open=24030, High=24120 (overshoots target), Low=24010, Close=24105
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 24030.0, 24120.0, 24010.0, 24105.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.TARGET_REACHED.value)
        self.assertEqual(rec.exit_price, 24100.0)
        self.assertEqual(rec.underlying_points_pnl, 100.0)
        self.assertEqual(rec.mfe_points, 100.0)  # Distance to target confirmed
        self.assertEqual(rec.mae_points, 20.0)   # From Bar 1 (24000 - 23980)
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value)
        self.assertEqual(rec.terminal_candle_max_favourable_bound, 120.0)  # Raw high overshoot preserved as bound

    def test_terminal_candle_stop_only_long(self):
        """
        Long stop reached on terminal candle:
        - Confirmed pre-exit MFE is strictly from preceding completed bars.
        - Terminal bar rally (which might have occurred after stop) is NOT credited as confirmed MFE!
        """
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        # Preceding bar (Bar 1): High=24010, Low=23980 -> MFE=10, MAE=20
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24010.0, 23980.0, 23990.0)
        tracker.process_candle(c1)

        # Terminal bar (Bar 2): High=24060 (post-exit rally?), Low=23940 (stop breached)
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 23990.0, 24060.0, 23940.0, 23945.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.STOP_LOSS_HIT.value)
        self.assertEqual(rec.exit_price, 23950.0)
        self.assertEqual(rec.underlying_points_pnl, -50.0)
        self.assertEqual(rec.mfe_points, 10.0)   # Strictly from pre-exit Bar 1! Not 60.0!
        self.assertEqual(rec.mae_points, 50.0)   # Stop distance
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value)
        self.assertEqual(rec.terminal_candle_max_favourable_bound, 60.0)

    def test_terminal_candle_target_and_stop_short(self):
        """Symmetrical tests for Short setups (Target only and Stop only)."""
        tracker = SpotOutcomeTracker()
        evt_short = self._create_synthetic_event(direction="BEARISH", entry=24000.0, stop=24050.0, target=23900.0)
        tracker.register_signal(evt_short)

        # Bar 1: High=24010, Low=23960 -> MFE=40, MAE=10
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24010.0, 23960.0, 23970.0)
        tracker.process_candle(c1)

        # Bar 2 Target: Low=23880 (below target 23900)
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 23970.0, 23980.0, 23880.0, 23890.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.TARGET_REACHED.value)
        self.assertEqual(rec.exit_price, 23900.0)
        self.assertEqual(rec.underlying_points_pnl, 100.0)
        self.assertEqual(rec.mfe_points, 100.0)
        self.assertEqual(rec.mae_points, 10.0)
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value)

    def test_terminal_candle_simultaneous_stop_first_zero_target_credit(self):
        """
        When both stop and target are touched in the same candle:
        - Stop-first rule: stopped out at stop price.
        - Confirmed pre-exit MFE does NOT claim target!
        """
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(direction="BULLISH", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24020.0, 23990.0, 24010.0)
        tracker.process_candle(c1)

        # Bar 2 touches BOTH High=24150 and Low=23920
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 24010.0, 24150.0, 23920.0, 24050.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.STOP_LOSS_HIT.value)
        self.assertEqual(rec.exit_reason, ExitReason.SIMULTANEOUS_STOP_FIRST.value)
        self.assertEqual(rec.exit_price, 23950.0)
        self.assertEqual(rec.mfe_points, 20.0)   # From Bar 1 only! Target not credited
        self.assertEqual(rec.mae_points, 50.0)
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value)
        self.assertEqual(rec.terminal_candle_max_favourable_bound, 150.0)

    def test_terminal_candle_gap_through_stop_excludes_post_open_movement(self):
        """
        Gap-through stop at candle.open:
        - Exit occurs at open.
        - Any subsequent intrabar high or low is excluded from pre-exit excursion.
        - Excursion status is CONFIRMED_PRE_EXIT.
        """
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(direction="BULLISH", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24015.0, 23985.0, 23970.0)
        tracker.process_candle(c1)

        # Bar 2 opens at 23910 (gap down below stop). High bounces to 23960, Low dips to 23890
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 23910.0, 23960.0, 23890.0, 23930.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.STOP_LOSS_HIT.value)
        self.assertEqual(rec.exit_reason, ExitReason.GAP_THROUGH_STOP.value)
        self.assertEqual(rec.exit_price, 23910.0)  # Open price
        self.assertEqual(rec.underlying_points_pnl, -90.0)
        self.assertEqual(rec.mfe_points, 15.0)     # From Bar 1! 23960 bounce occurred after exit
        self.assertEqual(rec.mae_points, 90.0)     # 24000 - 23910
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value)

    # =========================================================================
    # AUDIT ITEM 2: Next-Bar Entry Geometry Validation
    # =========================================================================

    def test_entry_invalidated_open_beyond_or_equal_stop(self):
        """Candle open beyond or equal to stop -> ENTRY_INVALIDATED (excluded from resolved trades)."""
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(direction="BULLISH", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        # Next bar opens at 23940 (below stop)
        c_inv = self._create_1m_candle("2026-11-09T09:30:00+05:30", 23940.0, 23960.0, 23920.0, 23930.0)
        completed = tracker.process_candle(c_inv)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.ENTRY_INVALIDATED.value)
        self.assertEqual(rec.exit_reason, ExitReason.OPEN_BEYOND_STOP.value)
        self.assertIsNone(rec.entry_reference_price)
        self.assertIsNone(rec.exit_price)
        self.assertIsNone(rec.underlying_points_pnl)
        self.assertEqual(rec.bars_held, 0)
        self.assertEqual(tracker.active_trades_count, 0)
        self.assertEqual(tracker.entry_invalidated_count, 1)

    def test_missed_entry_open_beyond_or_equal_target(self):
        """Candle open beyond or equal to target -> MISSED_ENTRY (excluded from resolved trades)."""
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(direction="BULLISH", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(event)

        # Next bar opens at 24120 (above target)
        c_miss = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24120.0, 24140.0, 24110.0, 24130.0)
        completed = tracker.process_candle(c_miss)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.MISSED_ENTRY.value)
        self.assertEqual(rec.exit_reason, ExitReason.OPEN_BEYOND_TARGET.value)
        self.assertIsNone(rec.entry_reference_price)
        self.assertIsNone(rec.exit_price)
        self.assertIsNone(rec.underlying_points_pnl)
        self.assertEqual(tracker.missed_entry_count, 1)

    def test_entry_invalidated_slippage_breach(self):
        """Slippage moving effective entry across stop or target invalidates entry."""
        tracker = SpotOutcomeTracker(
            entry_convention=EntryExecutionConvention.NEXT_BAR_OPEN_WITH_SLIPPAGE,
            default_slippage_points=5.0
        )
        # Target is 24004.0, open is 24000.0. Slippage +5.0 puts effective entry at 24005.0 >= target!
        event = self._create_synthetic_event(direction="BULLISH", entry=24000.0, stop=23950.0, target=24004.0)
        tracker.register_signal(event)

        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24010.0, 23990.0, 24002.0)
        completed = tracker.process_candle(c1)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.MISSED_ENTRY.value)
        self.assertEqual(rec.exit_reason, ExitReason.SLIPPAGE_BREACH_TARGET.value)

    # =========================================================================
    # AUDIT ITEM 3: Rejected-Signal Counterfactuals
    # =========================================================================

    def test_rejected_signal_with_valid_levels_tracked_as_counterfactual(self):
        """
        Signals failing canonical gates with valid Phase 2C levels are tracked
        as clearly labeled hypothetical counterfactuals.
        """
        tracker = SpotOutcomeTracker(track_rejected_signals=True)
        evt_rejected = self._create_synthetic_event(
            entry=24000.0, stop=23950.0, target=24100.0,
            gates_passed=False,
            failed_gates=["GATE_1_SQUEEZE_COMPRESSION"]
        )
        accepted = tracker.register_signal(evt_rejected)
        self.assertTrue(accepted)

        # Bar 1 enters
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24030.0, 23990.0, 24020.0)
        tracker.process_candle(c1)
        active = tracker._active_trades[0]
        self.assertTrue(active.is_counterfactual)
        self.assertFalse(active.canonical_gates_passed)

    def test_rejected_signal_missing_levels_excluded_from_outcomes(self):
        """
        Rejected signals without valid structural levels are excluded (EXCLUDED_NON_ELIGIBLE)
        and never manufactured into fake levels.
        """
        tracker = SpotOutcomeTracker()
        # Invalid levels: Stop=24050 is ABOVE Entry=24000 for Bullish
        evt_invalid = self._create_synthetic_event(
            entry=24000.0, stop=24050.0, target=24100.0,
            gates_passed=False
        )
        accepted = tracker.register_signal(evt_invalid)
        self.assertFalse(accepted)

        # Record created as EXCLUDED_NON_ELIGIBLE
        records = tracker.completed_records
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value)
        self.assertEqual(rec.exit_reason, ExitReason.MISSING_STRUCTURAL_LEVELS.value)
        self.assertEqual(tracker.rejected_missing_levels_count, 1)

    def test_multi_gate_rejection_attribution(self):
        """
        When multiple gates fail simultaneously, by_failed_gate credits each failed gate.
        """
        tracker = SpotOutcomeTracker()
        evt_multi = self._create_synthetic_event(
            entry=24000.0, stop=23950.0, target=24100.0,
            gates_passed=False,
            failed_gates=["GATE_1_SQUEEZE_COMPRESSION", "GATE_3_HTF_TREND_ALIGNMENT"]
        )
        tracker.register_signal(evt_multi)
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24110.0, 23990.0, 24105.0)
        records = tracker.process_candle(c1)

        report = SpotOutcomeAttributor.generate_full_attribution_report(records)
        by_gate = report["by_failed_gate"]
        self.assertIn("GATE_1_SQUEEZE_COMPRESSION", by_gate)
        self.assertIn("GATE_3_HTF_TREND_ALIGNMENT", by_gate)
        self.assertEqual(by_gate["GATE_1_SQUEEZE_COMPRESSION"]["total_observations"], 1)
        self.assertEqual(by_gate["GATE_3_HTF_TREND_ALIGNMENT"]["total_observations"], 1)

    # =========================================================================
    # AUDIT ITEM 4: Overlapping Signal Policy
    # =========================================================================

    def test_overlapping_independent_policy(self):
        """Under INDEPENDENT_SIGNAL_EVENTS, multiple signals are tracked concurrently."""
        tracker = SpotOutcomeTracker(overlapping_policy=OverlappingSignalPolicy.INDEPENDENT_SIGNAL_EVENTS)
        e1 = self._create_synthetic_event(event_id="e1", timestamp="2026-11-09T09:30:00+05:30")
        e2 = self._create_synthetic_event(event_id="e2", timestamp="2026-11-09T09:30:00+05:30", direction="BEARISH", entry=24000.0, stop=24050.0, target=23900.0)

        tracker.register_signal(e1)
        tracker.register_signal(e2)

        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24020.0, 23980.0, 24010.0)
        tracker.process_candle(c1)

        self.assertEqual(tracker.active_trades_count, 2)

    def test_overlapping_single_active_sequence_policy(self):
        """
        Under SINGLE_ACTIVE_SEQUENCE, subsequent signals while a trade is active
        are recorded as EXCLUDED_NON_ELIGIBLE (SKIPPED_DUE_TO_ACTIVE_POSITION).
        """
        tracker = SpotOutcomeTracker(overlapping_policy=OverlappingSignalPolicy.SINGLE_ACTIVE_SEQUENCE)
        e1 = self._create_synthetic_event(event_id="e1", timestamp="2026-11-09T09:30:00+05:30")
        e2 = self._create_synthetic_event(event_id="e2", timestamp="2026-11-09T09:31:00+05:30")

        tracker.register_signal(e1)
        # Bar 1 activates e1
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24020.0, 23990.0, 24010.0)
        tracker.process_candle(c1)
        self.assertEqual(tracker.active_trades_count, 1)

        # e2 arrives while e1 is active
        accepted = tracker.register_signal(e2)
        self.assertFalse(accepted)
        self.assertEqual(tracker.active_trades_count, 1)

        # Verify e2 is recorded as excluded
        records = tracker.completed_records
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].signal_id, "e2")
        self.assertEqual(records[0].outcome_classification, SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value)
        self.assertEqual(records[0].exit_reason, ExitReason.SKIPPED_DUE_TO_ACTIVE_POSITION.value)

    # =========================================================================
    # AUDIT ITEM 5: Attribution Denominators
    # =========================================================================

    def test_attribution_explicit_denominators(self):
        """
        Verifies exact separation of denominators:
        - Observed (all signals)
        - Eligible (excludes invalid/missed/excluded)
        - Resolved (excludes unresolved)
        Confirms unresolved and invalid entries do not pollute resolved trade expectancy.
        """
        tracker = SpotOutcomeTracker()

        # Signal 1: Target reached (+100 pts)
        e1 = self._create_synthetic_event(event_id="e1", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(e1)
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24110.0, 23990.0, 24105.0)
        tracker.process_candle(c1)

        # Signal 2: Stop loss hit (-50 pts)
        e2 = self._create_synthetic_event(event_id="e2", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(e2)
        c2 = self._create_1m_candle("2026-11-09T09:31:00+05:30", 24000.0, 24010.0, 23940.0, 23945.0)
        tracker.process_candle(c2)

        # Signal 3: Invalid entry (open beyond stop)
        e3 = self._create_synthetic_event(event_id="e3", entry=24000.0, stop=23950.0, target=24100.0)
        tracker.register_signal(e3)
        c3 = self._create_1m_candle("2026-11-09T09:32:00+05:30", 23930.0, 23940.0, 23910.0, 23920.0)
        tracker.process_candle(c3)

        # Signal 4: Unresolved trade
        e4 = self._create_synthetic_event(event_id="e4", entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(e4)
        c4 = self._create_1m_candle("2026-11-09T09:33:00+05:30", 24000.0, 24020.0, 23990.0, 24015.0)
        tracker.process_candle(c4)
        tracker.finalize_unresolved()

        all_records = tracker.completed_records
        report = SpotOutcomeAttributor.generate_full_attribution_report(all_records)
        summary = report["overall_summary"]

        self.assertEqual(summary["total_observations"], 4)
        self.assertEqual(summary["eligible_observations"], 3)  # Excludes Signal 3
        self.assertEqual(summary["resolved_observations"], 2)  # Signals 1 and 2
        self.assertEqual(summary["target_reached_count"], 1)
        self.assertEqual(summary["stop_loss_hit_count"], 1)
        self.assertEqual(summary["entry_invalidated_count"], 1)
        self.assertEqual(summary["unresolved_count"], 1)

        # Target rate: 1/2 resolved = 50.0%, 1/3 eligible = 33.33%
        self.assertEqual(summary["target_rate_pct_of_resolved"], 50.0)
        self.assertEqual(summary["target_rate_pct_of_eligible"], 33.33)

        # Resolved expectancy: (+100 - 50) / 2 = +25.0 pts
        self.assertEqual(summary["expectancy_per_resolved_pts"], 25.0)
        # Denominators explicitly verified
        self.assertEqual(summary["denominators"]["resolved_denominator"], 2)
        self.assertEqual(summary["denominators"]["eligible_denominator"], 3)
        self.assertEqual(summary["denominators"]["observed_denominator"], 4)

    # =========================================================================
    # AUDIT ITEM 6: Time-Exit Cutoff & Timestamps
    # =========================================================================

    def test_time_exit_cutoff_full_candle_close(self):
        """
        Time exit at 15:25 IST triggers strictly upon completion of the 15:24-15:25 bar.
        Exits at candle.close without accessing future bars.
        """
        tracker = SpotOutcomeTracker(intraday_time_exit=time(15, 25))
        event = self._create_synthetic_event(entry=24000.0, stop=23900.0, target=24200.0)
        tracker.register_signal(event)

        c1 = self._create_1m_candle("2026-11-09T15:23:00+05:30", 24000.0, 24020.0, 23990.0, 24010.0)
        tracker.process_candle(c1)

        # 15:24 to 15:25 candle completes at 15:25:00
        c2 = self._create_1m_candle("2026-11-09T15:24:00+05:30", 24010.0, 24035.0, 24005.0, 24030.0)
        completed = tracker.process_candle(c2)

        self.assertEqual(len(completed), 1)
        rec = completed[0]
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.TIME_EXIT.value)
        self.assertEqual(rec.exit_reason, ExitReason.SESSION_TIME_EXIT.value)
        self.assertEqual(rec.exit_price, 24030.0)  # Close of 15:24-15:25 bar
        self.assertEqual(rec.exit_timestamp, "2026-11-09T15:25:00+05:30")
        self.assertEqual(rec.terminal_candle_excursion_status, TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value)

    # =========================================================================
    # AUDIT ITEM 7: Fill-Model Disclaimers & Data Quality
    # =========================================================================

    def test_fill_model_disclaimers_and_data_quality_flags(self):
        """Audit qualifications state simulated reference touches, not guaranteed fills."""
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            quality_flags=[DataQualityFlag.OUT_OF_SESSION]
        )
        tracker.register_signal(event)
        c1 = self._create_1m_candle("2026-11-09T09:30:00+05:30", 24000.0, 24110.0, 23990.0, 24105.0)
        records = tracker.process_candle(c1)

        report = SpotOutcomeAttributor.generate_full_attribution_report(records)
        quals = report["audit_qualifications"]
        self.assertEqual(quals["fill_model"], "SIMULATED_REFERENCE_PRICE_TOUCHES_NOT_GUARANTEED_FILLS")
        self.assertEqual(quals["asset_class"], "NIFTY_50_SPOT_INDEX")
        self.assertEqual(quals["tradability"], "UNTRADABLE_REFERENCE_INDEX")
        self.assertEqual(quals["option_execution"], "STRICTLY_UNAVAILABLE")

        rec = records[0]
        self.assertIn(DataQualityFlag.OUT_OF_SESSION, rec.data_quality_flags)

    # =========================================================================
    # END-TO-END INTEGRATION
    # =========================================================================

    def test_integrated_spot_replay_with_audit_corrections(self):
        """
        Runs HistoricalSpotReplayEngine end-to-end with outcome_tracker attached.
        Confirms seamless execution, event generation, and audit compliance.
        """
        temp_dir = tempfile.mkdtemp()
        try:
            store = HistoricalSpotStore(base_dir=temp_dir, storage_format="csv")
            start_dt = datetime(2026, 11, 9, 9, 15, tzinfo=IST_ZONE)

            records = []
            curr = start_dt
            for i in range(40):
                p = 24000.0 + (i * 2.0)
                records.append({
                    "timestamp": curr.isoformat(),
                    "open": p,
                    "high": p + 3.0,
                    "low": p - 3.0,
                    "close": p + 1.5,
                    "volume": 0,
                    "is_closed": True
                })
                curr += timedelta(minutes=1)

            store.ingest_records(records, symbol="NIFTY", source_name="test_integ_audit_3d1")

            tracker = SpotOutcomeTracker(track_rejected_signals=True)
            engine = HistoricalSpotReplayEngine(store)

            events = list(engine.run_replay(
                symbol="NIFTY",
                evaluate_on_timeframe="5m",
                outcome_tracker=tracker
            ))

            self.assertGreater(len(events), 0)
            self.assertEqual(tracker.active_trades_count, 0)
            self.assertEqual(tracker.pending_signals_count, 0)

            records = tracker.completed_records
            for r in records:
                self.assertIsInstance(r, SpotOutcomeRecord)
                self.assertEqual(r.execution_metadata["underlying_signal_only"], True)
                self.assertEqual(r.execution_metadata["option_execution_eligible"], False)
                self.assertIn(r.terminal_candle_excursion_status, [
                    TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
                    TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
                    TerminalCandleExcursionStatus.NOT_APPLICABLE.value
                ])
        finally:
            shutil.rmtree(temp_dir)

    # =========================================================================
    # PHASE 3D.2: COHORT SEPARATION & REPORTING INTEGRITY TESTS
    # =========================================================================

    def test_counterfactual_outcome_mutation_invariance_on_accepted_strategy(self):
        """
        CRITICAL INVARIANCE PROOF (Audit Item 8):
        Altering counterfactual outcomes (e.g. transforming 100% losses to 100% wins in rejected setups)
        MUST produce ZERO change in the headline accepted-strategy performance metrics.
        """
        # 1. Base Accepted Cohort: 2 signals (1 target hit +100 pts, 1 stop hit -50 pts)
        accepted_records = [
            SpotOutcomeRecord(
                record_id="rec_acc_1",
                signal_id="sig_acc_1",
                signal_timestamp="2026-11-09T09:30:00+05:30",
                direction="BULLISH",
                entry_reference_price=24000.0,
                entry_timestamp="2026-11-09T09:30:00+05:30",
                entry_convention="NEXT_BAR_OPEN",
                slippage_points=0.0,
                structural_stop_loss=23950.0,
                structural_target=24100.0,
                initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T09:30:00+05:30",
                exit_timestamp="2026-11-09T09:32:00+05:30",
                exit_price=24100.0,
                exit_reason="TARGET_HIT",
                outcome_classification=SpotOutcomeType.TARGET_REACHED.value,
                mfe_points=100.0,
                mae_points=15.0,
                mfe_r_multiple=2.0,
                mae_r_multiple=0.3,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
                terminal_candle_max_favourable_bound=105.0,
                terminal_candle_max_adverse_bound=15.0,
                underlying_points_pnl=100.0,
                bars_held=2,
                setup_score=85.0,
                canonical_gates_passed=True,
                is_counterfactual=False,
                failed_gates=[],
                gate_rejection_reasons=[],
                data_quality_flags=[],
                execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": True}
            ),
            SpotOutcomeRecord(
                record_id="rec_acc_2",
                signal_id="sig_acc_2",
                signal_timestamp="2026-11-09T10:00:00+05:30",
                direction="BULLISH",
                entry_reference_price=24000.0,
                entry_timestamp="2026-11-09T10:00:00+05:30",
                entry_convention="NEXT_BAR_OPEN",
                slippage_points=0.0,
                structural_stop_loss=23950.0,
                structural_target=24100.0,
                initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T10:00:00+05:30",
                exit_timestamp="2026-11-09T10:02:00+05:30",
                exit_price=23950.0,
                exit_reason="STOP_LOSS_HIT",
                outcome_classification=SpotOutcomeType.STOP_LOSS_HIT.value,
                mfe_points=10.0,
                mae_points=50.0,
                mfe_r_multiple=0.2,
                mae_r_multiple=1.0,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
                terminal_candle_max_favourable_bound=10.0,
                terminal_candle_max_adverse_bound=50.0,
                underlying_points_pnl=-50.0,
                bars_held=2,
                setup_score=82.0,
                canonical_gates_passed=True,
                is_counterfactual=False,
                failed_gates=[],
                gate_rejection_reasons=[],
                data_quality_flags=[],
                execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": True}
            )
        ]

        # Scenario 1 Counterfactuals: 3 heavy stop-loss hits (-150 pts total)
        counterfactuals_loss = [
            SpotOutcomeRecord(
                record_id=f"rec_cf_loss_{i}",
                signal_id=f"sig_cf_{i}",
                signal_timestamp="2026-11-09T11:00:00+05:30",
                direction="BULLISH",
                entry_reference_price=24000.0,
                entry_timestamp="2026-11-09T11:00:00+05:30",
                entry_convention="NEXT_BAR_OPEN",
                slippage_points=0.0,
                structural_stop_loss=23950.0,
                structural_target=24100.0,
                initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T11:00:00+05:30",
                exit_timestamp="2026-11-09T11:02:00+05:30",
                exit_price=23950.0,
                exit_reason="STOP_LOSS_HIT",
                outcome_classification=SpotOutcomeType.STOP_LOSS_HIT.value,
                mfe_points=5.0,
                mae_points=50.0,
                mfe_r_multiple=0.1,
                mae_r_multiple=1.0,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
                terminal_candle_max_favourable_bound=5.0,
                terminal_candle_max_adverse_bound=50.0,
                underlying_points_pnl=-50.0,
                bars_held=2,
                setup_score=50.0,
                canonical_gates_passed=False,
                is_counterfactual=True,
                failed_gates=["GATE_1_SQUEEZE_COMPRESSION"],
                gate_rejection_reasons=["Squeeze active"],
                data_quality_flags=[],
                execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": True}
            )
            for i in range(3)
        ]

        # Scenario 2 Counterfactuals: 3 massive target hits (+300 pts total)
        counterfactuals_win = [
            SpotOutcomeRecord(
                record_id=f"rec_cf_win_{i}",
                signal_id=f"sig_cf_{i}",
                signal_timestamp="2026-11-09T11:00:00+05:30",
                direction="BULLISH",
                entry_reference_price=24000.0,
                entry_timestamp="2026-11-09T11:00:00+05:30",
                entry_convention="NEXT_BAR_OPEN",
                slippage_points=0.0,
                structural_stop_loss=23950.0,
                structural_target=24100.0,
                initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T11:00:00+05:30",
                exit_timestamp="2026-11-09T11:02:00+05:30",
                exit_price=24100.0,
                exit_reason="TARGET_HIT",
                outcome_classification=SpotOutcomeType.TARGET_REACHED.value,
                mfe_points=100.0,
                mae_points=10.0,
                mfe_r_multiple=2.0,
                mae_r_multiple=0.2,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
                terminal_candle_max_favourable_bound=100.0,
                terminal_candle_max_adverse_bound=10.0,
                underlying_points_pnl=100.0,
                bars_held=2,
                setup_score=50.0,
                canonical_gates_passed=False,
                is_counterfactual=True,
                failed_gates=["GATE_1_SQUEEZE_COMPRESSION"],
                gate_rejection_reasons=["Squeeze active"],
                data_quality_flags=[],
                execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": True}
            )
            for i in range(3)
        ]

        # Generate reports for both scenarios
        rep_loss = SpotOutcomeAttributor.generate_full_attribution_report(accepted_records + counterfactuals_loss)
        rep_win = SpotOutcomeAttributor.generate_full_attribution_report(accepted_records + counterfactuals_win)

        # 1. Headline Strategy Metrics MUST be 100% BIT-FOR-BIT IDENTICAL
        head_loss = rep_loss["headline_performance_summary"]
        head_win = rep_win["headline_performance_summary"]

        self.assertEqual(head_loss, head_win)
        self.assertEqual(head_loss["expectancy_per_resolved_pts"], 25.0)  # (+100 - 50) / 2 = +25.0
        self.assertEqual(head_loss["target_rate_pct_of_resolved"], 50.0)
        self.assertEqual(head_loss["stop_rate_pct_of_resolved"], 50.0)
        self.assertEqual(head_loss["total_points_pnl"], 50.0)
        self.assertEqual(head_loss["eligible_observations"], 2)
        self.assertEqual(head_loss["resolved_observations"], 2)

        # 2. Accepted Signal Observations Summary MUST also be identical
        self.assertEqual(
            rep_loss["accepted_signal_observations_summary"],
            rep_win["accepted_signal_observations_summary"]
        )

        # 3. Meanwhile, counterfactual summaries reflect their respective differences
        cf_loss = rep_loss["rejected_counterfactual_summary"]
        cf_win = rep_win["rejected_counterfactual_summary"]

        self.assertEqual(cf_loss["total_points_pnl"], -150.0)
        self.assertEqual(cf_win["total_points_pnl"], 300.0)
        self.assertNotEqual(cf_loss, cf_win)

        # 4. Supplementary combined diagnostics show the aggregate
        comb_loss = rep_loss["supplementary_combined_research_diagnostics"]["metrics"]
        comb_win = rep_win["supplementary_combined_research_diagnostics"]["metrics"]

        self.assertEqual(comb_loss["total_points_pnl"], -100.0)  # +50 - 150 = -100
        self.assertEqual(comb_win["total_points_pnl"], 350.0)    # +50 + 300 = 350

    def test_cohort_separation_and_renamed_terminology(self):
        """
        Verifies renamed cohort keys:
        - ACCEPTED_SIGNAL_OBSERVATIONS (not ACCEPTED_TRADES)
        - HYPOTHETICAL_REJECTED_COUNTERFACTUALS
        - Documented bars_held convention in metadata
        """
        accepted = [
            SpotOutcomeRecord(
                record_id="rec_1", signal_id="sig_1", signal_timestamp="2026-11-09T09:30:00+05:30",
                direction="BULLISH", entry_reference_price=24000.0, entry_timestamp="2026-11-09T09:30:00+05:30",
                entry_convention="NEXT_BAR_OPEN", slippage_points=0.0, structural_stop_loss=23950.0,
                structural_target=24100.0, initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T09:30:00+05:30", exit_timestamp="2026-11-09T09:31:00+05:30",
                exit_price=24100.0, exit_reason="TARGET_HIT", outcome_classification=SpotOutcomeType.TARGET_REACHED.value,
                mfe_points=100.0, mae_points=10.0, mfe_r_multiple=2.0, mae_r_multiple=0.2,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
                terminal_candle_max_favourable_bound=100.0, terminal_candle_max_adverse_bound=10.0,
                underlying_points_pnl=100.0, bars_held=1, setup_score=80.0,
                canonical_gates_passed=True, is_counterfactual=False, failed_gates=[], gate_rejection_reasons=[],
                data_quality_flags=[], execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": True}
            )
        ]
        rep = SpotOutcomeAttributor.generate_full_attribution_report(accepted)

        cohorts = rep["cohort_breakdown"]
        self.assertIn("ACCEPTED_SIGNAL_OBSERVATIONS", cohorts)
        self.assertIn("HYPOTHETICAL_REJECTED_COUNTERFACTUALS", cohorts)
        self.assertNotIn("ACCEPTED_TRADES", cohorts)

        # Verify bars_held convention in metadata
        bars_meta = rep["experiment_metadata"]["bars_held_convention"]
        self.assertEqual(bars_meta["unit"], "1_MINUTE_CLOSED_BARS")
        self.assertIn("Inclusive of the initial entry candle", bars_meta["counting_rule"])

    def test_dataset_end_and_invalidation_separate_counts(self):
        """Verifies dataset_end_unresolved_count is explicitly tracked alongside other categories."""
        records = [
            SpotOutcomeRecord(
                record_id="r1", signal_id="s1", signal_timestamp="2026-11-09T09:30:00+05:30",
                direction="BULLISH", entry_reference_price=24000.0, entry_timestamp="2026-11-09T09:30:00+05:30",
                entry_convention="NEXT_BAR_OPEN", slippage_points=0.0, structural_stop_loss=23950.0,
                structural_target=24100.0, initial_risk_points=50.0,
                first_eligible_candle_timestamp="2026-11-09T09:30:00+05:30", exit_timestamp=None,
                exit_price=None, exit_reason=ExitReason.DATASET_END_UNRESOLVED.value,
                outcome_classification=SpotOutcomeType.UNRESOLVED.value,
                mfe_points=25.0, mae_points=10.0, mfe_r_multiple=0.5, mae_r_multiple=0.2,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
                terminal_candle_max_favourable_bound=25.0, terminal_candle_max_adverse_bound=10.0,
                underlying_points_pnl=None, bars_held=5, setup_score=75.0,
                canonical_gates_passed=True, is_counterfactual=False, failed_gates=[], gate_rejection_reasons=[],
                data_quality_flags=[], execution_metadata={"underlying_signal_only": True, "actual_exit_occurred": False},
                last_observed_reference_price=24020.0,
                last_observed_timestamp="2026-11-09T09:35:00+05:30",
                unrealized_reference_points_pnl=20.0
            )
        ]
        metrics = SpotOutcomeAttributor.calculate_metrics(records)
        self.assertEqual(metrics["unresolved_count"], 1)
        self.assertEqual(metrics["dataset_end_unresolved_count"], 1)
        self.assertEqual(metrics["entry_invalidated_count"], 0)
        self.assertEqual(metrics["missed_entry_count"], 0)


if __name__ == "__main__":
    unittest.main()
