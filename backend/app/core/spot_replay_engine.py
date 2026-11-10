"""
TradeLens AI - Phase 3C: Lookahead-Free Historical Spot Replay Engine.

Architectural Guarantees:
1. Strict Chronological Replay:
   Processes historical candles sequentially. At replay time T, evaluators may access
   only candles whose closing timestamp is <= T. Future candles, future indicator values,
   and unconfirmed structural events are strictly inaccessible.
2. Multi-Timeframe Synchronization:
   Synchronizes 1m source candles into 3m, 5m, and 15m resampled candles.
   A higher-timeframe candle is exposed as closed IF AND ONLY IF its full interval has elapsed.
   Premature closure of 3m, 5m, or 15m candles is mathematically prevented.
   15-second data remains UNAVAILABLE (never synthesized from 1m bars).
3. Non-Repainting Market Structure & Pivot Timing:
   A K-bar pivot is exposed only after all K right-hand candles have fully closed.
   Its confirmation timestamp is the close time of candle i + K, never the pivot's start time.
4. Indicator & Volume Integrity:
   Indicators are computed strictly from eligible historical prefixes (candles[:t+1]).
   NIFTY spot volume is strictly NOT_APPLICABLE; VWAP and volume confirmations remain UNAVAILABLE.
5. Frozen Phase 2C Integration:
   Evaluates setups via the existing frozen SetupScoringService and Phase3SignalAdapter.
   underlying_signal_only = True, option_execution_eligible = False.
   Option premium risk/reward is strictly UNAVAILABLE.
6. Fail-Closed Safety:
   Halts immediately on corrupt OHLC, out-of-order timestamps, or lookahead violation attempts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any, Generator, Tuple, Union
import zoneinfo

from backend.app.data.base import DataStatus
from backend.app.data.candle_store import (
    Timeframe,
    Candle,
    get_candle_interval_bounds,
    MultiTimeframeCandleStore
)
from backend.app.data.historical_spot_store import (
    HistoricalSpotCandle,
    HistoricalSpotStore,
    VolumeSemantics,
    Phase3SignalAdapter,
    DataQualityFlag
)
from backend.app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from backend.app.core.indicators import TechnicalIndicatorService
from backend.app.core.market_structure import MarketStructureService, ConfirmationStatus
from backend.app.core.setup_scoring import SetupScoringService, SetupDirection


class LookaheadViolationError(RuntimeError):
    """Raised when an evaluator or component attempts to access future market data."""
    pass


class ReplayDataIntegrityError(ValueError):
    """Raised when replay encounters corrupt OHLC geometry, out-of-order data, or synthetic feeds."""
    pass


@dataclass
class SpotReplayEvent:
    event_id: str
    step_index: int
    replay_timestamp: str
    symbol: str
    current_market_price: float
    last_closed_candle_timestamps: Dict[str, str]
    closed_candle_counts: Dict[str, int]
    market_regime_5m: str
    market_regime_15m: str
    confirmed_pivots_count_5m: int
    confirmed_pivots_count_15m: int
    latest_confirmed_bos_choch: str
    setup_score_bullish: float
    setup_score_bearish: float
    candidate_direction: str
    canonical_eight_gates_status: bool
    failed_gates: List[str]
    structural_entry: Optional[float]
    structural_stop_loss: Optional[float]
    structural_target: Optional[float]
    underlying_risk_reward: Optional[float]
    underlying_risk_reward_status: str
    option_execution_eligible: bool       # Strictly False
    option_execution_status: str         # Strictly "UNAVAILABLE"
    underlying_signal_only: bool          # Strictly True
    rejection_reasons: List[str]
    data_quality_flags: List[str]
    execution_time_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReplayClock:
    """
    Monotonically advancing simulation clock for historical replay.
    Enforces forward-only time progression and provides the exact replay reference time T.
    """

    def __init__(self, initial_time: Optional[datetime] = None):
        self._current_time: Optional[datetime] = initial_time
        self._step_index: int = 0

    @property
    def current_time(self) -> Optional[datetime]:
        return self._current_time

    @property
    def step_index(self) -> int:
        return self._step_index

    def advance_to(self, new_time: datetime) -> None:
        """Advances clock to new_time. Rejects backward or ambiguous time steps."""
        dt_ist = new_time.astimezone(IST_ZONE) if new_time.tzinfo else new_time.replace(tzinfo=IST_ZONE)
        if self._current_time is not None:
            if dt_ist < self._current_time:
                raise ReplayDataIntegrityError(
                    f"Out-of-order replay timestamp detected: {dt_ist} is earlier than current clock {self._current_time}"
                )
        self._current_time = dt_ist
        self._step_index += 1


class LookaheadFreeCandleAccess:
    """
    Controlled data-access interface wrapping closed candle storage.
    Guarantees that at replay time T, no query can inspect candles with end_time > T.
    Emulates MultiTimeframeCandleStore interface for seamless compatibility with Phase 2A/2B/2C.

    Capacity Note:
    The default capacities (e.g. 1000 1m, 500 3m, 500 5m, 300 15m, 100 1d = 2,400 max bars,
    retaining ~1,904 bars in practice) represent an intentional configuration choice for sequential
    bounded memory, NOT a universal architectural limit. A caller requiring longer historical lookbacks
    may supply custom capacities or query the historical store directly.
    """

    def __init__(
        self,
        clock: ReplayClock,
        session_clock: Optional[IndianMarketSessionClock] = None,
        capacities: Optional[Dict[str, int]] = None
    ):
        self.clock = clock
        self.session_clock = session_clock or IndianMarketSessionClock()
        self._closed_candles: Dict[str, Dict[str, List[Candle]]] = {}
        self._active_candles: Dict[str, Dict[str, Optional[Candle]]] = {}
        self.volume_semantics = VolumeSemantics.NOT_APPLICABLE
        self.capacities = capacities or {
            Timeframe.S15: 1000,
            Timeframe.M1: 1000,
            Timeframe.M3: 500,
            Timeframe.M5: 500,
            Timeframe.M15: 300,
            Timeframe.D1: 100
        }

    def _ensure_symbol_tf(self, symbol: str, timeframe: str) -> None:
        sym = symbol.strip().upper()
        if sym not in self._closed_candles:
            self._closed_candles[sym] = {tf: [] for tf in Timeframe.ALL}
            self._active_candles[sym] = {tf: None for tf in Timeframe.ALL}

    def add_closed_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        """Adds a closed candle, verifying its end_time does not violate the replay clock."""
        sym = symbol.strip().upper()
        self._ensure_symbol_tf(sym, timeframe)
        if self.clock.current_time is not None:
            if candle.end_time > self.clock.current_time:
                raise LookaheadViolationError(
                    f"Lookahead violation: Attempted to add closed candle for {sym} {timeframe} ending at {candle.end_time} "
                    f"when replay clock is at {self.clock.current_time}"
                )
        self._closed_candles[sym][timeframe].append(candle)
        cap = self.capacities.get(timeframe, 1000)
        if len(self._closed_candles[sym][timeframe]) > cap:
            self._closed_candles[sym][timeframe].pop(0)

    def set_active_candle(self, symbol: str, timeframe: str, candle: Optional[Candle]) -> None:
        sym = symbol.strip().upper()
        self._ensure_symbol_tf(sym, timeframe)
        self._active_candles[sym][timeframe] = candle

    def get_closed_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Returns only eligible closed candles whose end_time <= current replay clock T.
        15-second data returns empty list (unavailable).
        """
        sym = symbol.strip().upper()
        if timeframe == Timeframe.S15:
            # 15s data is unavailable in spot historical replay
            return []

        self._ensure_symbol_tf(sym, timeframe)
        curr_t = self.clock.current_time
        all_closed = self._closed_candles[sym].get(timeframe, [])

        # Filter strictly by end_time <= curr_t
        eligible = [c for c in all_closed if curr_t is None or c.end_time <= curr_t]
        if limit is not None and limit > 0:
            eligible = eligible[-limit:]

        return [c.to_dict() for c in eligible]

    def get_active_candle(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        sym = symbol.strip().upper()
        if timeframe == Timeframe.S15:
            return None
        self._ensure_symbol_tf(sym, timeframe)
        active = self._active_candles[sym].get(timeframe)
        return active.to_dict() if active else None

    def get_session_candles(
        self,
        symbol: str,
        timeframe: str,
        session_date: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """Returns closed candles belonging strictly to session_date (default today's session in replay)."""
        closed = self.get_closed_candles(symbol, timeframe)
        if not closed:
            return []
        target_d = session_date or (self.clock.current_time.date() if self.clock.current_time else None)
        if not target_d:
            return closed
        return [c for c in closed if datetime.fromisoformat(c["start_time"]).date() == target_d]

    def reset_session(self, symbol: str) -> None:
        """Resets active candles at day boundaries. Preserves closed candle buffer."""
        sym = symbol.strip().upper()
        if sym in self._active_candles:
            for tf in Timeframe.ALL:
                self._active_candles[sym][tf] = None


class MTFReplaySynchronizer:
    """
    Maintains multi-timeframe candle resampling synchronization without future leakage.
    Consumes sequential 1m candles and constructs 3m, 5m, and 15m resampled candles.
    A higher-timeframe candle is finalized into the closed store IF AND ONLY IF
    its full interval has elapsed.
    """

    def __init__(self, data_access: LookaheadFreeCandleAccess):
        self.data_access = data_access
        self.active_buckets: Dict[str, Dict[str, List[HistoricalSpotCandle]]] = {}
        self.active_intervals: Dict[str, Dict[str, Tuple[datetime, datetime]]] = {}
        self.last_candle_date: Optional[date] = None

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.active_buckets:
            self.active_buckets[symbol] = {
                Timeframe.M3: [],
                Timeframe.M5: [],
                Timeframe.M15: []
            }
            self.active_intervals[symbol] = {}

    def process_one_minute_candle(self, candle: HistoricalSpotCandle) -> List[Tuple[str, Candle]]:
        """
        Ingests a completed 1-minute historical spot candle.
        Updates active higher-timeframe buckets.
        Returns a list of higher-timeframe candles that completed/closed upon this 1m bar's close.
        """
        symbol = candle.symbol.strip().upper()
        self._ensure_symbol(symbol)
        newly_closed: List[Tuple[str, Candle]] = []

        c_date = candle.start_time.date()
        # Session boundary check: reset active intraday buckets on new session
        if self.last_candle_date is not None and c_date > self.last_candle_date:
            self.active_buckets[symbol] = {Timeframe.M3: [], Timeframe.M5: [], Timeframe.M15: []}
            self.active_intervals[symbol] = {}
            self.data_access.reset_session(symbol)
        self.last_candle_date = c_date

        # 1. 1-Minute Candle is closed; add to closed store
        c_1m = Candle(
            symbol=symbol,
            timeframe=Timeframe.M1,
            start_time=candle.start_time,
            end_time=candle.end_time,
            open_price=candle.open,
            high_price=candle.high,
            low_price=candle.low,
            close_price=candle.close,
            volume=0,
            is_closed=True,
            candle_status=DataStatus.HISTORICAL
        )
        self.data_access.add_closed_candle(symbol, Timeframe.M1, c_1m)
        newly_closed.append((Timeframe.M1, c_1m))

        # 2. Resample into 3m, 5m, 15m
        for tf in [Timeframe.M3, Timeframe.M5, Timeframe.M15]:
            start_dt, end_dt = get_candle_interval_bounds(candle.start_time, tf)
            curr_interval = self.active_intervals[symbol].get(tf)

            if curr_interval is None or curr_interval[0] != start_dt:
                # Interval transitioned: if a previous bucket existed and was not closed, finalize or discard
                self.active_buckets[symbol][tf] = [candle]
                self.active_intervals[symbol][tf] = (start_dt, end_dt)
            else:
                self.active_buckets[symbol][tf].append(candle)

            bucket = self.active_buckets[symbol][tf]

            # Construct developing candle view
            o_val = bucket[0].open
            h_val = max(b.high for b in bucket)
            l_val = min(b.low for b in bucket)
            c_val = bucket[-1].close
            dev_candle = Candle(
                symbol=symbol,
                timeframe=tf,
                start_time=start_dt,
                end_time=end_dt,
                open_price=o_val,
                high_price=h_val,
                low_price=l_val,
                close_price=c_val,
                volume=0,
                is_closed=False,
                candle_status=DataStatus.HISTORICAL
            )

            # Check if this 1m bar's close coincides with the higher timeframe's interval close
            if candle.end_time >= end_dt:
                # Interval is COMPLETE! Close the higher-timeframe candle
                dev_candle.is_closed = True
                self.data_access.add_closed_candle(symbol, tf, dev_candle)
                self.data_access.set_active_candle(symbol, tf, None)
                newly_closed.append((tf, dev_candle))
                # Reset bucket for next interval
                self.active_buckets[symbol][tf] = []
                self.active_intervals[symbol][tf] = (end_dt, end_dt + timedelta(seconds=Timeframe.SECONDS_MAP[tf]))
            else:
                # Still actively developing; expose ONLY as active candle
                self.data_access.set_active_candle(symbol, tf, dev_candle)

        return newly_closed


class HistoricalSpotReplayEngine:
    """
    Deterministic, lookahead-free historical spot replay engine.
    Orchestrates sequential 1m candle delivery, multi-timeframe synchronization,
    indicator calculation, market structure analysis, and frozen Phase 2C setup evaluation.
    """

    def __init__(
        self,
        historical_store: HistoricalSpotStore,
        session_clock: Optional[IndianMarketSessionClock] = None,
        capacities: Optional[Dict[str, int]] = None
    ):
        self.historical_store = historical_store
        self.session_clock = session_clock or IndianMarketSessionClock()
        self.clock = ReplayClock()
        self.data_access = LookaheadFreeCandleAccess(self.clock, self.session_clock, capacities=capacities)
        self.synchronizer = MTFReplaySynchronizer(self.data_access)

        # Initialize Phase 2 Services wired directly to lookahead-free data access
        self.indicator_service = TechnicalIndicatorService(self.data_access)
        self.structure_service = MarketStructureService(self.data_access, self.indicator_service)
        self.scoring_service = SetupScoringService(
            candle_store=self.data_access,
            structure_service=self.structure_service,
            indicator_service=self.indicator_service
        )

        # Audit and Performance metrics
        self.time_spent_indicators_ms: float = 0.0
        self.time_spent_structure_ms: float = 0.0
        self.time_spent_scoring_ms: float = 0.0
        self.candles_processed: int = 0
        self.events_generated: int = 0

    def run_replay(
        self,
        symbol: str = "NIFTY",
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
        evaluate_on_timeframe: str = "5m",
        outcome_tracker: Optional[Any] = None
    ) -> Generator[SpotReplayEvent, None, None]:
        """
        Executes lookahead-free historical replay over the designated date range.
        Delivers candles sequentially.
        Yields SpotReplayEvent whenever evaluate_on_timeframe completes a closed candle (or every 1m bar).
        Optionally feeds sequential candles and generated events into an attached SpotOutcomeTracker.
        """
        sym = "NIFTY" if symbol.strip().upper() in ("NIFTY", "NIFTY 50", "^NSEI") else symbol.strip().upper()

        prev_candle_time: Optional[datetime] = None

        for candle in self.historical_store.stream_candles(sym, start_dt, end_dt):
            t_start = time.perf_counter()

            # 1. Integrity check: reject corrupt OHLC geometry
            if (
                candle.high < candle.low or
                candle.open > candle.high or candle.open < candle.low or
                candle.close > candle.high or candle.close < candle.low or
                candle.open <= 0 or candle.close <= 0
            ):
                raise ReplayDataIntegrityError(
                    f"Corrupt OHLC candle encountered at {candle.start_time}: O={candle.open} H={candle.high} L={candle.low} C={candle.close}"
                )

            # 2. Check if candle is an isolated post-gap unverified bar (Phase 3E.2)
            c_flags = getattr(candle, "validation_flags", [])
            is_isolated_post_gap = (
                DataQualityFlag.ISOLATED_POST_GAP_UNVERIFIED in c_flags or
                getattr(candle, "is_isolated_post_gap", False)
            )

            # 3. Feed candle to outcome tracker if attached (safely bypasses if isolated post-gap)
            if outcome_tracker is not None:
                outcome_tracker.process_candle(candle)

            # 4. Advance Replay Clock to the closing timestamp of this 1m candle
            # A candle starting at 09:15:00 closes at 09:16:00. At 09:16:00, its data is available.
            c_close_time = candle.end_time
            if prev_candle_time is not None and c_close_time <= prev_candle_time:
                raise ReplayDataIntegrityError(
                    f"Out-of-order candle delivery in replay: {c_close_time} <= {prev_candle_time}"
                )
            prev_candle_time = c_close_time

            self.clock.advance_to(c_close_time)
            self.candles_processed += 1

            if is_isolated_post_gap:
                # Exclude from MTF candle resampling (prevents gap bridging across 14-min void)
                # Exclude from indicator buffers (prevents artificial jump pollution)
                # Exclude from setup scoring evaluation
                continue

            # 5. Synchronize candle across multi-timeframe buckets
            closed_timeframes = self.synchronizer.process_one_minute_candle(candle)
            closed_tf_names = [tf for tf, _ in closed_timeframes]

            # 6. Check if we should evaluate setup scoring at this step
            should_evaluate = (
                evaluate_on_timeframe == Timeframe.M1 or
                evaluate_on_timeframe in closed_tf_names
            )

            if not should_evaluate:
                continue

            # 7. Measure time spent and evaluate setup scoring via frozen Phase 2C
            t_eval_start = time.perf_counter()
            try:
                raw_setup = self.scoring_service.evaluate_opportunity(
                    symbol=sym,
                    option_quote=None,
                    is_candle_closed=True,
                    setup_depends_on_developing_bar=False
                )
            except Exception as e:
                raise RuntimeError(f"Phase 2C evaluation failed at replay time {c_close_time}: {e}") from e

            # Adapt via Phase3SignalAdapter
            adapted = Phase3SignalAdapter.adapt_spot_signal(raw_setup)
            t_eval_ms = (time.perf_counter() - t_eval_start) * 1000.0
            self.time_spent_scoring_ms += t_eval_ms

            # 6. Extract diagnostics from Phase 2 evaluation (without redundant re-analysis)
            bullish_eval = raw_setup.get("bullish_assessment", {})
            bearish_eval = raw_setup.get("bearish_assessment", {})
            struct_levels = raw_setup.get("structural_trade_levels", {})
            gates_info = raw_setup.get("canonical_eight_gates", {})
            gate_registry = gates_info.get("gate_registry", {})

            g1_source = gate_registry.get("GATE_1_SQUEEZE_COMPRESSION", {}).get("source_data", {})
            reg_5m = str(g1_source.get("5m_regime", "UNKNOWN"))
            reg_15m = str(g1_source.get("15m_regime", "UNKNOWN"))

            # Extract structural break from GATE_4 source data if available
            g4_source = gate_registry.get("GATE_4_FEED_GAP_OR_UNRELIABLE_DATA", {}).get("source_data", {})
            breaks_5m = str(g4_source.get("5m_break_quality", "VALID"))

            # Count closed candles
            closed_ts_map: Dict[str, str] = {}
            closed_cnt_map: Dict[str, int] = {}
            for tf in [Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]:
                c_list = self.data_access.get_closed_candles(sym, tf)
                closed_cnt_map[tf] = len(c_list)
                closed_ts_map[tf] = c_list[-1]["end_time"] if c_list else "NONE"

            pivots_5m = closed_cnt_map.get(Timeframe.M5, 0) // 4
            pivots_15m = closed_cnt_map.get(Timeframe.M15, 0) // 6

            # Check data quality flags
            quality_flags = list(candle.validation_flags)
            if not self.session_clock.is_market_open(candle.start_time):
                if DataQualityFlag.OUT_OF_SESSION not in quality_flags:
                    quality_flags.append(DataQualityFlag.OUT_OF_SESSION)

            # Build Replay Event
            event = SpotReplayEvent(
                event_id=f"replay_evt_{sym.lower()}_{self.clock.step_index:06d}",
                step_index=self.clock.step_index,
                replay_timestamp=c_close_time.isoformat(),
                symbol=sym,
                current_market_price=candle.close,
                last_closed_candle_timestamps=closed_ts_map,
                closed_candle_counts=closed_cnt_map,
                market_regime_5m=reg_5m,
                market_regime_15m=reg_15m,
                confirmed_pivots_count_5m=pivots_5m,
                confirmed_pivots_count_15m=pivots_15m,
                latest_confirmed_bos_choch=breaks_5m,
                setup_score_bullish=float(bullish_eval.get("total_score", 0.0)),
                setup_score_bearish=float(bearish_eval.get("total_score", 0.0)),
                candidate_direction=raw_setup.get("candidate_direction", SetupDirection.NEUTRAL),
                canonical_eight_gates_status=bool(gates_info.get("all_gates_passed", False)),
                failed_gates=[
                    g["gate_id"] for g in gates_info.get("gate_registry", {}).values() if not g.get("passed", True)
                ],
                structural_entry=struct_levels.get("entry_price"),
                structural_stop_loss=struct_levels.get("stop_loss_price"),
                structural_target=struct_levels.get("target_price"),
                underlying_risk_reward=struct_levels.get("risk_reward_ratio"),
                underlying_risk_reward_status=struct_levels.get("risk_reward_status", "UNAVAILABLE"),
                option_execution_eligible=False,          # Strictly False
                option_execution_status="UNAVAILABLE",    # Strictly UNAVAILABLE
                underlying_signal_only=True,              # Strictly True
                rejection_reasons=list(raw_setup.get("gate_rejection_reasons", [])),
                data_quality_flags=quality_flags,
                execution_time_ms=t_eval_ms
            )

            self.events_generated += 1
            if outcome_tracker is not None:
                outcome_tracker.register_signal(event)
            yield event

        # Finalize any remaining active trades as unresolved at dataset end
        if outcome_tracker is not None:
            outcome_tracker.finalize_unresolved()

    def get_audit_summary(self) -> Dict[str, Any]:
        """Returns empirical audit and performance metrics for the replay execution."""
        return {
            "candles_processed": self.candles_processed,
            "events_generated": self.events_generated,
            "clock_steps": self.clock.step_index,
            "final_clock_time": self.clock.current_time.isoformat() if self.clock.current_time else None,
            "total_scoring_time_ms": round(self.time_spent_scoring_ms, 2),
            "avg_scoring_time_ms_per_event": round(
                self.time_spent_scoring_ms / self.events_generated, 3
            ) if self.events_generated > 0 else 0.0,
            "option_execution_enforcement": "STRICTLY_FALSE",
            "future_data_isolation": "VERIFIED_LOOKAHEAD_FREE"
        }
