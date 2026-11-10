"""
TradeLens AI - Multi-Timeframe Candle Resampling & Synchronization Store
Provides deterministic aggregation and storage of OHLCV candles across:
- 15 seconds (15s)
- 1 minute (1m)
- 3 minutes (3m)
- 5 minutes (5m)
- 15 minutes (15m)
- 1 day (1d)

Guarantees:
1. Strict session clock alignment relative to 09:15:00 IST session open.
2. Complete segregation of completed (closed) candles from active forming candles.
3. Zero repainting: Signal engines inspect ONLY closed candles.
4. Resampling preservation:
   Open = Open_first, High = max(High), Low = min(Low), Close = Close_last, Volume = sum(Volume).
"""
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Any, Optional, Tuple, Union
import zoneinfo

from .base import DataStatus
from .session_clock import IndianMarketSessionClock, IST_ZONE


class Timeframe:
    S15 = "15s"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    D1 = "1d"

    ALL_INTRADAY = [S15, M1, M3, M5, M15]
    ALL = [S15, M1, M3, M5, M15, D1]

    SECONDS_MAP = {
        S15: 15,
        M1: 60,
        M3: 180,
        M5: 300,
        M15: 900,
        D1: 86400
    }


class VolumeSemantics:
    """
    Defines the interpretation of the incoming tick volume field:
    - CUMULATIVE: Total volume traded so far in session (standard for broker quotes/LTP packets).
      Requires positive delta calculation (V_curr - V_prev) to avoid multi-million candle volume.
    - INCREMENTAL: Volume traded specifically in this event (standard for trade execution feeds).
    """
    CUMULATIVE = "CUMULATIVE"
    INCREMENTAL = "INCREMENTAL"


def determine_candle_closure(
    start_time: datetime,
    timeframe: str,
    current_exchange_time: Optional[datetime] = None
) -> Tuple[bool, datetime]:
    """
    Determines whether a candle interval has completed (closed) or is currently developing.
    Calculates interval end_time in IST and compares against current exchange time.
    Returns (is_closed: bool, end_time: datetime).
    """
    dt_ist = start_time.astimezone(IST_ZONE) if start_time.tzinfo else start_time.replace(tzinfo=IST_ZONE)
    start_dt, end_dt = get_candle_interval_bounds(dt_ist, timeframe)
    now_ist = current_exchange_time or IndianMarketSessionClock.now_ist()
    if now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=IST_ZONE)

    # If current exchange time has passed the end of the candle interval, it is closed
    if now_ist >= end_dt:
        return True, end_dt

    # If current time is after official session close (15:30 IST) for that trading day
    if now_ist.date() >= dt_ist.date() and now_ist.time() >= time(15, 30, 0):
        return True, end_dt

    # Otherwise the candle is actively developing (interval still ongoing)
    return False, end_dt


class Candle:
    """Represents a single OHLCV candlestick bar."""
    __slots__ = (
        "symbol", "timeframe", "start_time", "end_time",
        "open", "high", "low", "close", "volume",
        "is_closed", "candle_status"
    )

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime,
        end_time: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: int = 0,
        is_closed: bool = False,
        candle_status: str = DataStatus.LIVE
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_time = start_time
        self.end_time = end_time
        self.open = float(open_price)
        self.high = float(high_price)
        self.low = float(low_price)
        self.close = float(close_price)
        self.volume = int(volume)
        self.is_closed = is_closed
        self.candle_status = candle_status

    def update(self, price: float, volume: int = 0) -> None:
        """Updates developing candle with new tick price and volume."""
        p = float(price)
        if p > self.high:
            self.high = p
        if p < self.low:
            self.low = p
        self.close = p
        self.volume += int(volume)

    def to_dict(self) -> Dict[str, Any]:
        """Converts candle to standard dict schema."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.start_time.isoformat(),
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "open": round(self.open, 2),
            "high": round(self.high, 2),
            "low": round(self.low, 2),
            "close": round(self.close, 2),
            "volume": self.volume,
            "is_closed": self.is_closed,
            "candle_status": self.candle_status
        }


def get_candle_interval_bounds(dt: datetime, timeframe: str) -> Tuple[datetime, datetime]:
    """
    Calculates exact start and end datetimes for a candle interval in Asia/Kolkata timezone,
    aligned to NSE session start (09:15:00 IST).
    """
    dt_ist = dt.astimezone(IST_ZONE) if dt.tzinfo else dt.replace(tzinfo=IST_ZONE)
    t = dt_ist.time()
    curr_date = dt_ist.date()

    if timeframe == Timeframe.S15:
        sec = dt_ist.second
        start_sec = (sec // 15) * 15
        start_dt = dt_ist.replace(second=start_sec, microsecond=0)
        end_dt = start_dt + timedelta(seconds=15)
        return start_dt, end_dt

    elif timeframe == Timeframe.M1:
        start_dt = dt_ist.replace(second=0, microsecond=0)
        end_dt = start_dt + timedelta(minutes=1)
        return start_dt, end_dt

    elif timeframe == Timeframe.M3:
        # Aligned to 09:15 IST (bins: 09:15-09:18, 09:18-09:21, ...)
        # Since 15 is divisible by 3, standard minute // 3 * 3 aligns identically
        start_min = (dt_ist.minute // 3) * 3
        start_dt = dt_ist.replace(minute=start_min, second=0, microsecond=0)
        end_dt = start_dt + timedelta(minutes=3)
        return start_dt, end_dt

    elif timeframe == Timeframe.M5:
        # Aligned to 09:15 IST (bins: 09:15-09:20, 09:20-09:25, ...)
        start_min = (dt_ist.minute // 5) * 5
        start_dt = dt_ist.replace(minute=start_min, second=0, microsecond=0)
        end_dt = start_dt + timedelta(minutes=5)
        return start_dt, end_dt

    elif timeframe == Timeframe.M15:
        # Aligned to 09:15 IST (bins: 09:15-09:30, 09:30-09:45, ...)
        start_min = (dt_ist.minute // 15) * 15
        start_dt = dt_ist.replace(minute=start_min, second=0, microsecond=0)
        end_dt = start_dt + timedelta(minutes=15)
        return start_dt, end_dt

    elif timeframe == Timeframe.D1:
        # Full trading day: 09:15 to 15:30 IST
        start_dt = datetime.combine(curr_date, time(9, 15, 0), tzinfo=IST_ZONE)
        end_dt = datetime.combine(curr_date, time(15, 30, 0), tzinfo=IST_ZONE)
        return start_dt, end_dt

    # Default fallback: 1 minute
    start_dt = dt_ist.replace(second=0, microsecond=0)
    return start_dt, start_dt + timedelta(minutes=1)


def build_15s_candles_from_ticks(
    ticks: List[Dict[str, Any]],
    symbol: str = "NIFTY 50",
    volume_semantics: str = VolumeSemantics.INCREMENTAL
) -> List[Dict[str, Any]]:
    """
    Builds 15-second OHLCV candles from timestamped live ticks.
    Guarantees:
    - Angel One getCandleData does NOT support 15s; this function is the official builder from live ticks.
    - Boundaries align to :00, :15, :30, :45 of each minute.
    - Supports both INCREMENTAL and CUMULATIVE volume semantics.
    - Mathematical preservation: Open=first, High=max, Low=min, Close=last, Volume=sum.
    """
    if not ticks:
        return []

    parsed_ticks = []
    for t in ticks:
        ts = t.get("timestamp")
        if isinstance(ts, str):
            try:
                if "T" in ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
        elif isinstance(ts, datetime):
            dt = ts
        else:
            continue

        dt_ist = dt.astimezone(IST_ZONE) if dt.tzinfo else dt.replace(tzinfo=IST_ZONE)
        price = float(t.get("price", 0.0))
        vol = int(t.get("volume", 0) or 0)
        parsed_ticks.append((dt_ist, price, vol, t))

    # Sort strictly by timestamp
    parsed_ticks.sort(key=lambda x: x[0])

    # Compute per-tick volume contributions based on semantics
    processed_ticks: List[Tuple[datetime, float, int]] = []
    if volume_semantics == VolumeSemantics.CUMULATIVE:
        last_cum_vol = None
        for dt_ist, price, raw_vol, _ in parsed_ticks:
            if last_cum_vol is None:
                delta_v = 0
                last_cum_vol = raw_vol
            elif raw_vol < last_cum_vol:
                # Volume counter reset / reconnect
                delta_v = 0
                last_cum_vol = raw_vol
            elif raw_vol == last_cum_vol:
                # Duplicate or no volume change
                delta_v = 0
            else:
                delta_v = raw_vol - last_cum_vol
                last_cum_vol = raw_vol
            processed_ticks.append((dt_ist, price, delta_v))
    else:
        processed_ticks = [(dt_ist, price, max(0, raw_vol)) for dt_ist, price, raw_vol, _ in parsed_ticks]

    # Group into 15s intervals
    buckets: Dict[datetime, List[Tuple[float, int]]] = {}
    for dt_ist, price, v in processed_ticks:
        start_dt, end_dt = get_candle_interval_bounds(dt_ist, Timeframe.S15)
        if start_dt not in buckets:
            buckets[start_dt] = []
        buckets[start_dt].append((price, v))

    candles_15s: List[Dict[str, Any]] = []
    for start_dt in sorted(buckets.keys()):
        group = buckets[start_dt]
        end_dt = start_dt + timedelta(seconds=15)
        prices = [item[0] for item in group]
        vols = [item[1] for item in group]
        candles_15s.append({
            "symbol": symbol,
            "timeframe": Timeframe.S15,
            "timestamp": start_dt.isoformat(),
            "start_time": start_dt.isoformat(),
            "end_time": end_dt.isoformat(),
            "open": round(prices[0], 2),
            "high": round(max(prices), 2),
            "low": round(min(prices), 2),
            "close": round(prices[-1], 2),
            "volume": sum(vols),
            "is_closed": True,
            "candle_status": DataStatus.LIVE
        })

    return candles_15s


def resample_candles(
    source_candles: List[Dict[str, Any]],
    target_timeframe: str,
    symbol: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Pure-function resampling of OHLCV candles to target_timeframe.
    Groups candles by their target interval boundaries and aggregates:
    Open = first, High = max, Low = min, Close = last, Volume = sum.
    Guarantees strict non-repainting mathematical correctness.
    """
    if not source_candles:
        return []

    # Map target timeframe interval seconds
    resampled: List[Dict[str, Any]] = []
    current_bucket: List[Dict[str, Any]] = []
    current_interval_start: Optional[datetime] = None
    current_interval_end: Optional[datetime] = None

    for c in source_candles:
        ts_str = str(c.get("timestamp") or c.get("start_time"))
        try:
            if "T" in ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
        except Exception:
            continue

        start_dt, end_dt = get_candle_interval_bounds(dt, target_timeframe)

        if current_interval_start is None:
            current_interval_start = start_dt
            current_interval_end = end_dt
            current_bucket = [c]
        elif start_dt == current_interval_start:
            current_bucket.append(c)
        else:
            # Finalize previous bucket
            agg_candle = _aggregate_bucket(current_bucket, current_interval_start, current_interval_end, target_timeframe, symbol)
            if agg_candle:
                resampled.append(agg_candle)
            current_interval_start = start_dt
            current_interval_end = end_dt
            current_bucket = [c]

    # Finalize remaining bucket
    if current_bucket and current_interval_start and current_interval_end:
        agg_candle = _aggregate_bucket(current_bucket, current_interval_start, current_interval_end, target_timeframe, symbol)
        if agg_candle:
            resampled.append(agg_candle)

    return resampled


def _aggregate_bucket(
    bucket: List[Dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
    timeframe: str,
    symbol: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Aggregates a list of candles in a time bucket into one composite candle."""
    if not bucket:
        return None

    sym = symbol or bucket[0].get("symbol", "")
    open_val = float(bucket[0]["open"])
    high_val = max(float(b["high"]) for b in bucket)
    low_val = min(float(b["low"]) for b in bucket)
    close_val = float(bucket[-1]["close"])
    vol_val = sum(int(b.get("volume", 0)) for b in bucket)
    status_val = bucket[-1].get("candle_status", DataStatus.HISTORICAL)
    is_closed_val = all(b.get("is_closed", True) for b in bucket)

    return {
        "symbol": sym,
        "timeframe": timeframe,
        "timestamp": start_dt.isoformat(),
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "open": round(open_val, 2),
        "high": round(high_val, 2),
        "low": round(low_val, 2),
        "close": round(close_val, 2),
        "volume": vol_val,
        "is_closed": is_closed_val,
        "candle_status": status_val
    }


class MultiTimeframeCandleStore:
    """
    In-memory, multi-timeframe candle store with strict completed vs active candle segregation.
    Maintains synchronized streams across 15s, 1m, 3m, 5m, and 15m.
    Supports both CUMULATIVE session volume (with positive deltas, reset detection, and out-of-order rejection)
    and INCREMENTAL tick volume.
    Guarantees strict deduplication and chronological ordering.
    """

    DEFAULT_TIMEFRAME_CAPACITIES = {
        Timeframe.S15: 1500,  # Full 6h15m NSE regular session (1,500 15s candles)
        Timeframe.M1: 500,    # Full 375 1m regular session + pre-open/buffer
        Timeframe.M3: 200,    # Full 125 3m regular session
        Timeframe.M5: 150,    # Full 75 5m regular session
        Timeframe.M15: 50     # Full 25 15m regular session
    }

    def __init__(
        self,
        clock: Optional[IndianMarketSessionClock] = None,
        max_closed_candles: int = 500,
        volume_semantics: str = VolumeSemantics.INCREMENTAL
    ):
        self.clock = clock or IndianMarketSessionClock()
        self.max_closed_candles = max_closed_candles
        self.volume_semantics = volume_semantics
        # Timeframe capacities: ensure 15s can retain full 1,500 session bars
        self.capacities = dict(self.DEFAULT_TIMEFRAME_CAPACITIES)
        if max_closed_candles != 500:
            for tf in self.capacities:
                self.capacities[tf] = max_closed_candles
        # {symbol: {timeframe: [Candle, ...]}}
        self._closed_candles: Dict[str, Dict[str, List[Candle]]] = {}
        # {symbol: {timeframe: Candle}}
        self._active_candle: Dict[str, Dict[str, Optional[Candle]]] = {}
        # Last tick timestamps {symbol: datetime}
        self._last_tick_time: Dict[str, datetime] = {}
        # Last seen cumulative session volume {symbol: int}
        self._last_seen_volume: Dict[str, Optional[int]] = {}

    def _ensure_symbol_initialized(self, symbol: str) -> None:
        sym = symbol.strip().upper()
        if sym not in self._closed_candles:
            self._closed_candles[sym] = {tf: [] for tf in Timeframe.ALL}
        if sym not in self._active_candle:
            self._active_candle[sym] = {tf: None for tf in Timeframe.ALL}
        if sym not in self._last_seen_volume:
            self._last_seen_volume[sym] = None

    def on_tick(
        self,
        symbol: str,
        price: float,
        volume: int = 0,
        timestamp: Optional[datetime] = None,
        status: str = DataStatus.LIVE,
        volume_semantics: Optional[str] = None
    ) -> None:
        """
        Processes an incoming market tick across all intraday timeframes.
        Closes completed candles and updates developing active candles without repainting.
        Handles volume semantics:
        - If CUMULATIVE: derives positive deltas (V_t - V_{t-1}), detects volume resets and duplicate ticks.
        - If INCREMENTAL: sums per-tick trade quantities.
        """
        sym = symbol.strip().upper()
        self._ensure_symbol_initialized(sym)
        tick_time = self.clock.to_ist(timestamp or self.clock.now_ist())
        p = float(price)
        raw_vol = int(volume)
        sem = volume_semantics or self.volume_semantics

        # Check for daily session reset (e.g. crossing 09:15:00 IST)
        last_t = self._last_tick_time.get(sym)
        if last_t and self.clock.is_new_session(last_t, tick_time):
            self.reset_session(sym)
            last_t = None

        # Calculate volume delta according to semantics
        delta_v = 0
        if sem == VolumeSemantics.INCREMENTAL:
            delta_v = max(0, raw_vol)
        else:
            # VolumeSemantics.CUMULATIVE
            last_vol = self._last_seen_volume.get(sym)
            if last_t is not None and tick_time < last_t:
                # Out-of-order event: timestamp is older than latest processed tick
                # Discard volume delta and avoid state corruption
                delta_v = 0
            elif last_vol is None:
                # First tick for symbol / after session reset:
                # Establish cumulative volume baseline without inflating candle volume
                self._last_seen_volume[sym] = raw_vol
                delta_v = 0
            elif raw_vol < last_vol:
                # Cumulative volume counter reset / broker reconnect
                # Re-establish baseline
                self._last_seen_volume[sym] = raw_vol
                delta_v = 0
            elif raw_vol == last_vol:
                # Duplicate tick or quote price update without trade
                delta_v = 0
            else:
                # Valid monotonic cumulative volume advancement
                delta_v = raw_vol - last_vol
                self._last_seen_volume[sym] = raw_vol

        # Update last tick time if this tick is chronologically newer or equal
        if last_t is None or tick_time >= last_t:
            self._last_tick_time[sym] = tick_time

        for tf in Timeframe.ALL_INTRADAY:
            start_dt, end_dt = get_candle_interval_bounds(tick_time, tf)
            active = self._active_candle[sym][tf]

            if active is None:
                # Start new active candle
                self._active_candle[sym][tf] = Candle(
                    symbol=sym,
                    timeframe=tf,
                    start_time=start_dt,
                    end_time=end_dt,
                    open_price=p,
                    high_price=p,
                    low_price=p,
                    close_price=p,
                    volume=delta_v,
                    is_closed=False,
                    candle_status=status
                )
            elif tick_time >= active.end_time or start_dt > active.start_time:
                # Active candle interval has completed! Close it and freeze it.
                active.is_closed = True

                # Deduplication: check if candle with same start_time already exists
                closed_list = self._closed_candles[sym][tf]
                if closed_list and closed_list[-1].start_time == active.start_time:
                    closed_list[-1] = active
                else:
                    closed_list.append(active)
                    cap = self.capacities.get(tf, self.max_closed_candles)
                    if len(closed_list) > cap:
                        closed_list.pop(0)

                # Initialize new active candle for current interval
                self._active_candle[sym][tf] = Candle(
                    symbol=sym,
                    timeframe=tf,
                    start_time=start_dt,
                    end_time=end_dt,
                    open_price=p,
                    high_price=p,
                    low_price=p,
                    close_price=p,
                    volume=delta_v,
                    is_closed=False,
                    candle_status=status
                )
            else:
                # Update existing forming candle
                active.update(p, delta_v)

    def ingest_base_candles(
        self,
        symbol: str,
        candles: List[Dict[str, Any]],
        base_timeframe: str = Timeframe.M1
    ) -> None:
        """
        Ingests historical base candles and populates higher timeframes via resampling.
        Strictly segregates completed (is_closed=True) from active forming candles (is_closed=False).
        Prevents duplicate timestamps and maintains chronological ordering.
        """
        sym = symbol.strip().upper()
        self._ensure_symbol_initialized(sym)

        closed_new: List[Candle] = []
        active_new: Optional[Candle] = None

        for c in candles:
            ts_str = str(c.get("timestamp") or c.get("start_time"))
            try:
                if "T" in ts_str:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
            except Exception:
                continue

            dt_ist = self.clock.to_ist(dt)
            start_dt, end_dt = get_candle_interval_bounds(dt_ist, base_timeframe)
            is_closed = bool(c.get("is_closed", True))
            status = c.get("candle_status", DataStatus.HISTORICAL if is_closed else DataStatus.LIVE)

            candle_obj = Candle(
                symbol=sym,
                timeframe=base_timeframe,
                start_time=start_dt,
                end_time=end_dt,
                open_price=c["open"],
                high_price=c["high"],
                low_price=c["low"],
                close_price=c["close"],
                volume=c.get("volume", 0),
                is_closed=is_closed,
                candle_status=status
            )

            if is_closed:
                closed_new.append(candle_obj)
            else:
                active_new = candle_obj

        # Deduplication and chronological sorting: merge into existing closed candles keyed by start_time
        existing_map = {c.start_time: c for c in self._closed_candles[sym][base_timeframe]}
        for c in closed_new:
            existing_map[c.start_time] = c

        sorted_closed = [existing_map[k] for k in sorted(existing_map.keys())]
        cap_base = self.capacities.get(base_timeframe, self.max_closed_candles)
        self._closed_candles[sym][base_timeframe] = sorted_closed[-cap_base:]

        # If an active forming candle was present, preserve it as active candle
        if active_new:
            self._active_candle[sym][base_timeframe] = active_new

        # Resample closed base candles to higher timeframes
        target_tfs = [tf for tf in Timeframe.ALL_INTRADAY if Timeframe.SECONDS_MAP[tf] > Timeframe.SECONDS_MAP[base_timeframe]]
        base_dicts = [b.to_dict() for b in self._closed_candles[sym][base_timeframe]]

        for target_tf in target_tfs:
            resampled = resample_candles(base_dicts, target_tf, symbol=sym)
            resampled_map = {c.start_time: c for c in self._closed_candles[sym][target_tf]}
            for r in resampled:
                s_dt = datetime.fromisoformat(r["start_time"])
                e_dt = datetime.fromisoformat(r["end_time"])
                resampled_map[s_dt] = Candle(
                    symbol=sym,
                    timeframe=target_tf,
                    start_time=s_dt,
                    end_time=e_dt,
                    open_price=r["open"],
                    high_price=r["high"],
                    low_price=r["low"],
                    close_price=r["close"],
                    volume=r["volume"],
                    is_closed=True,
                    candle_status=r["candle_status"]
                )
            sorted_resampled = [resampled_map[k] for k in sorted(resampled_map.keys())]
            cap_target = self.capacities.get(target_tf, self.max_closed_candles)
            self._closed_candles[sym][target_tf] = sorted_resampled[-cap_target:]

    def get_closed_candles(self, symbol: str, timeframe: str, limit: Optional[int] = 100) -> List[Dict[str, Any]]:
        """
        Returns ONLY closed, finalized candles.
        Guarantees strictly non-repainting indicator inputs.
        If limit is None or <= 0, returns all available closed candles.
        """
        sym = symbol.strip().upper()
        self._ensure_symbol_initialized(sym)
        closed_list = self._closed_candles[sym].get(timeframe, [])
        if limit is not None and limit > 0:
            return [c.to_dict() for c in closed_list[-limit:]]
        return [c.to_dict() for c in closed_list]

    def get_session_candles(
        self,
        symbol: str,
        timeframe: str,
        session_date: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """
        Returns closed candles strictly belonging to the designated trading session date.
        Discards previous trading day bars to guarantee pristine intraday session calculations.
        """
        sym = symbol.strip().upper()
        self._ensure_symbol_initialized(sym)
        closed_list = self._closed_candles[sym].get(timeframe, [])
        if not closed_list:
            return []
        target_date = session_date or closed_list[-1].start_time.date()
        return [c.to_dict() for c in closed_list if c.start_time.date() == target_date]

    def get_active_candle(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        """Returns the single active unclosed candle, or None if market closed."""
        sym = symbol.strip().upper()
        self._ensure_symbol_initialized(sym)
        active = self._active_candle[sym].get(timeframe)
        return active.to_dict() if active else None

    def get_all_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
        include_active: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Returns closed candles, optionally appending the active candle at [-1].
        """
        candles = self.get_closed_candles(symbol, timeframe, limit=limit)
        if include_active:
            active = self.get_active_candle(symbol, timeframe)
            if active:
                candles.append(active)
        return candles

    def reset_session(self, symbol: Optional[str] = None) -> None:
        """Clears active intraday forming candles and resets volume baseline on session transition."""
        symbols = [symbol.strip().upper()] if symbol else list(self._active_candle.keys())
        for sym in symbols:
            if sym in self._active_candle:
                for tf in Timeframe.ALL:
                    self._active_candle[sym][tf] = None
            self._last_seen_volume[sym] = None
            if sym in self._last_tick_time:
                del self._last_tick_time[sym]
