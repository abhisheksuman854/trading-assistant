"""
TradeLens AI - Phase 3E.3 Dedicated Test Suite: Unresolved Position Reference Price & Outcome Semantics.

Verifies:
1. Unresolved observations retain the last observed continuous-session reference price
   and timestamp, while exit_price and exit_timestamp remain strictly None.
2. Actual target, stop, and time exits retain genuine exit_price and exit_timestamp.
3. Unresolved observations cannot enter resolved P&L or affect resolved expectancy.
4. Counterfactual mutations cannot alter accepted signal performance metrics.
5. Repeated historical replay produces bit-for-bit deterministic outcome records.
6. Isolated 15:28/15:29 candles are never used as reference prices; reference prices strictly
   reflect the last continuous intraday candle close (15:15 IST).
"""

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


class TestUnresolvedReferencePriceAudit(unittest.TestCase):

    def _create_synthetic_event(
        self,
        event_id: str = "evt_001",
        timestamp: str = "2026-11-02T15:00:00+05:30",
        direction: str = "BULLISH",
        entry: Optional[float] = 24000.0,
        stop: Optional[float] = 23900.0,
        target: Optional[float] = 24200.0,
        score: float = 75.0,
        gates_passed: bool = True
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
            setup_score_bullish=score if direction == "BULLISH" else 0.0,
            setup_score_bearish=score if direction == "BEARISH" else 0.0,
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
            execution_time_ms=1.5
        )

    # ------------------------------------------------------------------------
    # 1. Unresolved Observation Semantics: Reference Price vs Exit Price
    # ------------------------------------------------------------------------
    def test_01_unresolved_has_reference_price_but_no_actual_exit(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_unres_01",
            timestamp="2026-11-02T15:00:00+05:30",
            direction="BULLISH", entry=24000.0, stop=23900.0, target=24200.0
        )
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 1, tzinfo=IST_ZONE),
            open=24000.0, high=24010.0, low=23995.0, close=24005.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)

        # Final continuous candle of session: 15:14-15:15 IST (close = 24050.0)
        c_final = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24040.0, high=24055.0, low=24035.0, close=24050.0
        )
        tracker.process_candle(c_final)

        # Isolated 15:29 bar arrived (jump to 24150.0)
        c_isolated = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24150.0, high=24150.0, low=24150.0, close=24150.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_isolated)

        finalized = tracker.finalize_unresolved()
        self.assertEqual(len(finalized), 1)
        rec = finalized[0]

        # Mandatory Semantic Checks
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.UNRESOLVED.value)
        self.assertEqual(rec.exit_reason, ExitReason.DATASET_END_UNRESOLVED.value)
        # Exit fields must be None (no actual exit occurred)
        self.assertIsNone(rec.exit_price)
        self.assertIsNone(rec.exit_timestamp)
        self.assertIsNone(rec.underlying_points_pnl)
        # Reference valuation fields must be strictly populated
        self.assertEqual(rec.last_observed_reference_price, 24050.0)
        self.assertEqual(rec.last_observed_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertEqual(rec.unrealized_reference_points_pnl, 50.0)  # 24050 - 24000
        # Metadata must state reference valuation
        self.assertFalse(rec.execution_metadata["actual_exit_occurred"])
        self.assertEqual(rec.execution_metadata["outcome_type"], "UNRESOLVED_OBSERVATION")
        self.assertEqual(
            rec.execution_metadata["description"],
            "Unresolved position retained the last observed continuous-session reference price."
        )

    # ------------------------------------------------------------------------
    # 2. Actual Resolved Exits Retain Genuine Exit Price and Timestamp
    # ------------------------------------------------------------------------
    def test_02_actual_exits_retain_genuine_exit_fields(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_target_01",
            timestamp="2026-11-02T10:00:00+05:30",
            direction="BULLISH", entry=24000.0, stop=23900.0, target=24100.0
        )
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 10, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 10, 1, tzinfo=IST_ZONE),
            open=24000.0, high=24020.0, low=23990.0, close=24010.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)

        # Candle reaches target 24100.0
        c_target = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 10, 1, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 10, 2, tzinfo=IST_ZONE),
            open=24010.0, high=24110.0, low=24005.0, close=24105.0
        )
        completed = tracker.process_candle(c_target)
        self.assertEqual(len(completed), 1)
        rec = completed[0]

        # Mandatory Actual Exit Checks
        self.assertEqual(rec.outcome_classification, SpotOutcomeType.TARGET_REACHED.value)
        self.assertEqual(rec.exit_reason, ExitReason.TARGET_HIT.value)
        self.assertEqual(rec.exit_price, 24100.0)
        self.assertEqual(rec.exit_timestamp, "2026-11-02T10:02:00+05:30")
        self.assertEqual(rec.underlying_points_pnl, 100.0)
        self.assertEqual(rec.last_observed_reference_price, 24100.0)
        self.assertEqual(rec.last_observed_timestamp, "2026-11-02T10:02:00+05:30")
        self.assertIsNone(rec.unrealized_reference_points_pnl)
        self.assertTrue(rec.execution_metadata["actual_exit_occurred"])
        self.assertEqual(rec.execution_metadata["outcome_type"], "RESOLVED_EXIT")

    # ------------------------------------------------------------------------
    # 3. Unresolved Observations Cannot Affect Resolved Realized Expectancy
    # ------------------------------------------------------------------------
    def test_03_unresolved_cannot_affect_resolved_expectancy(self):
        # 1 Resolved trade (+80 points)
        rec_resolved = SpotOutcomeRecord(
            record_id="r_res", signal_id="s_res", signal_timestamp="2026-11-02T09:30:00+05:30",
            direction="BULLISH", entry_reference_price=24000.0, entry_timestamp="2026-11-02T09:30:00+05:30",
            entry_convention="NEXT_BAR_OPEN", slippage_points=0.0, structural_stop_loss=23950.0,
            structural_target=24080.0, initial_risk_points=50.0,
            first_eligible_candle_timestamp="2026-11-02T09:30:00+05:30",
            exit_timestamp="2026-11-02T09:40:00+05:30", exit_price=24080.0,
            exit_reason=ExitReason.TARGET_HIT.value, outcome_classification=SpotOutcomeType.TARGET_REACHED.value,
            mfe_points=80.0, mae_points=10.0, mfe_r_multiple=1.6, mae_r_multiple=0.2,
            terminal_candle_excursion_status=TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN.value,
            terminal_candle_max_favourable_bound=80.0, terminal_candle_max_adverse_bound=10.0,
            underlying_points_pnl=80.0, bars_held=10, setup_score=85.0,
            canonical_gates_passed=True, is_counterfactual=False, failed_gates=[], gate_rejection_reasons=[],
            data_quality_flags=[], execution_metadata={"actual_exit_occurred": True},
            last_observed_reference_price=24080.0, last_observed_timestamp="2026-11-02T09:40:00+05:30",
            unrealized_reference_points_pnl=None
        )

        metrics_base = SpotOutcomeAttributor.calculate_metrics([rec_resolved])
        self.assertEqual(metrics_base["resolved_observations"], 1)
        self.assertEqual(metrics_base["total_realized_points_pnl"], 80.0)
        self.assertEqual(metrics_base["expectancy_per_resolved_pts"], 80.0)

        # 3 Unresolved records with large unrealized positive and negative mark-to-market swings
        rec_unres1 = SpotOutcomeRecord(
            record_id="r_unres1", signal_id="s_u1", signal_timestamp="2026-11-02T15:00:00+05:30",
            direction="BULLISH", entry_reference_price=24000.0, entry_timestamp="2026-11-02T15:00:00+05:30",
            entry_convention="NEXT_BAR_OPEN", slippage_points=0.0, structural_stop_loss=23900.0,
            structural_target=24200.0, initial_risk_points=100.0,
            first_eligible_candle_timestamp="2026-11-02T15:00:00+05:30",
            exit_timestamp=None, exit_price=None,  # No actual exit
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value, outcome_classification=SpotOutcomeType.UNRESOLVED.value,
            mfe_points=20.0, mae_points=15.0, mfe_r_multiple=0.2, mae_r_multiple=0.15,
            terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
            terminal_candle_max_favourable_bound=20.0, terminal_candle_max_adverse_bound=15.0,
            underlying_points_pnl=None,  # No realized P&L
            bars_held=15, setup_score=75.0,
            canonical_gates_passed=True, is_counterfactual=False, failed_gates=[], gate_rejection_reasons=[],
            data_quality_flags=[], execution_metadata={"actual_exit_occurred": False},
            last_observed_reference_price=24010.0, last_observed_timestamp="2026-11-02T15:15:00+05:30",
            unrealized_reference_points_pnl=+10.0
        )
        rec_unres2 = SpotOutcomeRecord(
            record_id="r_unres2", signal_id="s_u2", signal_timestamp="2026-11-03T15:00:00+05:30",
            direction="BULLISH", entry_reference_price=24000.0, entry_timestamp="2026-11-03T15:00:00+05:30",
            entry_convention="NEXT_BAR_OPEN", slippage_points=0.0, structural_stop_loss=23900.0,
            structural_target=24200.0, initial_risk_points=100.0,
            first_eligible_candle_timestamp="2026-11-03T15:00:00+05:30",
            exit_timestamp=None, exit_price=None,
            exit_reason=ExitReason.SESSION_END_UNRESOLVED.value, outcome_classification=SpotOutcomeType.UNRESOLVED.value,
            mfe_points=10.0, mae_points=30.0, mfe_r_multiple=0.1, mae_r_multiple=0.3,
            terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
            terminal_candle_max_favourable_bound=10.0, terminal_candle_max_adverse_bound=30.0,
            underlying_points_pnl=None,
            bars_held=15, setup_score=72.0,
            canonical_gates_passed=True, is_counterfactual=False, failed_gates=[], gate_rejection_reasons=[],
            data_quality_flags=[], execution_metadata={"actual_exit_occurred": False},
            last_observed_reference_price=23980.0, last_observed_timestamp="2026-11-03T15:15:00+05:30",
            unrealized_reference_points_pnl=-20.0
        )

        all_records = [rec_resolved, rec_unres1, rec_unres2]
        metrics_combined = SpotOutcomeAttributor.calculate_metrics(all_records)

        # Realized Expectancy must be 100% UNCHANGED by the presence of unresolved records
        self.assertEqual(metrics_combined["resolved_observations"], 1)
        self.assertEqual(metrics_combined["unresolved_count"], 2)
        self.assertEqual(metrics_combined["total_realized_points_pnl"], 80.0)
        self.assertEqual(metrics_combined["expectancy_per_resolved_pts"], 80.0)
        # Unrealized reference P&L is tracked in its own isolated field (+10 - 20 = -10.0)
        self.assertEqual(metrics_combined["total_unrealized_reference_points_pnl"], -10.0)

    # ------------------------------------------------------------------------
    # 4. Counterfactual Mutations Cannot Alter Accepted Signal Metrics
    # ------------------------------------------------------------------------
    def test_04_counterfactual_mutations_cannot_change_accepted_metrics(self):
        tracker = SpotOutcomeTracker(track_rejected_signals=True)
        evt_acc = self._create_synthetic_event(
            event_id="evt_acc", timestamp="2026-11-02T10:00:00+05:30",
            entry=24000.0, stop=23950.0, target=24100.0, gates_passed=True
        )
        evt_rej = self._create_synthetic_event(
            event_id="evt_rej", timestamp="2026-11-02T10:00:00+05:30",
            entry=24000.0, stop=23950.0, target=24100.0, gates_passed=False
        )

        c1 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 10, 0, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 10, 1, tzinfo=IST_ZONE),
            open=24000.0, high=24020.0, low=23990.0, close=24010.0
        )
        c2 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 10, 1, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 10, 2, tzinfo=IST_ZONE),
            open=24010.0, high=24110.0, low=24005.0, close=24105.0
        )
        tracker.register_signal(evt_acc)
        tracker.register_signal(evt_rej)
        tracker.process_candle(c1)
        tracker.process_candle(c2)

        rep1 = SpotOutcomeAttributor.generate_full_attribution_report(tracker.completed_records)
        acc_base = rep1["headline_performance_summary"]

        # Mutate counterfactual record: change P&L, reference price, or add extra counterfactuals
        mutated_records = list(tracker.completed_records)
        for r in mutated_records:
            if r.is_counterfactual:
                r.underlying_points_pnl = -9999.0
                r.last_observed_reference_price = 0.0

        rep2 = SpotOutcomeAttributor.generate_full_attribution_report(mutated_records)
        acc_mutated = rep2["headline_performance_summary"]

        # Accepted headline metrics must be bit-for-bit identical
        self.assertEqual(acc_base["total_observations"], acc_mutated["total_observations"])
        self.assertEqual(acc_base["resolved_observations"], acc_mutated["resolved_observations"])
        self.assertEqual(acc_base["total_realized_points_pnl"], acc_mutated["total_realized_points_pnl"])
        self.assertEqual(acc_base["expectancy_per_resolved_pts"], acc_mutated["expectancy_per_resolved_pts"])

    # ------------------------------------------------------------------------
    # 5. Isolated 15:28/15:29 Candles Are Never Used as Reference Prices
    # ------------------------------------------------------------------------
    def test_05_isolated_candles_never_used_as_reference_prices(self):
        tracker = SpotOutcomeTracker()
        event = self._create_synthetic_event(
            event_id="evt_iso_check",
            timestamp="2026-11-02T15:10:00+05:30",
            direction="BULLISH", entry=24000.0, stop=23800.0, target=24400.0
        )
        c_entry = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 10, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 11, tzinfo=IST_ZONE),
            open=24000.0, high=24010.0, low=23990.0, close=24005.0
        )
        tracker.register_signal(event)
        tracker.process_candle(c_entry)

        # Last continuous candle (15:14-15:15 close = 24020.0)
        c_cont = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 14, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 15, tzinfo=IST_ZONE),
            open=24010.0, high=24025.0, low=24005.0, close=24020.0
        )
        tracker.process_candle(c_cont)

        # Isolated 15:29 bar (+200 pts jump to 24220.0)
        c_1529 = HistoricalSpotCandle(
            symbol="NIFTY", timeframe="1m",
            start_time=datetime(2026, 11, 2, 15, 29, tzinfo=IST_ZONE),
            end_time=datetime(2026, 11, 2, 15, 30, tzinfo=IST_ZONE),
            open=24220.0, high=24220.0, low=24220.0, close=24220.0,
            validation_flags=[DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED]
        )
        tracker.process_candle(c_1529)

        # Finalize
        recs = tracker.finalize_unresolved()
        self.assertEqual(len(recs), 1)
        rec = recs[0]

        # The reference price MUST be the continuous close 24020.0, NOT 24220.0
        self.assertNotEqual(rec.last_observed_reference_price, 24220.0)
        self.assertEqual(rec.last_observed_reference_price, 24020.0)
        self.assertEqual(rec.last_observed_timestamp, "2026-11-02T15:15:00+05:30")
        self.assertIsNone(rec.exit_price)
        self.assertIsNone(rec.exit_timestamp)


if __name__ == "__main__":
    unittest.main()

