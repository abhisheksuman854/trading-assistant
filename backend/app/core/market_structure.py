"""
TradeLens AI - Market Structure, Regime & Divergence Engine (Phase 2B)
Deterministic, non-repainting algorithmic market microstructure analysis.

Core Components:
1. Fractal Swing Detection (K-bar lookback/lookforward peaks and troughs).
2. Market Structure Classification: Higher High (HH), Higher Low (HL),
   Lower High (LH), Lower Low (LL), Equal High/Low (EQH/EQL).
   Macro Structure: BULLISH, BEARISH, SIDEWAYS, TRANSITIONAL, INSUFFICIENT_DATA.
3. BOS & CHoCH Break Detection: Bullish/Bearish Break of Structure (trend continuation)
   and Change of Character (trend reversal) with strict closed-candle break confirmation
   and wick-only false break (liquidity sweep) identification.
4. Market Regime Engine: Evaluates structure, EMA alignment (9/15/50), ADX(14) trend strength,
   Bollinger Bandwidth, and Carter's Squeeze to classify one of 7 mutually exclusive regimes:
   TRENDING_BULLISH, TRENDING_BEARISH, RANGING, VOLATILITY_COMPRESSION, VOLATILITY_EXPANSION,
   TRANSITIONAL, INSUFFICIENT_DATA.
5. Divergence Engine: Regular Bullish/Bearish (Reversals) and Hidden Bullish/Bearish
   (Continuations) against Wilder RSI(14) and MACD Histogram (12, 26, 9) with exact
   pivot-pairing, closed-candle confirmation, and explicit invalidation thresholds.
6. Multi-Timeframe Analysis: Independent multi-timeframe evaluation across 15s, 1m, 3m, 5m, 15m
   with alignment and conflict classification without lower-timeframe overwrite bias.
"""
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Any, Optional, Tuple, Union
import math

from app.data.base import DataStatus
from app.data.candle_store import MultiTimeframeCandleStore, Timeframe
from app.core.indicators import (
    TechnicalIndicatorService,
    calculate_rsi,
    calculate_macd,
    calculate_macd_series,
    calculate_adx,
    calculate_bollinger_bands,
    calculate_keltner_channels,
    detect_volatility_squeeze,
    calculate_atr,
    calculate_session_vwap,
    CalculationStatus,
    WarmupStatus
)


# ============================================================================
# Enums and Type Definitions
# ============================================================================

class SwingType:
    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"


class ConfirmationStatus:
    CONFIRMED = "CONFIRMED"
    DEVELOPING = "DEVELOPING"


class StructureType:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"
    TRANSITIONAL = "TRANSITIONAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class StructuralBreakType:
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"
    FALSE_BREAK_HIGH = "FALSE_BREAK_HIGH"
    FALSE_BREAK_LOW = "FALSE_BREAK_LOW"
    NONE = "NONE"


class MarketRegime:
    TRENDING_BULLISH = "TRENDING_BULLISH"
    TRENDING_BEARISH = "TRENDING_BEARISH"
    RANGING = "RANGING"
    VOLATILITY_COMPRESSION = "VOLATILITY_COMPRESSION"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    TRANSITIONAL = "TRANSITIONAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DivergenceType:
    REGULAR_BULLISH = "REGULAR_BULLISH"      # Reversal: Price Lower Low, Indicator Higher Low
    REGULAR_BEARISH = "REGULAR_BEARISH"      # Reversal: Price Higher High, Indicator Lower High
    HIDDEN_BULLISH = "HIDDEN_BULLISH"        # Continuation: Price Higher Low, Indicator Lower Low
    HIDDEN_BEARISH = "HIDDEN_BEARISH"        # Continuation: Price Lower High, Indicator Higher High
    NO_DIVERGENCE = "NO_DIVERGENCE"


class MTFAlignmentStatus:
    ALL_ALIGNED_BULLISH = "ALL_ALIGNED_BULLISH"
    ALL_ALIGNED_BEARISH = "ALL_ALIGNED_BEARISH"
    ALIGNED_RANGING = "ALIGNED_RANGING"
    CONFLICTING_MTF_REGIMES = "CONFLICTING_MTF_REGIMES"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class BreakLifecycleState:
    NEW_CONFIRMED_BREAK = "NEW_CONFIRMED_BREAK"
    CONSUMED = "CONSUMED"
    CONTINUATION = "CONTINUATION"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED_BY_NEW_PIVOT = "SUPERSEDED_BY_NEW_PIVOT"
    UNBROKEN_SWEEP = "UNBROKEN_SWEEP"
    UNBROKEN = "UNBROKEN"
    DEVELOPING_BREAK = "DEVELOPING_BREAK"
    FEED_GAP_SUPPRESSED = "FEED_GAP_SUPPRESSED"


class DivergenceInvalidationStatus:
    ACTIVE_UNTOUCHED = "ACTIVE_UNTOUCHED"
    ACTIVE_WICK_PROBED = "ACTIVE_WICK_PROBED"
    INVALIDATED_BY_CLOSE = "INVALIDATED_BY_CLOSE"
    FEED_GAP_SUPPRESSED = "FEED_GAP_SUPPRESSED"


# Canonical Timeframe K-Parameter Mapping
DEFAULT_TIMEFRAME_K: Dict[str, int] = {
    Timeframe.S15: 2,
    Timeframe.M1: 2,
    Timeframe.M3: 2,
    Timeframe.M5: 3,
    Timeframe.M15: 3
}


# ============================================================================
# 1. Fractal Swing Detection
# ============================================================================

def identify_fractal_pivots(
    candles: List[Dict[str, Any]],
    k: int = 2,
    symbol: str = "UNKNOWN",
    timeframe: str = "1m",
    tolerance_pct: float = 0.0002
) -> Dict[str, Any]:
    """
    Symmetrical K-bar Fractal Swing Detection with Non-Repainting Guarantees.

    Mathematical Conditions:
    - Candidate Swing High at bar i:
        High[i] > High[i - j] for all j in 1..K (strictly higher than left neighbours)
        High[i] >= High[i + j] for all j in 1..K (greater than or equal to right neighbours)
    - Candidate Swing Low at bar i:
        Low[i] < Low[i - j] for all j in 1..K (strictly lower than left neighbours)
        Low[i] <= Low[i + j] for all j in 1..K (lower than or equal to right neighbours)

    Deterministic Edge-Case Rules:
    1. Right-side Confirmation: A pivot at bar i is ONLY confirmed if bar i + K has closed (is_closed=True).
       If bar i + K is actively forming or has not occurred, the pivot is marked as DEVELOPING.
    2. Inside Bars: Inside bars (High <= High[i-1] and Low >= Low[i-1]) cannot form new swing extrema.
    3. Outside Bars: Outside bars (High > High[i-1] and Low < Low[i-1]) are evaluated independently;
       if an outside bar satisfies both high and low conditions, both pivots are captured at their exact prices.
    4. Equal Highs / Lows: Consecutive bars with price differences within tolerance_pct are flagged as EQH / EQL.
       The first peak in the equal cluster is preserved to prevent duplicate consecutive swings.
    5. Consecutive Same-Type Swings: If two consecutive swing highs occur without an intervening swing low,
       the higher high is preserved as the dominant structural pivot (and lower low for swing lows).
    6. Session Discontinuity: If timestamp gap between consecutive bars exceeds 2x the timeframe interval,
       a data quality flag (FEED_GAP) is attached.
    """
    n = len(candles)
    min_required = 2 * k + 1
    if n < min_required or k <= 0:
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "k_parameter": k,
            "candle_count": n,
            "swing_highs": [],
            "swing_lows": [],
            "all_pivots": [],
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    opens = [float(c["open"]) for c in candles]

    # Pre-calculate interval continuity and data quality across the candle series
    expected_sec = Timeframe.SECONDS_MAP.get(timeframe, 60)
    gap_threshold_sec = expected_sec * 1.5  # Reconciled rule: 1.5x expected interval
    bar_quality: List[str] = ["VALID"] * n

    for j in range(1, n):
        c_curr_ts = candles[j].get("timestamp") or candles[j].get("start_time")
        c_prev_ts = candles[j - 1].get("timestamp") or candles[j - 1].get("start_time")
        if c_curr_ts and c_prev_ts:
            try:
                dt_curr = datetime.fromisoformat(str(c_curr_ts).replace("Z", "+00:00"))
                dt_prev = datetime.fromisoformat(str(c_prev_ts).replace("Z", "+00:00"))
                diff_sec = (dt_curr - dt_prev).total_seconds()
                if dt_curr.date() != dt_prev.date():
                    # Inter-session / overnight transition is a legitimate boundary, not an intraday feed drop
                    bar_quality[j] = "SESSION_BOUNDARY"
                elif diff_sec > gap_threshold_sec:
                    # Intraday missing feed interval
                    bar_quality[j] = "FEED_GAP"
                else:
                    bar_quality[j] = "VALID"
            except Exception:
                bar_quality[j] = "VALID"

    raw_highs: List[Dict[str, Any]] = []
    raw_lows: List[Dict[str, Any]] = []

    for i in range(k, n):
        # Determine how many right bars exist
        right_bars_count = (n - 1) - i
        has_full_right_window = right_bars_count >= k

        # Check left condition (must be strictly satisfied)
        is_left_high = all(highs[i] > highs[i - j] for j in range(1, k + 1))
        is_left_low = all(lows[i] < lows[i - j] for j in range(1, k + 1))

        if not is_left_high and not is_left_low:
            continue

        # Check right condition
        # If full right window exists: evaluate all k bars
        # If right window is incomplete (< k): evaluate available right bars for DEVELOPING status
        eval_right_k = min(k, right_bars_count)
        is_right_high = True
        is_right_low = True

        for j in range(1, eval_right_k + 1):
            if highs[i] < highs[i + j]:
                is_right_high = False
            if lows[i] > lows[i + j]:
                is_right_low = False

        # Confirmation requires the full right window (i + k) to be present AND closed
        if has_full_right_window:
            confirming_bar = candles[i + k]
            is_confirmed = bool(confirming_bar.get("is_closed", True))
            if is_confirmed:
                confirm_ts = confirming_bar.get("timestamp") or confirming_bar.get("start_time")
                confirm_idx = i + k
            else:
                confirm_ts = None
                confirm_idx = None
        else:
            is_confirmed = False
            confirm_ts = None
            confirm_idx = None

        pivot_ts = candles[i].get("timestamp") or candles[i].get("start_time")
        data_status = candles[i].get("candle_status", DataStatus.HISTORICAL if is_confirmed else DataStatus.LIVE)

        # Check data quality across the pivot's k-bar neighbourhood [i - k, i + k]
        w_start = max(0, i - k)
        w_end = min(n, i + k + 1)
        window_qualities = bar_quality[w_start:w_end]

        if "FEED_GAP" in window_qualities:
            quality_status = "FEED_GAP"
        elif "SESSION_BOUNDARY" in window_qualities:
            quality_status = "SESSION_BOUNDARY"
        else:
            quality_status = "VALID"

        if is_left_high and is_right_high:
            pivot_h = {
                "symbol": symbol.strip().upper(),
                "timeframe": timeframe,
                "index": i,
                "pivot_timestamp": pivot_ts,
                "confirmation_timestamp": confirm_ts,
                "pivot_price": round(highs[i], 2),
                "swing_type": SwingType.SWING_HIGH,
                "k_parameter": k,
                "confirmation_status": ConfirmationStatus.CONFIRMED if is_confirmed else ConfirmationStatus.DEVELOPING,
                "confirmed_at_index": confirm_idx,
                "data_quality_status": quality_status
            }
            raw_highs.append(pivot_h)

        if is_left_low and is_right_low:
            pivot_l = {
                "symbol": symbol.strip().upper(),
                "timeframe": timeframe,
                "index": i,
                "pivot_timestamp": pivot_ts,
                "confirmation_timestamp": confirm_ts,
                "pivot_price": round(lows[i], 2),
                "swing_type": SwingType.SWING_LOW,
                "k_parameter": k,
                "confirmation_status": ConfirmationStatus.CONFIRMED if is_confirmed else ConfirmationStatus.DEVELOPING,
                "confirmed_at_index": confirm_idx,
                "data_quality_status": quality_status
            }
            raw_lows.append(pivot_l)

    # Deterministic Deduplication: Handle consecutive swings of the same type
    filtered_highs = _filter_consecutive_swings(raw_highs, is_high=True, tolerance_pct=tolerance_pct)
    filtered_lows = _filter_consecutive_swings(raw_lows, is_high=False, tolerance_pct=tolerance_pct)

    # Merge into chronological all_pivots list
    all_pivots = filtered_highs + filtered_lows
    all_pivots.sort(key=lambda p: p["index"])

    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe,
        "k_parameter": k,
        "candle_count": n,
        "swing_highs": filtered_highs,
        "swing_lows": filtered_lows,
        "all_pivots": all_pivots,
        "calculation_status": CalculationStatus.VALID
    }


def _filter_consecutive_swings(
    pivots: List[Dict[str, Any]],
    is_high: bool,
    tolerance_pct: float
) -> List[Dict[str, Any]]:
    """
    Deduplicates consecutive swing candidates occurring in close proximity.
    If two swing highs occur consecutively: preserves the dominant (highest) peak.
    If two swing lows occur consecutively: preserves the dominant (lowest) trough.
    If prices are within tolerance_pct, tags them as EQH/EQL.
    """
    if not pivots:
        return []

    filtered: List[Dict[str, Any]] = [pivots[0]]

    for p in pivots[1:]:
        prev = filtered[-1]
        p_price = p["pivot_price"]
        prev_price = prev["pivot_price"]
        tol = prev_price * tolerance_pct

        # Check for equal extrema
        if abs(p_price - prev_price) <= tol:
            p["is_equal_extrema"] = True
            prev["is_equal_extrema"] = True

        # If indices are close (e.g. within 2*k bars) and same direction: pick the dominant one
        if (p["index"] - prev["index"]) <= (p["k_parameter"] * 2):
            if is_high:
                if p_price > prev_price:
                    filtered[-1] = p  # replace with higher high
            else:
                if p_price < prev_price:
                    filtered[-1] = p  # replace with lower low
        else:
            filtered.append(p)

    return filtered


# ============================================================================
# 2. Market Structure Classification (HH, HL, LH, LL, Structure State)
# ============================================================================

def classify_market_structure(
    candles: List[Dict[str, Any]],
    k: int = 2,
    symbol: str = "UNKNOWN",
    timeframe: str = "1m",
    tolerance_pct: float = 0.0005
) -> Dict[str, Any]:
    """
    Classifies Market Structure using confirmed fractal swing points.

    Classification Schema:
    - HH (Higher High): High[t] > High[t-1] + tolerance
    - LH (Lower High): High[t] < High[t-1] - tolerance
    - EQH (Equal High): |High[t] - High[t-1]| <= tolerance
    - HL (Higher Low): Low[t] > Low[t-1] + tolerance
    - LL (Lower Low): Low[t] < Low[t-1] - tolerance
    - EQL (Equal Low): |Low[t] - Low[t-1]| <= tolerance

    Structure States:
    - BULLISH: Confirmed sequence of HH and HL.
    - BEARISH: Confirmed sequence of LH and LL.
    - SIDEWAYS: Equal highs/lows or range-bound without directional progression.
    - TRANSITIONAL: Mixed structure (e.g. HH followed by LL, or LH followed by HL).
    - INSUFFICIENT_DATA: Fewer than 2 confirmed swing highs or 2 confirmed swing lows.

    Strict Rule: Never classifies a trend from a single isolated pivot.
    """
    pivots_data = identify_fractal_pivots(candles, k=k, symbol=symbol, timeframe=timeframe)
    confirmed_highs = [p for p in pivots_data["swing_highs"] if p["confirmation_status"] == ConfirmationStatus.CONFIRMED]
    confirmed_lows = [p for p in pivots_data["swing_lows"] if p["confirmation_status"] == ConfirmationStatus.CONFIRMED]

    if len(confirmed_highs) < 2 or len(confirmed_lows) < 2:
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "structure_state": StructureType.INSUFFICIENT_DATA,
            "last_high_class": None,
            "last_low_class": None,
            "confirmed_highs_count": len(confirmed_highs),
            "confirmed_lows_count": len(confirmed_lows),
            "latest_confirmed_high": confirmed_highs[-1] if confirmed_highs else None,
            "previous_confirmed_high": confirmed_highs[-2] if len(confirmed_highs) >= 2 else None,
            "latest_confirmed_low": confirmed_lows[-1] if confirmed_lows else None,
            "previous_confirmed_low": confirmed_lows[-2] if len(confirmed_lows) >= 2 else None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    # Evaluate Highs
    h_curr = confirmed_highs[-1]["pivot_price"]
    h_prev = confirmed_highs[-2]["pivot_price"]
    h_tol = h_prev * tolerance_pct
    if h_curr > h_prev + h_tol:
        high_class = "HH"
    elif h_curr < h_prev - h_tol:
        high_class = "LH"
    else:
        high_class = "EQH"

    # Evaluate Lows
    l_curr = confirmed_lows[-1]["pivot_price"]
    l_prev = confirmed_lows[-2]["pivot_price"]
    l_tol = l_prev * tolerance_pct
    if l_curr > l_prev + l_tol:
        low_class = "HL"
    elif l_curr < l_prev - l_tol:
        low_class = "LL"
    else:
        low_class = "EQL"

    # Structure Determination
    if high_class == "HH" and low_class == "HL":
        structure_state = StructureType.BULLISH
    elif high_class == "LH" and low_class == "LL":
        structure_state = StructureType.BEARISH
    elif high_class == "EQH" or low_class == "EQL":
        structure_state = StructureType.SIDEWAYS
    else:
        # Mixed: HH with LL, or LH with HL
        structure_state = StructureType.TRANSITIONAL

    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe,
        "structure_state": structure_state,
        "last_high_class": high_class,
        "last_low_class": low_class,
        "confirmed_highs_count": len(confirmed_highs),
        "confirmed_lows_count": len(confirmed_lows),
        "latest_confirmed_high": confirmed_highs[-1],
        "previous_confirmed_high": confirmed_highs[-2],
        "latest_confirmed_low": confirmed_lows[-1],
        "previous_confirmed_low": confirmed_lows[-2],
        "calculation_status": CalculationStatus.VALID
    }


# ============================================================================
# 3. BOS and CHoCH Detection
# ============================================================================

def detect_structural_breaks(
    candles: List[Dict[str, Any]],
    k: int = 2,
    symbol: str = "UNKNOWN",
    timeframe: str = "1m",
    tolerance_pct: float = 0.0005
) -> Dict[str, Any]:
    """
    Detects Break of Structure (BOS) and Change of Character (CHoCH).

    Definitions:
    1. BOS_BULLISH: In a confirmed BULLISH trend, a closed candle closes strictly above
       the prior confirmed Swing High (trend continuation).
    2. BOS_BEARISH: In a confirmed BEARISH trend, a closed candle closes strictly below
       the prior confirmed Swing Low (trend continuation).
    3. CHOCH_BULLISH: In a confirmed BEARISH trend, a closed candle closes strictly above
       the prior confirmed Lower High (first indication of trend reversal).
    4. CHOCH_BEARISH: In a confirmed BULLISH trend, a closed candle closes strictly below
       the prior confirmed Higher Low (first indication of trend reversal).

    Execution Precision:
    - Candle Close Break: Strictly requires candle close beyond the level.
    - False Break (Liquidity Sweep): If candle wick pierces the level but close remains inside,
      emits FALSE_BREAK_HIGH or FALSE_BREAK_LOW.
    - Invalidation:
      * Bullish break invalidation level is the preceding swing low.
      * Bearish break invalidation level is the preceding swing high.
    - Deduplication: Consecutive closes beyond an already-broken level are suppressed.
    """
    struct = classify_market_structure(candles, k=k, symbol=symbol, timeframe=timeframe, tolerance_pct=tolerance_pct)
    if struct["structure_state"] == StructureType.INSUFFICIENT_DATA or not candles:
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "event_type": StructuralBreakType.NONE,
            "lifecycle_state": BreakLifecycleState.UNBROKEN,
            "broken_level_price": None,
            "broken_level_timestamp": None,
            "break_candle_timestamp": None,
            "break_candle_close": None,
            "previous_structure": struct["structure_state"],
            "resulting_structure": struct["structure_state"],
            "invalidation_level": None,
            "confirmation_status": ConfirmationStatus.CONFIRMED,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA,
            "data_quality_status": "VALID",
            "explanation": "Insufficient confirmed pivots to evaluate structural breaks."
        }

    latest_candle = candles[-1]
    n = len(candles)
    is_closed = bool(latest_candle.get("is_closed", True))
    curr_close = float(latest_candle["close"])
    curr_high = float(latest_candle["high"])
    curr_low = float(latest_candle["low"])
    curr_ts = latest_candle.get("timestamp") or latest_candle.get("start_time")

    last_high_pivot = struct["latest_confirmed_high"]
    last_low_pivot = struct["latest_confirmed_low"]
    current_state = struct["structure_state"]

    event_type = StructuralBreakType.NONE
    lifecycle_state = BreakLifecycleState.UNBROKEN
    broken_price = None
    broken_ts = None
    invalidation_level = None
    resulting_structure = current_state
    quality_status = "VALID"
    explanation = "No structural break detected on the latest candle."

    # 1. Evaluate Bullish Breakouts
    if last_high_pivot:
        target_high = float(last_high_pivot["pivot_price"])
        h_conf_idx = last_high_pivot.get("confirmed_at_index")
        if h_conf_idx is None:
            h_conf_idx = last_high_pivot["index"] + k

        # Check FEED_GAP on broken pivot
        if last_high_pivot.get("data_quality_status") == "FEED_GAP":
            quality_status = "FEED_GAP_SUPPRESSED"
            lifecycle_state = BreakLifecycleState.FEED_GAP_SUPPRESSED
            explanation = f"Structural break check suppressed: Swing High ({target_high}) spans an unverified feed gap."
        else:
            # Scan historical closed candles from confirmation index to detect first closed break
            first_break_idx = None
            scan_end = n if is_closed else n - 1
            for j in range(h_conf_idx, scan_end):
                if float(candles[j]["close"]) > target_high:
                    first_break_idx = j
                    break

            if first_break_idx == n - 1 and is_closed:
                # Fresh confirmed break on the latest candle!
                broken_price = target_high
                broken_ts = last_high_pivot.get("pivot_timestamp")
                lifecycle_state = BreakLifecycleState.NEW_CONFIRMED_BREAK
                invalidation_level = float(last_low_pivot["pivot_price"]) if last_low_pivot else None

                if current_state == StructureType.BULLISH:
                    event_type = StructuralBreakType.BOS_BULLISH
                    resulting_structure = StructureType.BULLISH
                    explanation = f"Bullish BOS confirmed: Candle close ({curr_close}) broke above confirmed Swing High ({target_high}). Level consumed."
                elif current_state == StructureType.BEARISH:
                    event_type = StructuralBreakType.CHOCH_BULLISH
                    resulting_structure = StructureType.TRANSITIONAL
                    explanation = f"Bullish CHoCH confirmed: Candle close ({curr_close}) broke above confirmed Lower High ({target_high}), signaling potential trend reversal."
                else:
                    event_type = StructuralBreakType.BOS_BULLISH
                    resulting_structure = StructureType.BULLISH
                    explanation = f"Bullish Break confirmed: Candle close ({curr_close}) broke above range high ({target_high})."

            elif first_break_idx is not None and first_break_idx < n - 1:
                # Level was already broken in the past: check for failed breakout, invalidation, or continuation
                broken_price = target_high
                broken_ts = last_high_pivot.get("pivot_timestamp")
                invalidation_level = float(last_low_pivot["pivot_price"]) if last_low_pivot else None

                # Check if invalidated since first_break_idx
                is_inv = False
                for j in range(first_break_idx + 1, n):
                    if invalidation_level is not None and float(candles[j]["close"]) < invalidation_level:
                        is_inv = True
                        break

                if is_inv:
                    lifecycle_state = BreakLifecycleState.INVALIDATED
                    event_type = StructuralBreakType.NONE
                    explanation = f"Prior break of Swing High {target_high} was invalidated when candle closed below {invalidation_level}."
                elif curr_close < target_high:
                    lifecycle_state = BreakLifecycleState.FAILED_BREAKOUT
                    event_type = StructuralBreakType.NONE
                    explanation = f"Breakout above Swing High {target_high} at bar index {first_break_idx} failed: Candle closed back inside level at {curr_close} (Failed Breakout / Bull Trap)."
                else:
                    lifecycle_state = BreakLifecycleState.CONSUMED  # Level consumed, breakout in continuation
                    event_type = StructuralBreakType.NONE
                    explanation = f"Swing High {target_high} was already broken at bar index {first_break_idx}; level consumed (continuation). Waiting for new swing high."

            elif not is_closed and curr_close > target_high and first_break_idx is None:
                # Active forming bar is trading above target_high
                broken_price = target_high
                broken_ts = last_high_pivot.get("pivot_timestamp")
                lifecycle_state = BreakLifecycleState.DEVELOPING_BREAK
                invalidation_level = float(last_low_pivot["pivot_price"]) if last_low_pivot else None
                event_type = StructuralBreakType.BOS_BULLISH if current_state != StructureType.BEARISH else StructuralBreakType.CHOCH_BULLISH
                explanation = f"Developing Bullish Break: Active candle close ({curr_close}) currently above Swing High ({target_high}); unconfirmed until closure."

            elif curr_high > target_high and curr_close <= target_high and first_break_idx is None:
                # Wick-only break: False Break / Liquidity Sweep
                event_type = StructuralBreakType.FALSE_BREAK_HIGH
                lifecycle_state = BreakLifecycleState.UNBROKEN_SWEEP
                broken_price = target_high
                broken_ts = last_high_pivot.get("pivot_timestamp")
                explanation = f"False Break (Liquidity Sweep) at High: Wick probed above {target_high} (High: {curr_high}), but closed below ({curr_close})."

    # 2. Evaluate Bearish Breakouts (if no bullish break occurred or if checking low break)
    if event_type == StructuralBreakType.NONE and lifecycle_state in (BreakLifecycleState.UNBROKEN, BreakLifecycleState.CONSUMED) and last_low_pivot:
        target_low = float(last_low_pivot["pivot_price"])
        l_conf_idx = last_low_pivot.get("confirmed_at_index")
        if l_conf_idx is None:
            l_conf_idx = last_low_pivot["index"] + k

        if last_low_pivot.get("data_quality_status") == "FEED_GAP":
            quality_status = "FEED_GAP_SUPPRESSED"
            lifecycle_state = BreakLifecycleState.FEED_GAP_SUPPRESSED
            explanation = f"Structural break check suppressed: Swing Low ({target_low}) spans an unverified feed gap."
        else:
            first_low_break_idx = None
            scan_end = n if is_closed else n - 1
            for j in range(l_conf_idx, scan_end):
                if float(candles[j]["close"]) < target_low:
                    first_low_break_idx = j
                    break

            if first_low_break_idx == n - 1 and is_closed:
                # Fresh confirmed bearish break on the latest candle!
                broken_price = target_low
                broken_ts = last_low_pivot.get("pivot_timestamp")
                lifecycle_state = BreakLifecycleState.NEW_CONFIRMED_BREAK
                invalidation_level = float(last_high_pivot["pivot_price"]) if last_high_pivot else None

                if current_state == StructureType.BEARISH:
                    event_type = StructuralBreakType.BOS_BEARISH
                    resulting_structure = StructureType.BEARISH
                    explanation = f"Bearish BOS confirmed: Candle close ({curr_close}) broke below confirmed Swing Low ({target_low}). Level consumed."
                elif current_state == StructureType.BULLISH:
                    event_type = StructuralBreakType.CHOCH_BEARISH
                    resulting_structure = StructureType.TRANSITIONAL
                    explanation = f"Bearish CHoCH confirmed: Candle close ({curr_close}) broke below confirmed Higher Low ({target_low}), signaling potential trend reversal."
                else:
                    event_type = StructuralBreakType.BOS_BEARISH
                    resulting_structure = StructureType.BEARISH
                    explanation = f"Bearish Break confirmed: Candle close ({curr_close}) broke below range low ({target_low})."

            elif first_low_break_idx is not None and first_low_break_idx < n - 1:
                # Low level was already consumed in the past: check for failed breakout, invalidation, or continuation
                broken_price = target_low
                broken_ts = last_low_pivot.get("pivot_timestamp")
                invalidation_level = float(last_high_pivot["pivot_price"]) if last_high_pivot else None

                is_inv = False
                for j in range(first_low_break_idx + 1, n):
                    if invalidation_level is not None and float(candles[j]["close"]) > invalidation_level:
                        is_inv = True
                        break

                if is_inv:
                    lifecycle_state = BreakLifecycleState.INVALIDATED
                    event_type = StructuralBreakType.NONE
                    explanation = f"Prior break of Swing Low {target_low} was invalidated when candle closed above {invalidation_level}."
                elif curr_close > target_low:
                    lifecycle_state = BreakLifecycleState.FAILED_BREAKOUT
                    event_type = StructuralBreakType.NONE
                    explanation = f"Breakout below Swing Low {target_low} at bar index {first_low_break_idx} failed: Candle closed back inside level at {curr_close} (Failed Breakout / Bear Trap)."
                else:
                    lifecycle_state = BreakLifecycleState.CONSUMED
                    event_type = StructuralBreakType.NONE
                    explanation = f"Swing Low {target_low} was already broken at bar index {first_low_break_idx}; level consumed (continuation). Waiting for new swing low."

            elif not is_closed and curr_close < target_low and first_low_break_idx is None:
                broken_price = target_low
                broken_ts = last_low_pivot.get("pivot_timestamp")
                lifecycle_state = BreakLifecycleState.DEVELOPING_BREAK
                invalidation_level = float(last_high_pivot["pivot_price"]) if last_high_pivot else None
                event_type = StructuralBreakType.BOS_BEARISH if current_state != StructureType.BULLISH else StructuralBreakType.CHOCH_BEARISH
                explanation = f"Developing Bearish Break: Active candle close ({curr_close}) currently below Swing Low ({target_low}); unconfirmed until closure."

            elif curr_low < target_low and curr_close >= target_low and first_low_break_idx is None:
                # Wick-only break: False Break / Liquidity Sweep
                event_type = StructuralBreakType.FALSE_BREAK_LOW
                lifecycle_state = BreakLifecycleState.UNBROKEN_SWEEP
                broken_price = target_low
                broken_ts = last_low_pivot.get("pivot_timestamp")
                explanation = f"False Break (Liquidity Sweep) at Low: Wick probed below {target_low} (Low: {curr_low}), but closed above ({curr_close})."

    # Assemble historical event log
    event_history: List[Dict[str, Any]] = []
    if last_high_pivot and 'first_break_idx' in locals() and first_break_idx is not None:
        target_h_val = float(last_high_pivot["pivot_price"])
        inv_h = float(last_low_pivot["pivot_price"]) if last_low_pivot else None
        curr_h_lc = lifecycle_state if broken_price == target_h_val else BreakLifecycleState.CONSUMED
        event_history.append({
            "event_type": StructuralBreakType.BOS_BULLISH if current_state != StructureType.BEARISH else StructuralBreakType.CHOCH_BULLISH,
            "break_index": first_break_idx,
            "break_candle_timestamp": candles[first_break_idx].get("timestamp") or candles[first_break_idx].get("start_time"),
            "broken_level_price": target_h_val,
            "broken_level_timestamp": last_high_pivot.get("pivot_timestamp"),
            "initial_lifecycle": BreakLifecycleState.NEW_CONFIRMED_BREAK,
            "current_lifecycle": curr_h_lc,
            "invalidation_level": inv_h
        })

    if last_low_pivot and 'first_low_break_idx' in locals() and first_low_break_idx is not None:
        target_l_val = float(last_low_pivot["pivot_price"])
        inv_l = float(last_high_pivot["pivot_price"]) if last_high_pivot else None
        curr_l_lc = lifecycle_state if broken_price == target_l_val else BreakLifecycleState.CONSUMED
        event_history.append({
            "event_type": StructuralBreakType.BOS_BEARISH if current_state != StructureType.BULLISH else StructuralBreakType.CHOCH_BEARISH,
            "break_index": first_low_break_idx,
            "break_candle_timestamp": candles[first_low_break_idx].get("timestamp") or candles[first_low_break_idx].get("start_time"),
            "broken_level_price": target_l_val,
            "broken_level_timestamp": last_low_pivot.get("pivot_timestamp"),
            "initial_lifecycle": BreakLifecycleState.NEW_CONFIRMED_BREAK,
            "current_lifecycle": curr_l_lc,
            "invalidation_level": inv_l
        })

    is_level_consumed = lifecycle_state in (BreakLifecycleState.CONSUMED, BreakLifecycleState.FAILED_BREAKOUT, BreakLifecycleState.INVALIDATED)

    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe,
        "event_type": event_type,
        "lifecycle_state": lifecycle_state,
        "is_level_consumed": is_level_consumed,
        "broken_level_price": broken_price,
        "broken_level_timestamp": broken_ts,
        "break_candle_timestamp": curr_ts,
        "break_candle_close": curr_close,
        "previous_structure": current_state,
        "resulting_structure": resulting_structure,
        "invalidation_level": invalidation_level,
        "confirmation_status": ConfirmationStatus.CONFIRMED if is_closed else ConfirmationStatus.DEVELOPING,
        "calculation_status": CalculationStatus.VALID,
        "data_quality_status": quality_status,
        "explanation": explanation,
        "event_history": event_history
    }


# ============================================================================
# 4. Market Regime Engine
# ============================================================================

def classify_market_regime(
    candles: List[Dict[str, Any]],
    indicators: Dict[str, Any],
    k: int = 2,
    symbol: str = "UNKNOWN",
    timeframe: str = "5m",
    adx_trend_threshold: float = 25.0,
    adx_chop_threshold: float = 20.0,
    atr_expansion_multiplier: float = 1.5,
    structure_data: Optional[Dict[str, Any]] = None,
    break_data: Optional[Dict[str, Any]] = None,
    squeeze_release_expiry_bars: int = 5,
    choch_expiry_bars: int = 10
) -> Dict[str, Any]:
    """
    Classifies the Market Regime into one of 7 mutually exclusive states using a deterministic precedence hierarchy:
    1. INSUFFICIENT_DATA (Priority 1): Series < 28 bars or missing indicator dependencies.
    2. VOLATILITY_COMPRESSION (Priority 2): Carter's Squeeze ON (BB inside KC). Energy coiling; directional bias deferred.
    3. VOLATILITY_EXPANSION (Priority 3): Squeeze Release within active persistence horizon (<= 5 bars) without mature trend.
    4. TRANSITIONAL (Priority 4): Structural disruption (CHoCH event within 10 bars), direct MA/structure contradiction,
       or ADX in neutral range (20-25) without unified directional confirmation.
    5. TRENDING_BULLISH (Priority 5): Bullish structure + Bullish EMA Stack (9>15>50) + (+DI > -DI) + (ADX >= 25 or resolved 20-25).
    6. TRENDING_BEARISH (Priority 6): Bearish structure + Bearish EMA Stack (9<15<50) + (-DI > +DI) + (ADX >= 25 or resolved 20-25).
    7. RANGING (Priority 7 - Default): ADX < 20 (chop), mixed MAs, or sideways range-bound structure.

    Deterministic Nuances:
    - ADX measures trend strength, not direction.
    - Squeeze release does not imply breakout direction.
    - ADX 20-25 is classified as TRANSITIONAL unless unified structure, EMA stack, and DI spread resolve it.
    """
    if len(candles) < 28 or not indicators:
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.INSUFFICIENT_DATA,
            "confidence_factors": [],
            "conflicting_factors": [],
            "metrics": {},
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA,
            "explanation": "Insufficient candle history (< 28 bars) for reliable regime classification."
        }

    struct_res = structure_data if structure_data is not None else classify_market_structure(candles, k=k, symbol=symbol, timeframe=timeframe)
    break_res = break_data if break_data is not None else detect_structural_breaks(candles, k=k, symbol=symbol, timeframe=timeframe)

    ma_data = indicators.get("moving_averages", {})
    trend_data = indicators.get("trend_strength", {})
    vol_data = indicators.get("volatility", {})

    ma_alignment = ma_data.get("alignment", "MIXED")
    adx_val = trend_data.get("adx")
    p_di = trend_data.get("plus_di")
    m_di = trend_data.get("minus_di")

    squeeze_data = vol_data.get("squeeze", {})
    is_squeeze_on = squeeze_data.get("is_squeeze_on", False)
    is_squeeze_release = squeeze_data.get("is_squeeze_release", False)
    bw = squeeze_data.get("bandwidth", 0.0) or 0.0
    atr_val = vol_data.get("atr")

    structure_state = struct_res.get("structure_state", StructureType.INSUFFICIENT_DATA)
    break_event = break_res.get("event_type", StructuralBreakType.NONE)

    confidence_factors = []
    conflicting_factors = []

    # -------------------------------------------------------------------------
    # Priority 2: Volatility Compression (Squeeze ON)
    # Absolute precedence over trending/directional states while bands are coiling
    # -------------------------------------------------------------------------
    if is_squeeze_on:
        confidence_factors.append("Bollinger Bands contracted strictly inside Keltner Channels (Carter's Squeeze ON).")
        confidence_factors.append(f"Compressed Bandwidth: {bw:.4f}")
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.VOLATILITY_COMPRESSION,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {
                "structure": structure_state,
                "ma_alignment": ma_alignment,
                "adx": adx_val,
                "bandwidth": bw,
                "is_squeeze_on": True
            },
            "calculation_status": CalculationStatus.VALID,
            "explanation": "Market is in Volatility Compression (Carter's Squeeze ON). Energy is coiling; directional bias deferred until release."
        }

    # Helper flags for mature trend evaluation
    is_bull_structure = structure_state == StructureType.BULLISH or break_event in (StructuralBreakType.BOS_BULLISH, StructuralBreakType.CHOCH_BULLISH)
    is_bear_structure = structure_state == StructureType.BEARISH or break_event in (StructuralBreakType.BOS_BEARISH, StructuralBreakType.CHOCH_BEARISH)
    is_bull_ma = ma_alignment == "BULLISH_STACK"
    is_bear_ma = ma_alignment == "BEARISH_STACK"
    is_bull_di = (p_di is not None and m_di is not None and p_di > m_di)
    is_bear_di = (p_di is not None and m_di is not None and m_di > p_di)
    di_spread = abs(p_di - m_di) if (p_di is not None and m_di is not None) else 0.0

    # ADX Tier Evaluation
    is_strong_trend_adx = (adx_val is not None and adx_val >= adx_trend_threshold)
    is_developing_adx = (adx_val is not None and adx_chop_threshold <= adx_val < adx_trend_threshold)

    # Mature trend resolution
    is_mature_bullish = is_bull_structure and is_bull_ma and is_bull_di and (is_strong_trend_adx or (is_developing_adx and di_spread >= 5.0))
    is_mature_bearish = is_bear_structure and is_bear_ma and is_bear_di and (is_strong_trend_adx or (is_developing_adx and di_spread >= 5.0))

    # -------------------------------------------------------------------------
    # Priority 3: Volatility Expansion (Squeeze Release)
    # Active if squeeze released within persistence horizon (<= 5 bars) and mature trend not yet formed
    # -------------------------------------------------------------------------
    if is_squeeze_release and not (is_mature_bullish or is_mature_bearish):
        confidence_factors.append("Squeeze Release event detected (bands expanding outside Keltner Channels).")
        confidence_factors.append(f"Expanding Bandwidth: {bw:.4f}")
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.VOLATILITY_EXPANSION,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {
                "structure": structure_state,
                "ma_alignment": ma_alignment,
                "adx": adx_val,
                "bandwidth": bw,
                "is_squeeze_release": True
            },
            "calculation_status": CalculationStatus.VALID,
            "explanation": "Market is in Volatility Expansion (Squeeze Release). Compression has released, but clear directional trend is still resolving."
        }

    # -------------------------------------------------------------------------
    # Priority 4: Transitional State
    # Structural break reversal (CHoCH), Structure/MA contradiction, or ADX 20-25 unresolved
    # -------------------------------------------------------------------------
    has_active_choch = break_event in (StructuralBreakType.CHOCH_BULLISH, StructuralBreakType.CHOCH_BEARISH)
    has_structure_ma_conflict = (is_bull_structure and is_bear_ma) or (is_bear_structure and is_bull_ma)
    is_unresolved_adx_midzone = is_developing_adx and not (is_mature_bullish or is_mature_bearish)

    if has_active_choch or structure_state == StructureType.TRANSITIONAL or has_structure_ma_conflict or is_unresolved_adx_midzone:
        if has_active_choch:
            confidence_factors.append(f"Structural transition: {break_res.get('explanation')}")
        if structure_state == StructureType.TRANSITIONAL:
            confidence_factors.append("Market structure is transitional with mixed swing progression.")
        if has_structure_ma_conflict:
            conflicting_factors.append(f"Structure/MA contradiction: Structure is {structure_state} but EMAs are in {ma_alignment}.")
        if is_unresolved_adx_midzone:
            confidence_factors.append(f"ADX = {adx_val:.2f} is in neutral/developing range ({adx_chop_threshold:.0f}-{adx_trend_threshold:.0f}) without unified directional alignment.")

        if is_unresolved_adx_midzone and not has_active_choch and not has_structure_ma_conflict:
            explanation = f"Market is in a Transitional state: ADX is in neutral/developing range ({adx_chop_threshold:.0f}-{adx_trend_threshold:.0f}) without unified directional confirmation."
        else:
            explanation = "Market is in a Transitional state: Directional forces are realigning following a structural break, moving average divergence, or neutral ADX momentum."

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.TRANSITIONAL,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {
                "structure": structure_state,
                "ma_alignment": ma_alignment,
                "adx": adx_val,
                "break_event": break_event,
                "di_spread": round(di_spread, 2)
            },
            "calculation_status": CalculationStatus.VALID,
            "explanation": explanation
        }

    # -------------------------------------------------------------------------
    # Priority 5: Trending Bullish
    # -------------------------------------------------------------------------
    if is_mature_bullish:
        confidence_factors.append("Confirmed Higher Highs and Higher Lows structure (or Bullish BOS).")
        confidence_factors.append("EMA alignment: 9 > 15 > 50 (Bullish Stack).")
        if is_strong_trend_adx:
            confidence_factors.append(f"ADX = {adx_val:.2f} >= {adx_trend_threshold} with +DI ({p_di:.2f}) > -DI ({m_di:.2f}).")
            explanation = "Market is in a confirmed Strong Bullish Trend across structure, moving averages, and directional momentum."
        else:
            confidence_factors.append(f"Developing Bullish Trend: ADX = {adx_val:.2f} with strong directional spread (+DI - -DI = {di_spread:.2f} >= 5.0).")
            explanation = f"Developing Bullish Trend: ADX is in 20-25 range ({adx_val:.1f}) confirmed by unified bullish structure and EMA stack."

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.TRENDING_BULLISH,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {"structure": structure_state, "ma_alignment": ma_alignment, "adx": adx_val, "plus_di": p_di, "minus_di": m_di},
            "calculation_status": CalculationStatus.VALID,
            "explanation": explanation
        }

    # -------------------------------------------------------------------------
    # Priority 6: Trending Bearish
    # -------------------------------------------------------------------------
    if is_mature_bearish:
        confidence_factors.append("Confirmed Lower Highs and Lower Lows structure (or Bearish BOS).")
        confidence_factors.append("EMA alignment: 9 < 15 < 50 (Bearish Stack).")
        if is_strong_trend_adx:
            confidence_factors.append(f"ADX = {adx_val:.2f} >= {adx_trend_threshold} with -DI ({m_di:.2f}) > +DI ({p_di:.2f}).")
            explanation = "Market is in a confirmed Strong Bearish Trend across structure, moving averages, and directional momentum."
        else:
            confidence_factors.append(f"Developing Bearish Trend: ADX = {adx_val:.2f} with strong directional spread (-DI - +DI = {di_spread:.2f} >= 5.0).")
            explanation = f"Developing Bearish Trend: ADX is in 20-25 range ({adx_val:.1f}) confirmed by unified bearish structure and EMA stack."

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.TRENDING_BEARISH,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {"structure": structure_state, "ma_alignment": ma_alignment, "adx": adx_val, "plus_di": p_di, "minus_di": m_di},
            "calculation_status": CalculationStatus.VALID,
            "explanation": explanation
        }

    # -------------------------------------------------------------------------
    # Priority 7: Ranging vs Transitional Fallback
    # RANGING strictly requires genuine range-bound evidence:
    # 1. Low directional momentum (ADX < adx_chop_threshold e.g. 20.0), AND
    # 2. Confirmed SIDEWAYS structure OR MIXED moving averages.
    # If ADX is strong (>= 25) with incomplete or contradictory structure/EMAs,
    # it MUST classify as TRANSITIONAL, never blindly default to RANGING!
    # -------------------------------------------------------------------------
    is_low_adx = (adx_val is not None and adx_val < adx_chop_threshold)
    is_sideways_struct = (structure_state in (StructureType.SIDEWAYS,))
    is_mixed_ma = (ma_alignment == "MIXED")

    is_genuine_ranging = (is_low_adx and (is_sideways_struct or is_mixed_ma)) or (is_sideways_struct and not is_strong_trend_adx)

    if is_genuine_ranging:
        if is_low_adx:
            confidence_factors.append(f"ADX = {adx_val:.2f} < {adx_chop_threshold} indicating weak directional momentum (Chop).")
        if is_sideways_struct:
            confidence_factors.append("Market structure is oscillating sideways with equal highs/lows.")
        if is_mixed_ma:
            confidence_factors.append("Moving averages are mixed / converging without directional stacking.")

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.RANGING,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {"structure": structure_state, "ma_alignment": ma_alignment, "adx": adx_val},
            "calculation_status": CalculationStatus.VALID,
            "explanation": f"Market is in a confirmed Ranging/Consolidation regime: ADX ({adx_val:.1f} < {adx_chop_threshold:.0f}) reflects low directional momentum within horizontal structure."
        }
    else:
        # Conflicted, incomplete, or realigning conditions -> TRANSITIONAL
        if is_strong_trend_adx:
            conflicting_factors.append(f"High trend momentum (ADX = {adx_val:.2f} >= {adx_trend_threshold:.0f}) detected, but structure ({structure_state}) and EMA alignment ({ma_alignment}) are contradictory or incomplete.")
        else:
            conflicting_factors.append(f"Market conditions lack unified alignment: structure is {structure_state}, EMAs are {ma_alignment}, ADX is {adx_val}.")

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "regime": MarketRegime.TRANSITIONAL,
            "confidence_factors": confidence_factors,
            "conflicting_factors": conflicting_factors,
            "metrics": {"structure": structure_state, "ma_alignment": ma_alignment, "adx": adx_val},
            "calculation_status": CalculationStatus.VALID,
            "explanation": f"Market is in a Transitional/Conflicted state: Trend strength (ADX = {adx_val:.1f}) lacks unified structural or moving average confirmation."
        }


# ============================================================================
# 5. Divergence Detection Engine
# ============================================================================

def detect_divergence(
    candles: List[Dict[str, Any]],
    indicator_series: List[Optional[float]],
    indicator_name: str = "RSI",
    k: int = 2,
    symbol: str = "UNKNOWN",
    timeframe: str = "1m",
    max_bars_lookback: int = 40,
    tolerance_pct: float = 0.0005
) -> Dict[str, Any]:
    """
    Detects Regular and Hidden Divergences between Price and an Indicator series (e.g. RSI, MACD Hist).

    Mathematical Rules:
    1. Regular Bullish Divergence (Reversal):
       Price forms Lower Low (LL): Low[2] < Low[1] - tol
       Indicator forms Higher Low (HL): Ind[2] > Ind[1] + tol
       Invalidation: Price closes below Low[2].
    2. Regular Bearish Divergence (Reversal):
       Price forms Higher High (HH): High[2] > High[1] + tol
       Indicator forms Lower High (LH): Ind[2] < Ind[1] - tol
       Invalidation: Price closes above High[2].
    3. Hidden Bullish Divergence (Continuation):
       Price forms Higher Low (HL): Low[2] > Low[1] + tol
       Indicator forms Lower Low (LL): Ind[2] < Ind[1] - tol
       Invalidation: Price closes below Low[1].
    4. Hidden Bearish Divergence (Continuation):
       Price forms Lower High (LH): High[2] < High[1] - tol
       Indicator forms Higher High (HH): Ind[2] > Ind[1] + tol
       Invalidation: Price closes above High[1].

    Guarantees:
    - Zero repainting: Evaluates confirmed pivots where right k-bars are closed.
    - Indicator pivot matching: Searches +/-1 bar around price pivot timestamp to match indicator peak/trough.
    """
    n = len(candles)
    if n != len(indicator_series) or n < 2 * k + 2:
        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "has_divergence": False,
            "divergence_type": DivergenceType.NO_DIVERGENCE,
            "confirmation_status": ConfirmationStatus.CONFIRMED,
            "indicator_name": indicator_name,
            "details": None
        }

    pivots_data = identify_fractal_pivots(candles, k=k, symbol=symbol, timeframe=timeframe)
    highs = [p for p in pivots_data["swing_highs"] if p["confirmation_status"] == ConfirmationStatus.CONFIRMED]
    lows = [p for p in pivots_data["swing_lows"] if p["confirmation_status"] == ConfirmationStatus.CONFIRMED]

    def get_indicator_pivot_val(price_idx: int, is_high: bool) -> Tuple[Optional[float], Optional[int]]:
        """
        Extracts the indicator pivot value within a +/- 1 bar window around price_idx.
        Deterministic Tie-Breaking Rule:
        Preference score:
        1. Exact price timestamp (idx == price_idx) -> Score 2
        2. Earlier historical bar (idx == price_idx - 1) -> Score 1
        3. Later bar (idx == price_idx + 1) -> Score 0
        """
        window = [price_idx - 1, price_idx, price_idx + 1]
        valid_vals = []
        for idx in window:
            if 0 <= idx < len(indicator_series) and indicator_series[idx] is not None:
                pref = 2 if idx == price_idx else (1 if idx < price_idx else 0)
                valid_vals.append((idx, float(indicator_series[idx]), pref))
        if not valid_vals:
            return None, None
        if is_high:
            # Maximum indicator value; if tied, highest preference score
            best = max(valid_vals, key=lambda x: (x[1], x[2]))
            return best[1], best[0]
        else:
            # Minimum indicator value; if tied, highest preference score
            best = min(valid_vals, key=lambda x: (x[1], -x[2]))
            return best[1], best[0]

    # Evaluate Bullish Divergences on Swing Lows
    bullish_result = None
    if len(lows) >= 2:
        p1 = lows[-2]
        p2 = lows[-1]
        if (p2["index"] - p1["index"]) <= max_bars_lookback:
            # Feed Gap Check: Disqualify if either pivot spans an unverified feed gap
            if p1.get("data_quality_status") == "FEED_GAP" or p2.get("data_quality_status") == "FEED_GAP":
                bullish_result = {
                    "symbol": symbol.strip().upper(),
                    "timeframe": timeframe,
                    "has_divergence": False,
                    "divergence_type": DivergenceType.NO_DIVERGENCE,
                    "confirmation_status": ConfirmationStatus.CONFIRMED,
                    "indicator_name": indicator_name,
                    "invalidation_status": DivergenceInvalidationStatus.FEED_GAP_SUPPRESSED,
                    "data_quality_status": "FEED_GAP_SUPPRESSED",
                    "explanation": f"Divergence evaluation on {indicator_name} suppressed: Swing low spans an unverified feed gap."
                }
            else:
                ind1, ind1_idx = get_indicator_pivot_val(p1["index"], is_high=False)
                ind2, ind2_idx = get_indicator_pivot_val(p2["index"], is_high=False)

                if ind1 is not None and ind2 is not None:
                    p_diff = p2["pivot_price"] - p1["pivot_price"]
                    p_tol = p1["pivot_price"] * tolerance_pct
                    ind_diff = ind2 - ind1
                    ind_tol = 0.2 if indicator_name == "RSI" else 0.02
                    p2_conf = p2.get("confirmed_at_index") or (p2["index"] + k)

                    # Regular Bullish: Price LL, Indicator HL (Reversal)
                    if p_diff < -p_tol and ind_diff > ind_tol:
                        inv_level = p2["pivot_price"]
                        closed_below = any(float(candles[j]["close"]) < inv_level for j in range(p2_conf, n))
                        wick_probed = any(float(candles[j]["low"]) < inv_level for j in range(p2_conf, n))

                        if closed_below:
                            inv_status = DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE
                            has_div = False
                            status_desc = f"Invalidated: Subsequent closed candle broke below invalidation level {inv_level}."
                        elif wick_probed:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_WICK_PROBED
                            has_div = True
                            status_desc = f"Active (Wick probed below {inv_level} without closed break)."
                        else:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_UNTOUCHED
                            has_div = True
                            status_desc = f"Active and confirmed. Invalidation level: {inv_level}."

                        bullish_result = {
                            "symbol": symbol.strip().upper(),
                            "timeframe": timeframe,
                            "has_divergence": has_div,
                            "divergence_type": DivergenceType.REGULAR_BULLISH,
                            "confirmation_status": ConfirmationStatus.CONFIRMED,
                            "invalidation_status": inv_status,
                            "bias": "BULLISH",
                            "category": "REGULAR_REVERSAL",
                            "indicator_name": indicator_name,
                            "pivot1_timestamp": p1["pivot_timestamp"],
                            "pivot2_timestamp": p2["pivot_timestamp"],
                            "pivot1_price": p1["pivot_price"],
                            "pivot2_price": p2["pivot_price"],
                            "pivot1_indicator_value": round(ind1, 2),
                            "pivot2_indicator_value": round(ind2, 2),
                            "price_movement": f"{p1['pivot_price']} -> {p2['pivot_price']} (Lower Low)",
                            "indicator_movement": f"{ind1:.2f} -> {ind2:.2f} (Higher Low)",
                            "confirmation_timestamp": p2.get("confirmation_timestamp"),
                            "invalidation_level": inv_level,
                            "data_quality_status": "VALID",
                            "explanation": f"Regular Bullish Divergence on {indicator_name}: Price formed Lower Low while {indicator_name} formed Higher Low, indicating exhausted selling momentum. {status_desc}"
                        }

                    # Hidden Bullish: Price HL, Indicator LL (Continuation)
                    elif p_diff > p_tol and ind_diff < -ind_tol:
                        inv_level = p1["pivot_price"]
                        closed_below = any(float(candles[j]["close"]) < inv_level for j in range(p2_conf, n))
                        wick_probed = any(float(candles[j]["low"]) < inv_level for j in range(p2_conf, n))

                        if closed_below:
                            inv_status = DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE
                            has_div = False
                            status_desc = f"Invalidated: Subsequent closed candle broke below invalidation level {inv_level}."
                        elif wick_probed:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_WICK_PROBED
                            has_div = True
                            status_desc = f"Active (Wick probed below {inv_level} without closed break)."
                        else:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_UNTOUCHED
                            has_div = True
                            status_desc = f"Active and confirmed. Invalidation level: {inv_level}."

                        bullish_result = {
                            "symbol": symbol.strip().upper(),
                            "timeframe": timeframe,
                            "has_divergence": has_div,
                            "divergence_type": DivergenceType.HIDDEN_BULLISH,
                            "confirmation_status": ConfirmationStatus.CONFIRMED,
                            "invalidation_status": inv_status,
                            "bias": "BULLISH",
                            "category": "HIDDEN_CONTINUATION",
                            "indicator_name": indicator_name,
                            "pivot1_timestamp": p1["pivot_timestamp"],
                            "pivot2_timestamp": p2["pivot_timestamp"],
                            "pivot1_price": p1["pivot_price"],
                            "pivot2_price": p2["pivot_price"],
                            "pivot1_indicator_value": round(ind1, 2),
                            "pivot2_indicator_value": round(ind2, 2),
                            "price_movement": f"{p1['pivot_price']} -> {p2['pivot_price']} (Higher Low)",
                            "indicator_movement": f"{ind1:.2f} -> {ind2:.2f} (Lower Low)",
                            "confirmation_timestamp": p2.get("confirmation_timestamp"),
                            "invalidation_level": inv_level,
                            "data_quality_status": "VALID",
                            "explanation": f"Hidden Bullish Divergence on {indicator_name}: Price formed Higher Low while {indicator_name} formed Lower Low, signaling uptrend continuation. {status_desc}"
                        }

    # Evaluate Bearish Divergences on Swing Highs
    bearish_result = None
    if len(highs) >= 2:
        p1 = highs[-2]
        p2 = highs[-1]
        if (p2["index"] - p1["index"]) <= max_bars_lookback:
            if p1.get("data_quality_status") == "FEED_GAP" or p2.get("data_quality_status") == "FEED_GAP":
                bearish_result = {
                    "symbol": symbol.strip().upper(),
                    "timeframe": timeframe,
                    "has_divergence": False,
                    "divergence_type": DivergenceType.NO_DIVERGENCE,
                    "confirmation_status": ConfirmationStatus.CONFIRMED,
                    "indicator_name": indicator_name,
                    "invalidation_status": DivergenceInvalidationStatus.FEED_GAP_SUPPRESSED,
                    "data_quality_status": "FEED_GAP_SUPPRESSED",
                    "explanation": f"Divergence evaluation on {indicator_name} suppressed: Swing high spans an unverified feed gap."
                }
            else:
                ind1, ind1_idx = get_indicator_pivot_val(p1["index"], is_high=True)
                ind2, ind2_idx = get_indicator_pivot_val(p2["index"], is_high=True)

                if ind1 is not None and ind2 is not None:
                    p_diff = p2["pivot_price"] - p1["pivot_price"]
                    p_tol = p1["pivot_price"] * tolerance_pct
                    ind_diff = ind2 - ind1
                    ind_tol = 0.2 if indicator_name == "RSI" else 0.02
                    p2_conf = p2.get("confirmed_at_index") or (p2["index"] + k)

                    # Regular Bearish: Price HH, Indicator LH (Reversal)
                    if p_diff > p_tol and ind_diff < -ind_tol:
                        inv_level = p2["pivot_price"]
                        closed_above = any(float(candles[j]["close"]) > inv_level for j in range(p2_conf, n))
                        wick_probed = any(float(candles[j]["high"]) > inv_level for j in range(p2_conf, n))

                        if closed_above:
                            inv_status = DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE
                            has_div = False
                            status_desc = f"Invalidated: Subsequent closed candle broke above invalidation level {inv_level}."
                        elif wick_probed:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_WICK_PROBED
                            has_div = True
                            status_desc = f"Active (Wick probed above {inv_level} without closed break)."
                        else:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_UNTOUCHED
                            has_div = True
                            status_desc = f"Active and confirmed. Invalidation level: {inv_level}."

                        bearish_result = {
                            "symbol": symbol.strip().upper(),
                            "timeframe": timeframe,
                            "has_divergence": has_div,
                            "divergence_type": DivergenceType.REGULAR_BEARISH,
                            "confirmation_status": ConfirmationStatus.CONFIRMED,
                            "invalidation_status": inv_status,
                            "bias": "BEARISH",
                            "category": "REGULAR_REVERSAL",
                            "indicator_name": indicator_name,
                            "pivot1_timestamp": p1["pivot_timestamp"],
                            "pivot2_timestamp": p2["pivot_timestamp"],
                            "pivot1_price": p1["pivot_price"],
                            "pivot2_price": p2["pivot_price"],
                            "pivot1_indicator_value": round(ind1, 2),
                            "pivot2_indicator_value": round(ind2, 2),
                            "price_movement": f"{p1['pivot_price']} -> {p2['pivot_price']} (Higher High)",
                            "indicator_movement": f"{ind1:.2f} -> {ind2:.2f} (Lower High)",
                            "confirmation_timestamp": p2.get("confirmation_timestamp"),
                            "invalidation_level": inv_level,
                            "data_quality_status": "VALID",
                            "explanation": f"Regular Bearish Divergence on {indicator_name}: Price formed Higher High while {indicator_name} formed Lower High, indicating exhausted buying momentum. {status_desc}"
                        }

                    # Hidden Bearish: Price LH, Indicator HH (Continuation)
                    elif p_diff < -p_tol and ind_diff > ind_tol:
                        inv_level = p1["pivot_price"]
                        closed_above = any(float(candles[j]["close"]) > inv_level for j in range(p2_conf, n))
                        wick_probed = any(float(candles[j]["high"]) > inv_level for j in range(p2_conf, n))

                        if closed_above:
                            inv_status = DivergenceInvalidationStatus.INVALIDATED_BY_CLOSE
                            has_div = False
                            status_desc = f"Invalidated: Subsequent closed candle broke above invalidation level {inv_level}."
                        elif wick_probed:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_WICK_PROBED
                            has_div = True
                            status_desc = f"Active (Wick probed above {inv_level} without closed break)."
                        else:
                            inv_status = DivergenceInvalidationStatus.ACTIVE_UNTOUCHED
                            has_div = True
                            status_desc = f"Active and confirmed. Invalidation level: {inv_level}."

                        bearish_result = {
                            "symbol": symbol.strip().upper(),
                            "timeframe": timeframe,
                            "has_divergence": has_div,
                            "divergence_type": DivergenceType.HIDDEN_BEARISH,
                            "confirmation_status": ConfirmationStatus.CONFIRMED,
                            "invalidation_status": inv_status,
                            "bias": "BEARISH",
                            "category": "HIDDEN_CONTINUATION",
                            "indicator_name": indicator_name,
                            "pivot1_timestamp": p1["pivot_timestamp"],
                            "pivot2_timestamp": p2["pivot_timestamp"],
                            "pivot1_price": p1["pivot_price"],
                            "pivot2_price": p2["pivot_price"],
                            "pivot1_indicator_value": round(ind1, 2),
                            "pivot2_indicator_value": round(ind2, 2),
                            "price_movement": f"{p1['pivot_price']} -> {p2['pivot_price']} (Lower High)",
                            "indicator_movement": f"{ind1:.2f} -> {ind2:.2f} (Higher High)",
                            "confirmation_timestamp": p2.get("confirmation_timestamp"),
                            "invalidation_level": inv_level,
                            "data_quality_status": "VALID",
                            "explanation": f"Hidden Bearish Divergence on {indicator_name}: Price formed Lower High while {indicator_name} formed Higher High, signaling downtrend continuation. {status_desc}"
                        }

    if bullish_result and bearish_result:
        b_idx = lows[-1]["index"]
        s_idx = highs[-1]["index"]
        return bullish_result if b_idx >= s_idx else bearish_result
    elif bullish_result:
        return bullish_result
    elif bearish_result:
        return bearish_result

    return {
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe,
        "has_divergence": False,
        "divergence_type": DivergenceType.NO_DIVERGENCE,
        "confirmation_status": ConfirmationStatus.CONFIRMED,
        "indicator_name": indicator_name,
        "invalidation_status": DivergenceInvalidationStatus.ACTIVE_UNTOUCHED,
        "data_quality_status": "VALID",
        "details": None
    }


# ============================================================================
# 6. Multi-Timeframe Analysis & MarketStructureService
# ============================================================================

class MarketStructureService:
    """
    High-Level Service for Market Structure, Regime & Divergence Engine.
    Consumes MultiTimeframeCandleStore and TechnicalIndicatorService.

    Guarantees:
    - Pure non-repainting execution on closed candles.
    - Zero lower-timeframe overwrite bias (higher timeframes maintain precedence).
    - Multi-timeframe consistency across 15s, 1m, 3m, 5m, 15m.
    """
    def __init__(
        self,
        candle_store: MultiTimeframeCandleStore,
        indicator_service: Optional[TechnicalIndicatorService] = None
    ):
        self.candle_store = candle_store
        self.indicator_service = indicator_service or TechnicalIndicatorService(candle_store)

    def analyze_market_structure(
        self,
        symbol: str,
        timeframe: str,
        k: Optional[int] = None,
        candles: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Analyzes fractal pivots, market structure, and BOS/CHoCH breaks for a single timeframe."""
        eff_k = k if k is not None else DEFAULT_TIMEFRAME_K.get(timeframe, 2)
        c_list = candles if candles is not None else self.candle_store.get_closed_candles(symbol, timeframe, limit=None)
        pivots = identify_fractal_pivots(c_list, k=eff_k, symbol=symbol, timeframe=timeframe)
        structure = classify_market_structure(c_list, k=eff_k, symbol=symbol, timeframe=timeframe)
        breaks = detect_structural_breaks(c_list, k=eff_k, symbol=symbol, timeframe=timeframe)

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "k_parameter": eff_k,
            "candle_count": len(c_list),
            "pivots": pivots,
            "structure": structure,
            "structural_break": breaks
        }

    def analyze_divergences(
        self,
        symbol: str,
        timeframe: str,
        k: Optional[int] = None,
        candles: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Detects Regular and Hidden Divergences against RSI(14) and MACD Histogram."""
        eff_k = k if k is not None else DEFAULT_TIMEFRAME_K.get(timeframe, 2)
        c_list = candles if candles is not None else self.candle_store.get_closed_candles(symbol, timeframe, limit=None)
        if len(c_list) < 35:
            return {
                "symbol": symbol.strip().upper(),
                "timeframe": timeframe,
                "rsi_divergence": {"has_divergence": False, "divergence_type": DivergenceType.NO_DIVERGENCE},
                "macd_divergence": {"has_divergence": False, "divergence_type": DivergenceType.NO_DIVERGENCE}
            }

        closes = [float(c["close"]) for c in c_list]
        rsi_series = calculate_rsi(closes, period=14)

        # Calculate historical MACD histogram series in O(N) time
        macd_series_data = calculate_macd_series(closes, 12, 26, 9, round_output=False)
        macd_hist_series = macd_series_data["hist"]

        rsi_div = detect_divergence(c_list, rsi_series, indicator_name="RSI", k=eff_k, symbol=symbol, timeframe=timeframe)
        macd_div = detect_divergence(c_list, macd_hist_series, indicator_name="MACD_HIST", k=eff_k, symbol=symbol, timeframe=timeframe)

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "rsi_divergence": rsi_div,
            "macd_divergence": macd_div
        }

    def classify_regime(
        self,
        symbol: str,
        timeframe: str,
        k: Optional[int] = None,
        structure_data: Optional[Dict[str, Any]] = None,
        break_data: Optional[Dict[str, Any]] = None,
        candles: Optional[List[Dict[str, Any]]] = None,
        indicators: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Classifies the market regime for a single timeframe."""
        eff_k = k if k is not None else DEFAULT_TIMEFRAME_K.get(timeframe, 2)
        c_list = candles if candles is not None else self.candle_store.get_closed_candles(symbol, timeframe, limit=None)
        ind_packet = indicators if indicators is not None else self.indicator_service.compute_all_indicators(symbol, timeframe, include_developing=False)
        return classify_market_regime(
            c_list, ind_packet, k=eff_k, symbol=symbol, timeframe=timeframe,
            structure_data=structure_data, break_data=break_data
        )

    def analyze_multi_timeframe_suite(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
        k: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Executes independent market structure, divergence, and regime analysis
        across all 5 intraday timeframes (15s, 1m, 3m, 5m, 15m), and synthesizes
        a multi-timeframe alignment & conflict evaluation.
        Defaults to canonical K parameters: K=2 for 15s/1m/3m and K=3 for 5m/15m.
        """
        tfs = timeframes or [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]
        tf_results: Dict[str, Any] = {}

        for tf in tfs:
            eff_k = k if k is not None else DEFAULT_TIMEFRAME_K.get(tf, 2)
            c_list = self.candle_store.get_closed_candles(symbol, tf, limit=None)
            struct_data = self.analyze_market_structure(symbol, tf, k=eff_k, candles=c_list)
            div_data = self.analyze_divergences(symbol, tf, k=eff_k, candles=c_list)
            regime_data = self.classify_regime(
                symbol, tf, k=eff_k,
                structure_data=struct_data["structure"],
                break_data=struct_data["structural_break"],
                candles=c_list
            )

            tf_results[tf] = {
                "structure": struct_data["structure"],
                "structural_break": struct_data["structural_break"],
                "regime": regime_data,
                "divergences": div_data
            }

        # Multi-timeframe synthesis
        mtf_summary = self._evaluate_mtf_alignment(tf_results)

        return {
            "symbol": symbol.strip().upper(),
            "timeframe_analyses": tf_results,
            "multi_timeframe_summary": mtf_summary
        }

    def _evaluate_mtf_alignment(self, tf_results: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synthesizes macro anchor (15m, 5m) vs intermediate trigger (3m, 1m) vs refiner (15s).
        Never allows lower timeframes to overwrite higher-timeframe confirmed structure.
        """
        regimes = {tf: data["regime"]["regime"] for tf, data in tf_results.items() if "regime" in data}
        structures = {tf: data["structure"]["structure_state"] for tf, data in tf_results.items() if "structure" in data}

        reg_15m = regimes.get(Timeframe.M15, MarketRegime.INSUFFICIENT_DATA)
        reg_5m = regimes.get(Timeframe.M5, MarketRegime.INSUFFICIENT_DATA)
        reg_1m = regimes.get(Timeframe.M1, MarketRegime.INSUFFICIENT_DATA)

        # Check alignment
        if reg_15m == MarketRegime.TRENDING_BULLISH and reg_5m == MarketRegime.TRENDING_BULLISH:
            if reg_1m == MarketRegime.TRENDING_BULLISH:
                status = MTFAlignmentStatus.ALL_ALIGNED_BULLISH
                desc = "Strong Full-Spectrum Bullish Alignment: 15m, 5m, and 1m are all in confirmed TRENDING_BULLISH."
            else:
                status = MTFAlignmentStatus.CONFLICTING_MTF_REGIMES
                desc = f"Macro Bullish with Lower-TF Pullback: 15m & 5m are TRENDING_BULLISH, but 1m is {reg_1m}."
        elif reg_15m == MarketRegime.TRENDING_BEARISH and reg_5m == MarketRegime.TRENDING_BEARISH:
            if reg_1m == MarketRegime.TRENDING_BEARISH:
                status = MTFAlignmentStatus.ALL_ALIGNED_BEARISH
                desc = "Strong Full-Spectrum Bearish Alignment: 15m, 5m, and 1m are all in confirmed TRENDING_BEARISH."
            else:
                status = MTFAlignmentStatus.CONFLICTING_MTF_REGIMES
                desc = f"Macro Bearish with Lower-TF Pullback: 15m & 5m are TRENDING_BEARISH, but 1m is {reg_1m}."
        elif reg_15m == MarketRegime.RANGING and reg_5m == MarketRegime.RANGING:
            status = MTFAlignmentStatus.ALIGNED_RANGING
            desc = "Macro Ranging / Consolidation: Higher timeframes (15m, 5m) are both in RANGING regime."
        elif any(r == MarketRegime.INSUFFICIENT_DATA for r in (reg_15m, reg_5m)):
            status = MTFAlignmentStatus.INSUFFICIENT_DATA
            desc = "Insufficient data on higher timeframes (15m or 5m) to evaluate macro alignment."
        else:
            status = MTFAlignmentStatus.CONFLICTING_MTF_REGIMES
            desc = f"Conflicting MTF Regimes: 15m is {reg_15m}, 5m is {reg_5m}, and 1m is {reg_1m}."

        return {
            "alignment_status": status,
            "macro_anchor_regimes": {"15m": reg_15m, "5m": reg_5m},
            "intermediate_trigger_regimes": {"3m": regimes.get(Timeframe.M3), "1m": reg_1m},
            "refiner_regime": {"15s": regimes.get(Timeframe.S15)},
            "macro_anchor_structures": {"15m": structures.get(Timeframe.M15), "5m": structures.get(Timeframe.M5)},
            "explanation": desc
        }
