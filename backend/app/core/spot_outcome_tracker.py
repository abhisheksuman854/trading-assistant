"""
TradeLens AI - Phase 3D: Underlying Spot Outcome Tracking & Performance Attribution Engine.

Architectural Guarantees & Audit Invariants (Phase 3D.1):
1. Terminal-Candle MFE/MAE Integrity:
   - Excursions from fully completed candles prior to the terminal candle are confirmed.
   - When a stop or target is triggered within a 1-minute bar, intra-bar sequence of High vs Low
     is fundamentally indeterminate. Terminal candle excursions are stamped as
     INTRABAR_SEQUENCE_UNKNOWN. Ambiguous terminal extrema are stored separately as bounds,
     not claimed as confirmed pre-exit MFE/MAE.
   - Stop-first rule is preserved for simultaneous stop/target touches.
   - Gap-through stops exit at candle.open; later intra-bar extrema are excluded from pre-exit excursion.

2. Next-Bar Entry Geometry Validation:
   - Evaluates candle.open against frozen Phase 2C structural levels before creating an active trade.
   - Open beyond/equal to stop -> ENTRY_INVALIDATED (excluded from resolved trade expectancy).
   - Open beyond/equal to target -> MISSED_ENTRY (excluded from resolved trade expectancy).
   - Slippage breaches or invalid initial risk -> ENTRY_INVALIDATED.
   - Phase 2C levels are NEVER moved or recalculated.

3. Rejected-Signal Counterfactuals:
   - Phase 2C rejected signals are tracked as clearly labeled hypothetical counterfactuals
     (is_counterfactual=True, is_accepted_trade=False) IF AND ONLY IF valid Phase 2C structural
     levels exist.
   - If valid levels do not exist, the signal is EXCLUDED_NON_ELIGIBLE.
   - Rejection counts are reported separately from hypothetical outcome counts.

4. Overlapping Signal Policy:
   - Configurable policy: INDEPENDENT_SIGNAL_EVENTS vs SINGLE_ACTIVE_SEQUENCE.
   - Selected policy is recorded in all output metadata.
   - Under SINGLE_ACTIVE_SEQUENCE, subsequent signals while a trade is active are recorded
     as EXCLUDED_NON_ELIGIBLE (SKIPPED_DUE_TO_ACTIVE_POSITION).

5. Explicit Attribution Denominators:
   - Every metric explicitly declares its denominator (e.g. resolved eligible vs all eligible).
   - Unresolved and invalidated entries are never silently counted as zero-point outcomes.

6. Non-Zero Latency Execution & Time Exit Correctness:
   - Signal at close T is filled strictly at candle T+1 open (NEXT_BAR_OPEN).
   - Time exit cutoff (15:25 IST) triggers strictly upon full closure of the 15:24-15:25 bar,
     exiting at candle.close without reading later bars. Documented as a configurable research assumption.

7. Institutional Disclaimers:
   - Spot reference simulation only; untradable cash index; no option P&L; no broker queue simulation.
   - Zero forward win probability or profitability claims.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple, Union

from backend.app.data.session_clock import IST_ZONE
from backend.app.data.historical_spot_store import HistoricalSpotCandle, DataQualityFlag
from backend.app.data.candle_store import Candle
from backend.app.core.spot_replay_engine import SpotReplayEvent


class SpotOutcomeType(str, Enum):
    """Classification of the terminal outcome for an underlying spot setup."""
    TARGET_REACHED = "TARGET_REACHED"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    TIME_EXIT = "TIME_EXIT"
    UNRESOLVED = "UNRESOLVED"
    ENTRY_INVALIDATED = "ENTRY_INVALIDATED"
    MISSED_ENTRY = "MISSED_ENTRY"
    EXCLUDED_NON_ELIGIBLE = "EXCLUDED_NON_ELIGIBLE"


class EntryExecutionConvention(str, Enum):
    """Execution convention governing the entry price reference."""
    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"
    NEXT_BAR_OPEN_WITH_SLIPPAGE = "NEXT_BAR_OPEN_WITH_SLIPPAGE"


class OverlappingSignalPolicy(str, Enum):
    """Policy governing handling of candidate signals arriving while a trade is unresolved."""
    INDEPENDENT_SIGNAL_EVENTS = "INDEPENDENT_SIGNAL_EVENTS"
    SINGLE_ACTIVE_SEQUENCE = "SINGLE_ACTIVE_SEQUENCE"


class TerminalCandleExcursionStatus(str, Enum):
    """Status indicating whether terminal-candle excursions can be verified as pre-exit."""
    CONFIRMED_PRE_EXIT = "CONFIRMED_PRE_EXIT"
    INTRABAR_SEQUENCE_UNKNOWN = "INTRABAR_SEQUENCE_UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ExitReason(str, Enum):
    """Detailed mechanical trigger reason for trade exit or invalidation."""
    TARGET_HIT = "TARGET_HIT"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    SIMULTANEOUS_STOP_FIRST = "SIMULTANEOUS_STOP_FIRST"
    GAP_THROUGH_STOP = "GAP_THROUGH_STOP"
    GAP_THROUGH_TARGET = "GAP_THROUGH_TARGET"
    SESSION_TIME_EXIT = "SESSION_TIME_EXIT"
    MAX_BARS_TIME_EXIT = "MAX_BARS_TIME_EXIT"
    DATASET_END_UNRESOLVED = "DATASET_END_UNRESOLVED"
    SESSION_END_UNRESOLVED = "SESSION_END_UNRESOLVED"
    
    # Entry invalidation reasons
    OPEN_BEYOND_STOP = "OPEN_BEYOND_STOP"
    OPEN_BEYOND_TARGET = "OPEN_BEYOND_TARGET"
    OPEN_EQUAL_STOP = "OPEN_EQUAL_STOP"
    OPEN_EQUAL_TARGET = "OPEN_EQUAL_TARGET"
    SLIPPAGE_BREACH_STOP = "SLIPPAGE_BREACH_STOP"
    SLIPPAGE_BREACH_TARGET = "SLIPPAGE_BREACH_TARGET"
    INVALID_RISK_GEOMETRY = "INVALID_RISK_GEOMETRY"
    SKIPPED_DUE_TO_ACTIVE_POSITION = "SKIPPED_DUE_TO_ACTIVE_POSITION"
    MISSING_STRUCTURAL_LEVELS = "MISSING_STRUCTURAL_LEVELS"
    DATA_QUALITY_INVALIDATION = "DATA_QUALITY_INVALIDATION"


# Explicit Dataset-Aware Research Observation Boundaries (Phase 3E.1)
DATASET_LATEST_OBSERVATION_BOUNDARY = time(15, 15)
DATASET_LATEST_CANDLE_START = time(15, 14)


@dataclass
class SpotOutcomeRecord:
    """
    Auditable permanent outcome record for an individual spot setup signal.
    Captures complete chronology, execution references, structural levels, excursions, and attribution.
    """
    record_id: str
    signal_id: str
    signal_timestamp: str
    direction: str
    entry_reference_price: Optional[float]
    entry_timestamp: Optional[str]
    entry_convention: str
    slippage_points: float
    structural_stop_loss: Optional[float]
    structural_target: Optional[float]
    initial_risk_points: Optional[float]
    first_eligible_candle_timestamp: Optional[str]
    exit_timestamp: Optional[str]
    exit_price: Optional[float]
    exit_reason: str
    outcome_classification: str
    
    # Pre-exit confirmed excursions (strictly preceding terminal candle)
    mfe_points: float
    mae_points: float
    mfe_r_multiple: float
    mae_r_multiple: float
    
    # Terminal candle ambiguous bounds (Audit Item 1)
    terminal_candle_excursion_status: str
    terminal_candle_max_favourable_bound: Optional[float]
    terminal_candle_max_adverse_bound: Optional[float]
    
    underlying_points_pnl: Optional[float]
    bars_held: int  # Count of distinct 1m closed bars active (inclusive of entry bar and exit bar; 0 for invalidated/missed)
    setup_score: float
    canonical_gates_passed: bool
    is_counterfactual: bool
    failed_gates: List[str]
    gate_rejection_reasons: List[str]
    data_quality_flags: List[str]
    execution_metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActiveSpotTrade:
    """
    In-flight state of a tracked spot signal during sequential replay.
    """
    signal_id: str
    event_id: str
    signal_timestamp: datetime
    direction: str
    entry_reference_price: float
    entry_timestamp: datetime
    entry_convention: str
    slippage_points: float
    structural_stop_loss: float
    structural_target: float
    initial_risk_points: float
    first_eligible_candle_timestamp: datetime
    setup_score: float
    canonical_gates_passed: bool
    is_counterfactual: bool
    failed_gates: List[str]
    gate_rejection_reasons: List[str]
    data_quality_flags: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # In-flight mutable tracking state
    bars_held: int = 0
    confirmed_pre_exit_mfe: float = 0.0
    confirmed_pre_exit_mae: float = 0.0
    is_active: bool = True


class SpotOutcomeTracker:
    """
    Lookahead-free sequential outcome tracking engine for historical spot setups.
    Consumes sequential 1m candles and updates active candidate signals bar-by-bar.
    """

    def __init__(
        self,
        entry_convention: EntryExecutionConvention = EntryExecutionConvention.NEXT_BAR_OPEN,
        default_slippage_points: float = 0.0,
        intraday_time_exit: time = time(15, 25),
        max_holding_bars: Optional[int] = None,
        track_rejected_signals: bool = True,
        overlapping_policy: OverlappingSignalPolicy = OverlappingSignalPolicy.INDEPENDENT_SIGNAL_EVENTS,
        dataset_observation_cutoff: Optional[time] = None
    ):
        """
        Args:
            entry_convention: Execution convention for fill price (NEXT_BAR_OPEN).
            default_slippage_points: Additional adverse points applied to entry.
            intraday_time_exit: Daily session square-off cutoff (default 15:25 IST; use 15:15 for dataset-aware research cutoff).
            max_holding_bars: Maximum 1m bars before forced time exit (default None).
            track_rejected_signals: If True, tracks signals that failed canonical gates as counterfactuals.
            overlapping_policy: INDEPENDENT_SIGNAL_EVENTS or SINGLE_ACTIVE_SEQUENCE.
            dataset_observation_cutoff: Observed provider feed cutoff boundary (e.g. 15:15 IST).
        """
        self.entry_convention = entry_convention
        self.default_slippage_points = float(default_slippage_points)
        self.intraday_time_exit = intraday_time_exit
        self.max_holding_bars = max_holding_bars
        self.track_rejected_signals = track_rejected_signals
        self.overlapping_policy = overlapping_policy
        self.dataset_observation_cutoff = dataset_observation_cutoff

        # Queues and storage
        self._pending_signals: List[Tuple[SpotReplayEvent, datetime]] = []
        self._active_trades: List[ActiveSpotTrade] = []
        self._completed_records: List[SpotOutcomeRecord] = []
        self._record_counter: int = 0
        self._last_processed_candle: Optional[Union[HistoricalSpotCandle, Candle]] = None
        self._last_continuous_candle: Optional[Union[HistoricalSpotCandle, Candle]] = None

        # Denominator and observation counters
        self.total_signals_observed: int = 0
        self.accepted_signals_count: int = 0
        self.rejected_signals_count: int = 0
        self.rejected_missing_levels_count: int = 0
        self.entry_invalidated_count: int = 0
        self.missed_entry_count: int = 0
        self.eligible_observations_count: int = 0
        self.resolved_eligible_count: int = 0
        self.unresolved_count: int = 0
        self.dataset_end_unresolved_count: int = 0

    @property
    def completed_records(self) -> List[SpotOutcomeRecord]:
        return list(self._completed_records)

    @property
    def active_trades_count(self) -> int:
        return len(self._active_trades)

    @property
    def pending_signals_count(self) -> int:
        return len(self._pending_signals)

    def register_signal(self, event: SpotReplayEvent) -> bool:
        """
        Registers a SpotReplayEvent generated at candle close T.
        Queues the signal for execution at the open of candle T+1.
        Validates structural levels and overlapping policies.
        """
        dir_clean = str(event.candidate_direction).upper()
        if dir_clean not in ("BULLISH", "BEARISH"):
            return False

        self.total_signals_observed += 1
        is_accepted = bool(event.canonical_eight_gates_status)
        if is_accepted:
            self.accepted_signals_count += 1
        else:
            self.rejected_signals_count += 1

        # Check Overlapping Policy
        if self.overlapping_policy == OverlappingSignalPolicy.SINGLE_ACTIVE_SEQUENCE:
            if len(self._active_trades) > 0 or len(self._pending_signals) > 0:
                # Signal is skipped due to active sequence policy
                self._record_counter += 1
                rec_id = f"spot_out_{event.event_id}_{self._record_counter:06d}"
                rec = SpotOutcomeRecord(
                    record_id=rec_id,
                    signal_id=event.event_id,
                    signal_timestamp=event.replay_timestamp,
                    direction=dir_clean,
                    entry_reference_price=None,
                    entry_timestamp=None,
                    entry_convention=self.entry_convention.value,
                    slippage_points=self.default_slippage_points,
                    structural_stop_loss=event.structural_stop_loss,
                    structural_target=event.structural_target,
                    initial_risk_points=None,
                    first_eligible_candle_timestamp=None,
                    exit_timestamp=event.replay_timestamp,
                    exit_price=None,
                    exit_reason=ExitReason.SKIPPED_DUE_TO_ACTIVE_POSITION.value,
                    outcome_classification=SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value,
                    mfe_points=0.0,
                    mae_points=0.0,
                    mfe_r_multiple=0.0,
                    mae_r_multiple=0.0,
                    terminal_candle_excursion_status=TerminalCandleExcursionStatus.NOT_APPLICABLE.value,
                    terminal_candle_max_favourable_bound=None,
                    terminal_candle_max_adverse_bound=None,
                    underlying_points_pnl=None,
                    bars_held=0,
                    setup_score=event.setup_score_bullish if dir_clean == "BULLISH" else event.setup_score_bearish,
                    canonical_gates_passed=is_accepted,
                    is_counterfactual=not is_accepted,
                    failed_gates=list(event.failed_gates),
                    gate_rejection_reasons=list(event.rejection_reasons),
                    data_quality_flags=list(event.data_quality_flags),
                    execution_metadata={
                        "exclusion_reason": "SINGLE_ACTIVE_SEQUENCE policy active; prior trade in flight",
                        "overlapping_policy": self.overlapping_policy.value
                    }
                )
                self._completed_records.append(rec)
                return False

        # Validate Structural Levels
        has_valid_levels = (
            event.structural_entry is not None and
            event.structural_stop_loss is not None and
            event.structural_target is not None
        )

        if has_valid_levels:
            # Check Geometric Validity
            entry = float(event.structural_entry)
            stop = float(event.structural_stop_loss)
            tgt = float(event.structural_target)
            if dir_clean == "BULLISH" and not (stop < entry < tgt):
                has_valid_levels = False
            elif dir_clean == "BEARISH" and not (tgt < entry < stop):
                has_valid_levels = False

        if not has_valid_levels:
            self.rejected_missing_levels_count += 1
            self._record_counter += 1
            rec_id = f"spot_out_{event.event_id}_{self._record_counter:06d}"
            rec = SpotOutcomeRecord(
                record_id=rec_id,
                signal_id=event.event_id,
                signal_timestamp=event.replay_timestamp,
                direction=dir_clean,
                entry_reference_price=None,
                entry_timestamp=None,
                entry_convention=self.entry_convention.value,
                slippage_points=self.default_slippage_points,
                structural_stop_loss=event.structural_stop_loss,
                structural_target=event.structural_target,
                initial_risk_points=None,
                first_eligible_candle_timestamp=None,
                exit_timestamp=event.replay_timestamp,
                exit_price=None,
                exit_reason=ExitReason.MISSING_STRUCTURAL_LEVELS.value,
                outcome_classification=SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value,
                mfe_points=0.0,
                mae_points=0.0,
                mfe_r_multiple=0.0,
                mae_r_multiple=0.0,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.NOT_APPLICABLE.value,
                terminal_candle_max_favourable_bound=None,
                terminal_candle_max_adverse_bound=None,
                underlying_points_pnl=None,
                bars_held=0,
                setup_score=event.setup_score_bullish if dir_clean == "BULLISH" else event.setup_score_bearish,
                canonical_gates_passed=is_accepted,
                is_counterfactual=not is_accepted,
                failed_gates=list(event.failed_gates),
                gate_rejection_reasons=list(event.rejection_reasons),
                data_quality_flags=list(event.data_quality_flags),
                execution_metadata={
                    "exclusion_reason": "Structural levels absent or geometrically invalid in Phase 2C",
                    "overlapping_policy": self.overlapping_policy.value
                }
            )
            self._completed_records.append(rec)
            return False

        # If rejected signals are excluded from counterfactual tracking, stop here
        if not is_accepted and not self.track_rejected_signals:
            return False

        sig_time = datetime.fromisoformat(event.replay_timestamp)
        self._pending_signals.append((event, sig_time))
        return True

    def process_candle(self, candle: Union[HistoricalSpotCandle, Candle]) -> List[SpotOutcomeRecord]:
        """
        Processes a newly arrived 1m historical candle sequentially.
        1. Activates pending signals using candle.open (Next Bar Open), validating geometry.
        2. Evaluates active trades: checks exit conditions FIRST to prevent terminal-candle contamination.
        3. Returns any SpotOutcomeRecord finalized during this candle.
        """
        c_start = candle.start_time
        c_end = candle.end_time
        c_open = candle.open if hasattr(candle, "open") else candle.open_price
        c_high = candle.high if hasattr(candle, "high") else candle.high_price
        c_low = candle.low if hasattr(candle, "low") else candle.low_price
        c_close = candle.close if hasattr(candle, "close") else candle.close_price

        newly_completed: List[SpotOutcomeRecord] = []

        # Session Boundary & Overnight Transition Handling (Phase 3E.1)
        # An intraday trade from a previous calendar day must not carry overnight into the new day.
        # If its configured time-exit was not reached because session data ended earlier (e.g. 15:15),
        # finalize it as UNRESOLVED at the previous session's final close.
        if self._last_processed_candle is not None and c_start.date() > self._last_processed_candle.start_time.date():
            prev_c = self._last_continuous_candle or self._last_processed_candle
            prev_end = prev_c.end_time
            prev_close = prev_c.close if hasattr(prev_c, "close") else prev_c.close_price
            prev_start = prev_c.start_time

            still_current_day: List[ActiveSpotTrade] = []
            for trade in self._active_trades:
                if trade.entry_timestamp.date() < c_start.date():
                    self._record_counter += 1
                    self.dataset_end_unresolved_count += 1
                    rec_id = f"spot_out_{trade.signal_id}_{self._record_counter:06d}"
                    pnl = (prev_close - trade.entry_reference_price) if trade.direction == "BULLISH" else (trade.entry_reference_price - prev_close)
                    r_val = trade.initial_risk_points if trade.initial_risk_points > 0 else 1.0

                    rec = SpotOutcomeRecord(
                        record_id=rec_id,
                        signal_id=trade.signal_id,
                        signal_timestamp=trade.signal_timestamp.isoformat(),
                        direction=trade.direction,
                        entry_reference_price=trade.entry_reference_price,
                        entry_timestamp=trade.entry_timestamp.isoformat(),
                        entry_convention=trade.entry_convention,
                        slippage_points=trade.slippage_points,
                        structural_stop_loss=trade.structural_stop_loss,
                        structural_target=trade.structural_target,
                        initial_risk_points=trade.initial_risk_points,
                        first_eligible_candle_timestamp=trade.first_eligible_candle_timestamp.isoformat(),
                        exit_timestamp=prev_end.isoformat(),
                        exit_price=round(prev_close, 2),
                        exit_reason=ExitReason.SESSION_END_UNRESOLVED.value,
                        outcome_classification=SpotOutcomeType.UNRESOLVED.value,
                        mfe_points=round(trade.confirmed_pre_exit_mfe, 2),
                        mae_points=round(trade.confirmed_pre_exit_mae, 2),
                        mfe_r_multiple=round(trade.confirmed_pre_exit_mfe / r_val, 3),
                        mae_r_multiple=round(trade.confirmed_pre_exit_mae / r_val, 3),
                        terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
                        terminal_candle_max_favourable_bound=round(trade.confirmed_pre_exit_mfe, 2),
                        terminal_candle_max_adverse_bound=round(trade.confirmed_pre_exit_mae, 2),
                        underlying_points_pnl=round(pnl, 2),
                        bars_held=trade.bars_held,
                        setup_score=trade.setup_score,
                        canonical_gates_passed=trade.canonical_gates_passed,
                        is_counterfactual=trade.is_counterfactual,
                        failed_gates=list(trade.failed_gates),
                        gate_rejection_reasons=list(trade.gate_rejection_reasons),
                        data_quality_flags=list(trade.data_quality_flags),
                        execution_metadata={
                            "underlying_signal_only": True,
                            "option_execution_eligible": False,
                            "option_execution_status": "UNAVAILABLE",
                            "overlapping_policy": self.overlapping_policy.value,
                            "data_source": "ANGEL_ONE_HISTORICAL_SPOT",
                            "exit_candle_start": prev_start.isoformat(),
                            "exit_candle_end": prev_end.isoformat(),
                            "configured_time_exit": str(self.intraday_time_exit),
                            "cutoff_cause": "CONFIGURED_TIME_EXIT_NOT_REACHED_IN_SESSION_DATA",
                            "isolated_post_gap_excluded": True,
                            "disclaimer": "Simulated underlying spot tracking. Untradable index; not option P&L."
                        }
                    )
                    newly_completed.append(rec)
                    self._completed_records.append(rec)
                else:
                    still_current_day.append(trade)
            self._active_trades = still_current_day

        # Check if candle is an isolated post-gap unverified bar (Phase 3E.2)
        c_flags = getattr(candle, "validation_flags", [])
        if isinstance(c_flags, str):
            c_flags = [x.strip() for x in c_flags.split(",") if x.strip()]

        is_isolated_post_gap = (
            DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED in c_flags or
            getattr(candle, "is_isolated_post_gap", False)
        )
        if not is_isolated_post_gap and self._last_processed_candle is not None:
            if c_start.date() == self._last_processed_candle.start_time.date():
                gap_sec = (c_start - self._last_processed_candle.start_time).total_seconds()
                if gap_sec >= 300 and (c_open == c_high == c_low == c_close):
                    is_isolated_post_gap = True

        if is_isolated_post_gap:
            # Mandatory Phase 3E.2 Exclusions:
            # 1. Do NOT evaluate exit conditions (no stop/target/time-exit trigger across gap)
            # 2. Do NOT update confirmed pre-exit excursions (no MFE/MAE inflation)
            # 3. Do NOT increment bars_held
            # 4. Do NOT activate pending signals
            # 5. Position remains unresolved; annotate observation
            for trade in self._active_trades:
                trade.metadata["isolated_post_gap_bypassed"] = c_start.isoformat()
                trade.metadata["isolated_post_gap_price"] = c_close
            self._last_processed_candle = candle
            return newly_completed

        self._last_continuous_candle = candle
        self._last_processed_candle = candle

        # -------------------------------------------------------------
        # STEP 1: Activate Pending Signals at candle.open (Next Bar Open)
        # -------------------------------------------------------------
        activated_pending: List[Tuple[SpotReplayEvent, datetime]] = []
        for event, sig_time in self._pending_signals:
            if c_start >= sig_time:
                trade_or_record = self._activate_pending_signal(event, candle, c_open, c_start)
                if isinstance(trade_or_record, ActiveSpotTrade):
                    self._active_trades.append(trade_or_record)
                    self.eligible_observations_count += 1
                elif isinstance(trade_or_record, SpotOutcomeRecord):
                    # Entry was invalidated or missed before trade could become active
                    newly_completed.append(trade_or_record)
                    self._completed_records.append(trade_or_record)
                activated_pending.append((event, sig_time))

        for item in activated_pending:
            self._pending_signals.remove(item)

        # -------------------------------------------------------------
        # STEP 2: Evaluate Active Trades (Exit Check FIRST)
        # -------------------------------------------------------------
        remaining_trades: List[ActiveSpotTrade] = []

        for trade in self._active_trades:
            trade.bars_held += 1

            # Check Exit Conditions FIRST (Audit Item 1: Prevents Terminal-Bar Contamination)
            record = self._evaluate_exit_conditions(trade, candle, c_open, c_high, c_low, c_close, c_end)

            if record is not None:
                newly_completed.append(record)
                self._completed_records.append(record)
                self.resolved_eligible_count += 1
            else:
                # No exit triggered on this candle: fully completed pre-exit bar!
                # Safely update confirmed excursions
                self._update_confirmed_excursions(trade, c_high, c_low)
                remaining_trades.append(trade)

        self._active_trades = remaining_trades
        self.unresolved_count = len(self._active_trades)
        return newly_completed

    def finalize_unresolved(self) -> List[SpotOutcomeRecord]:
        """
        Finalizes all remaining active trades when replay terminates or dataset ends.
        Classifies them as UNRESOLVED with exit price at the last known continuous candle close.
        Excludes isolated post-gap settlement observations from contaminating exit prices.
        """
        finalized: List[SpotOutcomeRecord] = []
        if self._last_processed_candle is None:
            return finalized

        ref_c = self._last_continuous_candle or self._last_processed_candle
        c_end = ref_c.end_time
        c_close = (
            ref_c.close
            if hasattr(ref_c, "close")
            else ref_c.close_price
        )
        c_start_iso = (
            ref_c.start_time.isoformat()
            if hasattr(ref_c, "start_time")
            else str(c_end)
        )

        for trade in self._active_trades:
            self._record_counter += 1
            self.dataset_end_unresolved_count += 1
            rec_id = f"spot_out_{trade.signal_id}_{self._record_counter:06d}"

            pnl = (
                (c_close - trade.entry_reference_price)
                if trade.direction == "BULLISH"
                else (trade.entry_reference_price - c_close)
            )
            r_val = trade.initial_risk_points if trade.initial_risk_points > 0 else 1.0

            record = SpotOutcomeRecord(
                record_id=rec_id,
                signal_id=trade.signal_id,
                signal_timestamp=trade.signal_timestamp.isoformat(),
                direction=trade.direction,
                entry_reference_price=trade.entry_reference_price,
                entry_timestamp=trade.entry_timestamp.isoformat(),
                entry_convention=trade.entry_convention,
                slippage_points=trade.slippage_points,
                structural_stop_loss=trade.structural_stop_loss,
                structural_target=trade.structural_target,
                initial_risk_points=trade.initial_risk_points,
                first_eligible_candle_timestamp=trade.first_eligible_candle_timestamp.isoformat(),
                exit_timestamp=c_end.isoformat(),
                exit_price=c_close,
                exit_reason=ExitReason.DATASET_END_UNRESOLVED.value,
                outcome_classification=SpotOutcomeType.UNRESOLVED.value,
                mfe_points=round(trade.confirmed_pre_exit_mfe, 2),
                mae_points=round(trade.confirmed_pre_exit_mae, 2),
                mfe_r_multiple=round(trade.confirmed_pre_exit_mfe / r_val, 3),
                mae_r_multiple=round(trade.confirmed_pre_exit_mae / r_val, 3),
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT.value,
                terminal_candle_max_favourable_bound=round(trade.confirmed_pre_exit_mfe, 2),
                terminal_candle_max_adverse_bound=round(trade.confirmed_pre_exit_mae, 2),
                underlying_points_pnl=round(pnl, 2),
                bars_held=trade.bars_held,
                setup_score=trade.setup_score,
                canonical_gates_passed=trade.canonical_gates_passed,
                is_counterfactual=trade.is_counterfactual,
                failed_gates=list(trade.failed_gates),
                gate_rejection_reasons=list(trade.gate_rejection_reasons),
                data_quality_flags=list(trade.data_quality_flags),
                execution_metadata={
                    "underlying_signal_only": True,
                    "option_execution_eligible": False,
                    "option_execution_status": "UNAVAILABLE",
                    "overlapping_policy": self.overlapping_policy.value,
                    "data_source": "ANGEL_ONE_HISTORICAL_SPOT",
                    "exit_candle_start": c_start_iso,
                    "exit_candle_end": c_end.isoformat(),
                    "configured_time_exit": str(self.intraday_time_exit),
                    "cutoff_cause": "CONFIGURED_TIME_EXIT_NOT_REACHED_IN_DATASET",
                    "isolated_post_gap_excluded": True,
                    "disclaimer": "Simulated underlying spot tracking. Untradable index; not option P&L."
                }
            )
            finalized.append(record)
            self._completed_records.append(record)

        self._active_trades.clear()
        self._pending_signals.clear()
        self.unresolved_count = 0
        return finalized

    def _activate_pending_signal(
        self,
        event: SpotReplayEvent,
        candle: Union[HistoricalSpotCandle, Candle],
        c_open: float,
        c_start: datetime
    ) -> Union[ActiveSpotTrade, SpotOutcomeRecord]:
        """
        Validates next-bar entry geometry (Audit Item 2).
        If candle.open breaches stop or target, creates ENTRY_INVALIDATED or MISSED_ENTRY record.
        Otherwise initializes and returns ActiveSpotTrade.
        """
        direction = str(event.candidate_direction).upper()
        stop_price = float(event.structural_stop_loss)
        target_price = float(event.structural_target)
        is_accepted = bool(event.canonical_eight_gates_status)
        is_counterfactual = not is_accepted

        score = event.setup_score_bullish if direction == "BULLISH" else event.setup_score_bearish
        slip = self.default_slippage_points if self.entry_convention == EntryExecutionConvention.NEXT_BAR_OPEN_WITH_SLIPPAGE else 0.0

        # -------------------------------------------------------------
        # Next-Bar Entry Geometry Validation (Audit Item 2)
        # -------------------------------------------------------------
        invalidation_reason: Optional[ExitReason] = None
        outcome_type: Optional[SpotOutcomeType] = None

        if direction == "BULLISH":
            # 1. Open already beyond or at Stop
            if c_open < stop_price:
                invalidation_reason = ExitReason.OPEN_BEYOND_STOP
                outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
            elif c_open == stop_price:
                invalidation_reason = ExitReason.OPEN_EQUAL_STOP
                outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
            # 2. Open already beyond or at Target
            elif c_open > target_price:
                invalidation_reason = ExitReason.OPEN_BEYOND_TARGET
                outcome_type = SpotOutcomeType.MISSED_ENTRY
            elif c_open == target_price:
                invalidation_reason = ExitReason.OPEN_EQUAL_TARGET
                outcome_type = SpotOutcomeType.MISSED_ENTRY
            else:
                # 3. Check Slippage Breach
                eff_entry = c_open + slip
                if eff_entry <= stop_price:
                    invalidation_reason = ExitReason.SLIPPAGE_BREACH_STOP
                    outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
                elif eff_entry >= target_price:
                    invalidation_reason = ExitReason.SLIPPAGE_BREACH_TARGET
                    outcome_type = SpotOutcomeType.MISSED_ENTRY
                elif (eff_entry - stop_price) <= 0:
                    invalidation_reason = ExitReason.INVALID_RISK_GEOMETRY
                    outcome_type = SpotOutcomeType.ENTRY_INVALIDATED

        elif direction == "BEARISH":
            # 1. Open already beyond or at Stop
            if c_open > stop_price:
                invalidation_reason = ExitReason.OPEN_BEYOND_STOP
                outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
            elif c_open == stop_price:
                invalidation_reason = ExitReason.OPEN_EQUAL_STOP
                outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
            # 2. Open already beyond or at Target
            elif c_open < target_price:
                invalidation_reason = ExitReason.OPEN_BEYOND_TARGET
                outcome_type = SpotOutcomeType.MISSED_ENTRY
            elif c_open == target_price:
                invalidation_reason = ExitReason.OPEN_EQUAL_TARGET
                outcome_type = SpotOutcomeType.MISSED_ENTRY
            else:
                # 3. Check Slippage Breach
                eff_entry = c_open - slip
                if eff_entry >= stop_price:
                    invalidation_reason = ExitReason.SLIPPAGE_BREACH_STOP
                    outcome_type = SpotOutcomeType.ENTRY_INVALIDATED
                elif eff_entry <= target_price:
                    invalidation_reason = ExitReason.SLIPPAGE_BREACH_TARGET
                    outcome_type = SpotOutcomeType.MISSED_ENTRY
                elif (stop_price - eff_entry) <= 0:
                    invalidation_reason = ExitReason.INVALID_RISK_GEOMETRY
                    outcome_type = SpotOutcomeType.ENTRY_INVALIDATED

        # If invalidated or missed, create terminal record immediately (Audit Item 2)
        if invalidation_reason is not None and outcome_type is not None:
            if outcome_type == SpotOutcomeType.ENTRY_INVALIDATED:
                self.entry_invalidated_count += 1
            else:
                self.missed_entry_count += 1

            self._record_counter += 1
            rec_id = f"spot_out_{event.event_id}_{self._record_counter:06d}"
            return SpotOutcomeRecord(
                record_id=rec_id,
                signal_id=event.event_id,
                signal_timestamp=event.replay_timestamp,
                direction=direction,
                entry_reference_price=None,
                entry_timestamp=None,
                entry_convention=self.entry_convention.value,
                slippage_points=slip,
                structural_stop_loss=round(stop_price, 2),
                structural_target=round(target_price, 2),
                initial_risk_points=None,
                first_eligible_candle_timestamp=c_start.isoformat(),
                exit_timestamp=c_start.isoformat(),
                exit_price=None,
                exit_reason=invalidation_reason.value,
                outcome_classification=outcome_type.value,
                mfe_points=0.0,
                mae_points=0.0,
                mfe_r_multiple=0.0,
                mae_r_multiple=0.0,
                terminal_candle_excursion_status=TerminalCandleExcursionStatus.NOT_APPLICABLE.value,
                terminal_candle_max_favourable_bound=None,
                terminal_candle_max_adverse_bound=None,
                underlying_points_pnl=None,
                bars_held=0,
                setup_score=float(score),
                canonical_gates_passed=is_accepted,
                is_counterfactual=is_counterfactual,
                failed_gates=list(event.failed_gates),
                gate_rejection_reasons=list(event.rejection_reasons),
                data_quality_flags=list(event.data_quality_flags),
                execution_metadata={
                    "underlying_signal_only": True,
                    "option_execution_eligible": False,
                    "option_execution_status": "UNAVAILABLE",
                    "invalidation_detail": f"Candle open {c_open} failed entry geometry at {c_start.isoformat()}",
                    "overlapping_policy": self.overlapping_policy.value
                }
            )

        # Valid entry geometry: instantiate ActiveSpotTrade
        if direction == "BULLISH":
            entry_ref = c_open + slip
            initial_risk = entry_ref - stop_price
        else:
            entry_ref = c_open - slip
            initial_risk = stop_price - entry_ref

        return ActiveSpotTrade(
            signal_id=event.event_id,
            event_id=event.event_id,
            signal_timestamp=datetime.fromisoformat(event.replay_timestamp),
            direction=direction,
            entry_reference_price=round(entry_ref, 2),
            entry_timestamp=c_start,
            entry_convention=self.entry_convention.value,
            slippage_points=slip,
            structural_stop_loss=round(stop_price, 2),
            structural_target=round(target_price, 2),
            initial_risk_points=round(initial_risk, 2),
            first_eligible_candle_timestamp=c_start,
            setup_score=float(score),
            canonical_gates_passed=is_accepted,
            is_counterfactual=is_counterfactual,
            failed_gates=list(event.failed_gates),
            gate_rejection_reasons=list(event.rejection_reasons),
            data_quality_flags=list(event.data_quality_flags),
            metadata={
                "market_regime_5m": event.market_regime_5m,
                "market_regime_15m": event.market_regime_15m,
                "underlying_risk_reward": event.underlying_risk_reward,
                "overlapping_policy": self.overlapping_policy.value
            }
        )

    def _update_confirmed_excursions(self, trade: ActiveSpotTrade, c_high: float, c_low: float) -> None:
        """
        Updates confirmed pre-exit excursions strictly from completed candles known to precede the terminal exit.
        """
        if trade.direction == "BULLISH":
            fav = c_high - trade.entry_reference_price
            adv = trade.entry_reference_price - c_low
        else:
            fav = trade.entry_reference_price - c_low
            adv = c_high - trade.entry_reference_price

        if fav > trade.confirmed_pre_exit_mfe:
            trade.confirmed_pre_exit_mfe = fav
        if adv > trade.confirmed_pre_exit_mae:
            trade.confirmed_pre_exit_mae = adv

    def _evaluate_exit_conditions(
        self,
        trade: ActiveSpotTrade,
        candle: Union[HistoricalSpotCandle, Candle],
        c_open: float,
        c_high: float,
        c_low: float,
        c_close: float,
        c_end: datetime
    ) -> Optional[SpotOutcomeRecord]:
        """
        Evaluates stop, target, time exit, or gap conditions on the current candle.
        Enforces Audit Item 1: Terminal-candle excursion integrity and conservative stop-first rule.
        """
        direction = trade.direction
        stop = trade.structural_stop_loss
        target = trade.structural_target
        entry = trade.entry_reference_price
        c_start = candle.start_time

        exit_triggered = False
        exit_price = entry
        exit_reason = ExitReason.STOP_LOSS_HIT
        outcome_type = SpotOutcomeType.STOP_LOSS_HIT
        excursion_status = TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN

        # Raw candle excursions for ambiguous bounds reporting
        if direction == "BULLISH":
            candle_raw_fav = round(c_high - entry, 2)
            candle_raw_adv = round(entry - c_low, 2)
        else:
            candle_raw_fav = round(entry - c_low, 2)
            candle_raw_adv = round(c_high - entry, 2)

        confirmed_mfe = trade.confirmed_pre_exit_mfe
        confirmed_mae = trade.confirmed_pre_exit_mae

        # -----------------------------------------------------------------
        # Condition 1: Gap-Through Stop Loss at Bar Open (Adverse Gap)
        # -----------------------------------------------------------------
        if (direction == "BULLISH" and c_open <= stop) or (direction == "BEARISH" and c_open >= stop):
            exit_triggered = True
            exit_price = c_open
            exit_reason = ExitReason.GAP_THROUGH_STOP
            outcome_type = SpotOutcomeType.STOP_LOSS_HIT
            # Gap occurs at open: zero post-open intrabar movement preceded the exit!
            excursion_status = TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT
            if direction == "BULLISH":
                confirmed_mae = max(confirmed_mae, entry - c_open)
            else:
                confirmed_mae = max(confirmed_mae, c_open - entry)

        # -----------------------------------------------------------------
        # Condition 2: Simultaneous Stop and Target Hit ("Stop-First Rule")
        # -----------------------------------------------------------------
        elif (
            (direction == "BULLISH" and c_low <= stop and c_high >= target) or
            (direction == "BEARISH" and c_high >= stop and c_low <= target)
        ):
            exit_triggered = True
            exit_price = stop
            exit_reason = ExitReason.SIMULTANEOUS_STOP_FIRST
            outcome_type = SpotOutcomeType.STOP_LOSS_HIT
            excursion_status = TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN
            # Stop-first assumption: adverse excursion reached stop distance
            confirmed_mae = max(confirmed_mae, abs(entry - stop))
            # Favourable excursion does NOT claim target! Remains what preceded the terminal bar

        # -----------------------------------------------------------------
        # Condition 3: Normal Stop Loss Hit
        # -----------------------------------------------------------------
        elif (direction == "BULLISH" and c_low <= stop) or (direction == "BEARISH" and c_high >= stop):
            exit_triggered = True
            exit_price = stop
            exit_reason = ExitReason.STOP_LOSS_HIT
            outcome_type = SpotOutcomeType.STOP_LOSS_HIT
            excursion_status = TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN
            confirmed_mae = max(confirmed_mae, abs(entry - stop))

        # -----------------------------------------------------------------
        # Condition 4: Target Reached
        # -----------------------------------------------------------------
        elif (direction == "BULLISH" and c_high >= target) or (direction == "BEARISH" and c_low <= target):
            exit_triggered = True
            if (direction == "BULLISH" and c_open >= target) or (direction == "BEARISH" and c_open <= target):
                exit_price = target
                exit_reason = ExitReason.GAP_THROUGH_TARGET
                excursion_status = TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT
            else:
                exit_price = target
                exit_reason = ExitReason.TARGET_HIT
                excursion_status = TerminalCandleExcursionStatus.INTRABAR_SEQUENCE_UNKNOWN
            outcome_type = SpotOutcomeType.TARGET_REACHED
            # Favourable excursion confirmed to reach target distance
            confirmed_mfe = max(confirmed_mfe, abs(target - entry))

        # -----------------------------------------------------------------
        # Condition 5: Session Time Cutoff (15:25 IST - Fully Closed Bar)
        # -----------------------------------------------------------------
        elif c_end.time() >= self.intraday_time_exit:
            exit_triggered = True
            exit_price = c_close
            exit_reason = ExitReason.SESSION_TIME_EXIT
            outcome_type = SpotOutcomeType.TIME_EXIT
            # Exit occurs at candle close: all intrabar price action preceded the close!
            excursion_status = TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT
            confirmed_mfe = max(confirmed_mfe, candle_raw_fav)
            confirmed_mae = max(confirmed_mae, candle_raw_adv)

        # -----------------------------------------------------------------
        # Condition 6: Max Holding Bars Exceeded
        # -----------------------------------------------------------------
        elif self.max_holding_bars is not None and trade.bars_held >= self.max_holding_bars:
            exit_triggered = True
            exit_price = c_close
            exit_reason = ExitReason.MAX_BARS_TIME_EXIT
            outcome_type = SpotOutcomeType.TIME_EXIT
            excursion_status = TerminalCandleExcursionStatus.CONFIRMED_PRE_EXIT
            confirmed_mfe = max(confirmed_mfe, candle_raw_fav)
            confirmed_mae = max(confirmed_mae, candle_raw_adv)

        if not exit_triggered:
            return None

        # Build auditable outcome record
        self._record_counter += 1
        rec_id = f"spot_out_{trade.signal_id}_{self._record_counter:06d}"

        pnl = (exit_price - entry) if direction == "BULLISH" else (entry - exit_price)
        r_val = trade.initial_risk_points if trade.initial_risk_points > 0 else 1.0

        return SpotOutcomeRecord(
            record_id=rec_id,
            signal_id=trade.signal_id,
            signal_timestamp=trade.signal_timestamp.isoformat(),
            direction=trade.direction,
            entry_reference_price=trade.entry_reference_price,
            entry_timestamp=trade.entry_timestamp.isoformat(),
            entry_convention=trade.entry_convention,
            slippage_points=trade.slippage_points,
            structural_stop_loss=trade.structural_stop_loss,
            structural_target=trade.structural_target,
            initial_risk_points=trade.initial_risk_points,
            first_eligible_candle_timestamp=trade.first_eligible_candle_timestamp.isoformat(),
            exit_timestamp=c_end.isoformat(),
            exit_price=round(exit_price, 2),
            exit_reason=exit_reason.value,
            outcome_classification=outcome_type.value,
            mfe_points=round(confirmed_mfe, 2),
            mae_points=round(confirmed_mae, 2),
            mfe_r_multiple=round(confirmed_mfe / r_val, 3),
            mae_r_multiple=round(confirmed_mae / r_val, 3),
            terminal_candle_excursion_status=excursion_status.value,
            terminal_candle_max_favourable_bound=max(confirmed_mfe, candle_raw_fav),
            terminal_candle_max_adverse_bound=max(confirmed_mae, candle_raw_adv),
            underlying_points_pnl=round(pnl, 2),
            bars_held=trade.bars_held,
            setup_score=trade.setup_score,
            canonical_gates_passed=trade.canonical_gates_passed,
            is_counterfactual=trade.is_counterfactual,
            failed_gates=list(trade.failed_gates),
            gate_rejection_reasons=list(trade.gate_rejection_reasons),
            data_quality_flags=list(trade.data_quality_flags),
            execution_metadata={
                "underlying_signal_only": True,
                "option_execution_eligible": False,
                "option_execution_status": "UNAVAILABLE",
                "overlapping_policy": self.overlapping_policy.value,
                "data_source": "ANGEL_ONE_HISTORICAL_SPOT",
                "exit_candle_start": c_start.isoformat(),
                "exit_candle_end": c_end.isoformat(),
                "configured_time_exit": str(self.intraday_time_exit),
                "disclaimer": "Simulated underlying spot tracking. Untradable index; not option P&L."
            }
        )


class SpotOutcomeAttributor:
    """
    Independent attribution analyzer for spot outcome records.
    Enforces strict cohort separation (Phase 3D.2):
    - Accepted Phase 2C signal observations form the headline strategy performance summary.
    - Rejected counterfactual observations are computed independently.
    - Combined research diagnostics are isolated into an explicitly labeled supplementary section.
    - Terminology uses signal observations, avoiding execution-implying terms.
    - Explicit denominators and separated counts for invalidated, missed, unresolved, and dataset-end.
    """

    @staticmethod
    def calculate_metrics(records: List[SpotOutcomeRecord]) -> Dict[str, Any]:
        """Calculates core statistical attribution metrics with explicit denominators."""
        total_observed = len(records)
        if total_observed == 0:
            return {
                "total_observations": 0,
                "eligible_observations": 0,
                "resolved_observations": 0,
                "target_reached_count": 0,
                "stop_loss_hit_count": 0,
                "time_exit_count": 0,
                "unresolved_count": 0,
                "dataset_end_unresolved_count": 0,
                "entry_invalidated_count": 0,
                "missed_entry_count": 0,
                "excluded_non_eligible_count": 0,
                "target_rate_pct_of_resolved": 0.0,
                "target_rate_pct_of_eligible": 0.0,
                "stop_rate_pct_of_resolved": 0.0,
                "stop_rate_pct_of_eligible": 0.0,
                "time_exit_rate_pct_of_resolved": 0.0,
                "avg_confirmed_mfe_points": 0.0,
                "avg_confirmed_mae_points": 0.0,
                "total_points_pnl": 0.0,
                "expectancy_per_resolved_pts": 0.0,
                "expectancy_per_eligible_pts": 0.0,
                "avg_bars_held_resolved": 0.0,
                "denominators": {
                    "resolved_denominator": 0,
                    "eligible_denominator": 0,
                    "observed_denominator": 0
                }
            }

        # Filter categories (Audit Item 6: Separately counted)
        invalid_cnt = sum(1 for r in records if r.outcome_classification == SpotOutcomeType.ENTRY_INVALIDATED.value)
        missed_cnt = sum(1 for r in records if r.outcome_classification == SpotOutcomeType.MISSED_ENTRY.value)
        excluded_cnt = sum(1 for r in records if r.outcome_classification == SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value)
        dataset_end_cnt = sum(
            1 for r in records
            if r.exit_reason in (ExitReason.DATASET_END_UNRESOLVED.value, ExitReason.SESSION_END_UNRESOLVED.value)
        )

        eligible_records = [
            r for r in records
            if r.outcome_classification not in (
                SpotOutcomeType.ENTRY_INVALIDATED.value,
                SpotOutcomeType.MISSED_ENTRY.value,
                SpotOutcomeType.EXCLUDED_NON_ELIGIBLE.value
            )
        ]
        n_eligible = len(eligible_records)

        resolved_records = [
            r for r in eligible_records
            if r.outcome_classification in (
                SpotOutcomeType.TARGET_REACHED.value,
                SpotOutcomeType.STOP_LOSS_HIT.value,
                SpotOutcomeType.TIME_EXIT.value
            )
        ]
        n_resolved = len(resolved_records)
        unresolved_cnt = sum(1 for r in eligible_records if r.outcome_classification == SpotOutcomeType.UNRESOLVED.value)

        target_cnt = sum(1 for r in resolved_records if r.outcome_classification == SpotOutcomeType.TARGET_REACHED.value)
        stop_cnt = sum(1 for r in resolved_records if r.outcome_classification == SpotOutcomeType.STOP_LOSS_HIT.value)
        time_cnt = sum(1 for r in resolved_records if r.outcome_classification == SpotOutcomeType.TIME_EXIT.value)

        tot_resolved_pnl = sum(r.underlying_points_pnl or 0.0 for r in resolved_records)
        tot_eligible_pnl = sum(r.underlying_points_pnl or 0.0 for r in eligible_records)
        tot_mfe = sum(r.mfe_points for r in eligible_records)
        tot_mae = sum(r.mae_points for r in eligible_records)
        tot_bars = sum(r.bars_held for r in resolved_records)

        return {
            "total_observations": total_observed,
            "eligible_observations": n_eligible,
            "resolved_observations": n_resolved,
            "target_reached_count": target_cnt,
            "stop_loss_hit_count": stop_cnt,
            "time_exit_count": time_cnt,
            "unresolved_count": unresolved_cnt,
            "dataset_end_unresolved_count": dataset_end_cnt,
            "entry_invalidated_count": invalid_cnt,
            "missed_entry_count": missed_cnt,
            "excluded_non_eligible_count": excluded_cnt,
            "target_rate_pct_of_resolved": round((target_cnt / n_resolved) * 100.0, 2) if n_resolved > 0 else 0.0,
            "target_rate_pct_of_eligible": round((target_cnt / n_eligible) * 100.0, 2) if n_eligible > 0 else 0.0,
            "stop_rate_pct_of_resolved": round((stop_cnt / n_resolved) * 100.0, 2) if n_resolved > 0 else 0.0,
            "stop_rate_pct_of_eligible": round((stop_cnt / n_eligible) * 100.0, 2) if n_eligible > 0 else 0.0,
            "time_exit_rate_pct_of_resolved": round((time_cnt / n_resolved) * 100.0, 2) if n_resolved > 0 else 0.0,
            "avg_confirmed_mfe_points": round(tot_mfe / n_eligible, 2) if n_eligible > 0 else 0.0,
            "avg_confirmed_mae_points": round(tot_mae / n_eligible, 2) if n_eligible > 0 else 0.0,
            "total_points_pnl": round(tot_resolved_pnl, 2),
            "expectancy_per_resolved_pts": round(tot_resolved_pnl / n_resolved, 2) if n_resolved > 0 else 0.0,
            "expectancy_per_eligible_pts": round(tot_eligible_pnl / n_eligible, 2) if n_eligible > 0 else 0.0,
            "avg_bars_held_resolved": round(tot_bars / n_resolved, 1) if n_resolved > 0 else 0.0,
            "denominators": {
                "resolved_denominator": n_resolved,
                "eligible_denominator": n_eligible,
                "observed_denominator": total_observed
            }
        }

    @classmethod
    def generate_full_attribution_report(
        cls,
        records: List[SpotOutcomeRecord],
        tracker_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generates comprehensive multi-dimensional attribution with strict cohort separation (Phase 3D.2):
        1. Headline Performance Summary: ACCEPTED observations only.
        2. Rejected Counterfactual Summary: REJECTED observations only.
        3. Supplementary Combined Research Diagnostics: Clearly labeled research aggregate.
        4. Setup score tiers & direction: Evaluated on accepted observations.
        5. Specific failed gates: Evaluated on counterfactual observations.
        """
        # 1. Stratify Cohorts strictly
        accepted_records = [r for r in records if r.canonical_gates_passed and not r.is_counterfactual]
        counterfactual_records = [r for r in records if r.is_counterfactual]

        accepted_summary = cls.calculate_metrics(accepted_records)
        counterfactual_summary = cls.calculate_metrics(counterfactual_records)
        supplementary_combined = cls.calculate_metrics(records)

        by_cohort = {
            "ACCEPTED_SIGNAL_OBSERVATIONS": accepted_summary,
            "HYPOTHETICAL_REJECTED_COUNTERFACTUALS": counterfactual_summary
        }

        # 2. Stratify Setup Score Tiers on Accepted observations only
        by_score_tier_accepted = {
            "SCORE_80_PLUS": cls.calculate_metrics([r for r in accepted_records if r.setup_score >= 80.0]),
            "SCORE_70_TO_79": cls.calculate_metrics([r for r in accepted_records if 70.0 <= r.setup_score < 80.0]),
            "SCORE_60_TO_69": cls.calculate_metrics([r for r in accepted_records if 60.0 <= r.setup_score < 70.0]),
            "SCORE_BELOW_60": cls.calculate_metrics([r for r in accepted_records if r.setup_score < 60.0])
        }

        # 3. Stratify Direction on Accepted observations only
        by_direction_accepted = {
            "BULLISH": cls.calculate_metrics([r for r in accepted_records if r.direction == "BULLISH"]),
            "BEARISH": cls.calculate_metrics([r for r in accepted_records if r.direction == "BEARISH"])
        }

        # 4. Stratify Specific Failed Gates on Counterfactual observations only
        gate_breakdown: Dict[str, List[SpotOutcomeRecord]] = {}
        for r in counterfactual_records:
            for g in r.failed_gates:
                gate_breakdown.setdefault(g, []).append(r)

        by_failed_gate = {
            g_id: cls.calculate_metrics(g_records)
            for g_id, g_records in sorted(gate_breakdown.items())
        }

        # 5. Exit Reason Breakdown on Accepted records and All records
        exit_reasons_accepted: Dict[str, int] = {}
        for r in accepted_records:
            exit_reasons_accepted[r.exit_reason] = exit_reasons_accepted.get(r.exit_reason, 0) + 1

        exit_reasons_all: Dict[str, int] = {}
        for r in records:
            exit_reasons_all[r.exit_reason] = exit_reasons_all.get(r.exit_reason, 0) + 1

        # 6. Terminal Candle Excursion Status Distribution
        excursion_status_count: Dict[str, int] = {}
        for r in records:
            excursion_status_count[r.terminal_candle_excursion_status] = (
                excursion_status_count.get(r.terminal_candle_excursion_status, 0) + 1
            )

        # Documented metadata with exact bars_held convention
        meta = tracker_metadata or {
            "time_exit_configuration": {
                "intraday_time_exit": "15:25:00",
                "latest_valid_observation_boundary": "15:15:00",
                "timezone": "Asia/Kolkata",
                "nature": "CONFIGURABLE_RESEARCH_ASSUMPTION",
                "exchange_mandated": False,
                "exit_reference": "CANDLE_CLOSE_AT_CUTOFF",
                "dataset_cutoff_observed": True,
                "dataset_cutoff_cause": "BROKER_HISTORICAL_FEED_INTRADAY_TRUNCATION",
                "dataset_cutoff_cause_verified": False,
                "fabricated_exit_prevention": "ENFORCED"
            },
            "sample_warning": (
                "Pipeline smoke-test observation only. N=1 accepted signal observation. "
                "Not strategy validation, statistical validation, or evidence of profitability."
            ),
            "bars_held_convention": {
                "unit": "1_MINUTE_CLOSED_BARS",
                "counting_rule": (
                    "Inclusive of the initial entry candle (Bar 1) and the terminal exit candle. "
                    "Each subsequent 1m closed bar trade remains active increments bars_held by 1. "
                    "Non-eligible or invalidated observations have bars_held = 0."
                ),
                "example": "Entered at 09:30:00 (Bar 1: 09:30-09:31) and exited at 09:31:30 (Bar 2: 09:31-09:32) = 2 bars held."
            },
            "overlapping_policy": "INDEPENDENT_SIGNAL_EVENTS"
        }

        return {
            "headline_performance_summary": accepted_summary,  # Headline metrics: Accepted observations ONLY
            "accepted_signal_observations_summary": accepted_summary,
            "rejected_counterfactual_summary": counterfactual_summary,
            "cohort_breakdown": by_cohort,
            "by_canonical_gate_status": by_cohort,  # Renamed to ACCEPTED_SIGNAL_OBSERVATIONS
            "by_setup_score_tier": by_score_tier_accepted,
            "by_direction": by_direction_accepted,
            "by_failed_gate": by_failed_gate,
            "exit_reason_distribution": exit_reasons_accepted,
            "terminal_candle_excursion_distribution": excursion_status_count,
            "supplementary_combined_research_diagnostics": {
                "disclaimer": (
                    "RESEARCH DIAGNOSTICS ONLY: Combined aggregate of accepted signals and hypothetical "
                    "rejected counterfactuals. Do NOT cite as strategy performance."
                ),
                "metrics": supplementary_combined,
                "exit_reason_distribution_combined": exit_reasons_all
            },
            "overall_summary": accepted_summary,  # Compatibility alias pointing strictly to accepted cohort
            "experiment_metadata": meta,
            "audit_qualifications": {
                "asset_class": "NIFTY_50_SPOT_INDEX",
                "nature": "GENUINE_BROKER_SOURCED_HISTORICAL_INDEX_OHLC",
                "tradability": "UNTRADABLE_REFERENCE_INDEX",
                "option_execution": "STRICTLY_UNAVAILABLE",
                "probability_status": "EMPIRICAL_SAMPLE_FREQUENCY_ONLY",
                "claim_restriction": "NO_WIN_PROBABILITY_OR_PROFITABILITY_CLAIMS_AUTHORISED",
                "sample_qualification": "PIPELINE_SMOKE_TEST_OBSERVATION_NOT_STATISTICAL_VALIDATION",
                "fill_model": "SIMULATED_REFERENCE_PRICE_TOUCHES_NOT_GUARANTEED_FILLS",
                "cohort_integrity": "HEADLINE_METRICS_COMPUTED_STRICTLY_ON_ACCEPTED_SIGNAL_OBSERVATIONS_ONLY"
            }
        }
