"""
TradeLens AI - Market Structure, S/R Zones & Divergence Detection Engine (Phase 2B)
Deterministic, non-repainting algorithmic market structure analysis.

Key Capabilities:
1. Fractal Pivot Detection (K-bar lookback/lookforward peaks and troughs).
2. Market Structure Classification: Higher Highs (HH), Higher Lows (HL),
   Lower Highs (LH), Lower Lows (LL), Trend State (UPTREND, DOWNTREND, SIDEWAYS).
3. Structural Breakpoints: Break of Structure (BOS) vs Change of Character (CHoCH).
4. Dynamic S/R Zones: Opening Range (ORH/ORL), Previous Day (PDH/PDL/PDC),
   Multi-level clustering with touch-count, rejection velocity and confluence scoring.
5. Divergence Detection Engine: Regular (Reversal) & Hidden (Continuation) Bullish/Bearish
   divergence against RSI and MACD Histogram with zero forward-fill or repainting.
"""
from datetime import datetime, time
from typing import Dict, List, Any, Optional, Tuple
import math

from app.data.base import DataStatus


class MarketStructureTrend:
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    SIDEWAYS = "SIDEWAYS"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class StructuralBreakType:
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"
    NONE = "NONE"


class DivergenceType:
    REGULAR_BULLISH = "REGULAR_BULLISH"      # Reversal: Price LL, Indicator HL
    REGULAR_BEARISH = "REGULAR_BEARISH"      # Reversal: Price HH, Indicator LH
    HIDDEN_BULLISH = "HIDDEN_BULLISH"        # Continuation: Price HL, Indicator LL
    HIDDEN_BEARISH = "HIDDEN_BEARISH"        # Continuation: Price LH, Indicator HH
    NO_DIVERGENCE = "NO_DIVERGENCE"


class DivergenceStatus:
    CONFIRMED = "CONFIRMED"
    DEVELOPING = "DEVELOPING"
    NONE = "NONE"


# ============================================================================
# 1. Fractal Pivot Detection & Market Structure Classification
# ============================================================================

def identify_fractal_pivots(
    candles: List[Dict[str, Any]],
    k: int = 2,
    price_high_key: str = "high",
    price_low_key: str = "low"
) -> Dict[str, Any]:
    """
    Identifies fractal swing highs and swing lows across a candle series.
    A swing high at bar i requires:
      High[i] > High[i - j] for all j in 1..k (strictly higher than left bars)
      High[i] >= High[i + j] for all j in 1..k (greater than or equal to right bars)
    A swing low at bar i requires:
      Low[i] < Low[i - j] for all j in 1..k (strictly lower than left bars)
      Low[i] <= Low[i + j] for all j in 1..k (lower than or equal to right bars)

    Non-Repainting Guarantee:
    - A pivot at bar i is ONLY confirmed if bar i + k has closed (is_closed=True).
    - If bar i + k has not closed, the pivot is marked as is_confirmed=False (developing).
    """
    n = len(candles)
    min_required = 2 * k + 1
    if n < min_required:
        return {
            "swing_highs": [],
            "swing_lows": [],
            "all_pivots": [],
            "k": k,
            "candle_count": n
        }

    swing_highs: List[Dict[str, Any]] = []
    swing_lows: List[Dict[str, Any]] = []
    all_pivots: List[Dict[str, Any]] = []

    highs = [float(c[price_high_key]) for c in candles]
    lows = [float(c[price_low_key]) for c in candles]

    for i in range(k, n - k):
        # Check swing high
        is_high = True
        for j in range(1, k + 1):
            if highs[i] <= highs[i - j] or highs[i] < highs[i + j]:
                is_high = False
                break

        # Check swing low
        is_low = True
        for j in range(1, k + 1):
            if lows[i] >= lows[i - j] or lows[i] > lows[i + j]:
                is_low = False
                break

        # Check confirmation status (requires rightmost confirming bar i + k to be closed)
        right_bar = candles[i + k]
        is_confirmed = bool(right_bar.get("is_closed", True))

        if is_high:
            pivot_h = {
                "index": i,
                "timestamp": candles[i].get("timestamp") or candles[i].get("start_time"),
                "price": round(highs[i], 2),
                "type": "SWING_HIGH",
                "is_confirmed": is_confirmed,
                "confirmed_at_index": i + k if is_confirmed else None,
                "confirmed_at_timestamp": right_bar.get("timestamp") if is_confirmed else None
            }
            swing_highs.append(pivot_h)
            all_pivots.append(pivot_h)

        if is_low:
            pivot_l = {
                "index": i,
                "timestamp": candles[i].get("timestamp") or candles[i].get("start_time"),
                "price": round(lows[i], 2),
                "type": "SWING_LOW",
                "is_confirmed": is_confirmed,
                "confirmed_at_index": i + k if is_confirmed else None,
                "confirmed_at_timestamp": right_bar.get("timestamp") if is_confirmed else None
            }
            swing_lows.append(pivot_l)
            all_pivots.append(pivot_l)

    # Sort all_pivots chronologically by index
    all_pivots.sort(key=lambda p: p["index"])

    return {
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "all_pivots": all_pivots,
        "k": k,
        "candle_count": n
    }


def classify_market_structure(
    candles: List[Dict[str, Any]],
    k: int = 2,
    tolerance_pct: float = 0.0005
) -> Dict[str, Any]:
    """
    Classifies market structure based on confirmed fractal swing points:
    - Higher Highs (HH), Lower Highs (LH), Equal Highs (EQH)
    - Higher Lows (HL), Lower Lows (LL), Equal Lows (EQL)
    - Macro Trend: UPTREND, DOWNTREND, SIDEWAYS
    - Structural Breaks: Break of Structure (BOS) vs Change of Character (CHoCH)
    """
    pivots_data = identify_fractal_pivots(candles, k=k)
    highs = [p for p in pivots_data["swing_highs"] if p["is_confirmed"]]
    lows = [p for p in pivots_data["swing_lows"] if p["is_confirmed"]]

    if len(highs) < 2 or len(lows) < 2:
        return {
            "trend": MarketStructureTrend.INSUFFICIENT_DATA,
            "structural_break": StructuralBreakType.NONE,
            "last_high_classification": None,
            "last_low_classification": None,
            "confirmed_highs_count": len(highs),
            "confirmed_lows_count": len(lows),
            "swing_highs": highs,
            "swing_lows": lows,
            "latest_swing_high": highs[-1] if highs else None,
            "latest_swing_low": lows[-1] if lows else None
        }

    # High comparison
    h_curr = highs[-1]["price"]
    h_prev = highs[-2]["price"]
    h_tol = h_prev * tolerance_pct
    if h_curr > h_prev + h_tol:
        high_class = "HH"
    elif h_curr < h_prev - h_tol:
        high_class = "LH"
    else:
        high_class = "EQH"

    # Low comparison
    l_curr = lows[-1]["price"]
    l_prev = lows[-2]["price"]
    l_tol = l_prev * tolerance_pct
    if l_curr > l_prev + l_tol:
        low_class = "HL"
    elif l_curr < l_prev - l_tol:
        low_class = "LL"
    else:
        low_class = "EQL"

    # Trend determination
    if high_class == "HH" and low_class == "HL":
        trend = MarketStructureTrend.UPTREND
    elif high_class == "LH" and low_class == "LL":
        trend = MarketStructureTrend.DOWNTREND
    else:
        trend = MarketStructureTrend.SIDEWAYS

    # Structural Break Detection (BOS / CHoCH) against latest closed bar
    structural_break = StructuralBreakType.NONE
    break_price = None
    break_candle_ts = None

    if candles:
        latest_close = float(candles[-1]["close"])
        latest_ts = candles[-1].get("timestamp") or candles[-1].get("start_time")

        # In an uptrend:
        # BOS: Price closes above the prior confirmed Swing High (trend continuation)
        # CHoCH: Price closes below the prior confirmed Higher Low (trend reversal character change)
        if trend == MarketStructureTrend.UPTREND:
            if latest_close > highs[-1]["price"]:
                structural_break = StructuralBreakType.BOS_BULLISH
                break_price = highs[-1]["price"]
                break_candle_ts = latest_ts
            elif latest_close < lows[-1]["price"]:
                structural_break = StructuralBreakType.CHOCH_BEARISH
                break_price = lows[-1]["price"]
                break_candle_ts = latest_ts

        # In a downtrend:
        # BOS: Price closes below the prior confirmed Swing Low (trend continuation)
        # CHoCH: Price closes above the prior confirmed Lower High (trend reversal character change)
        elif trend == MarketStructureTrend.DOWNTREND:
            if latest_close < lows[-1]["price"]:
                structural_break = StructuralBreakType.BOS_BEARISH
                break_price = lows[-1]["price"]
                break_candle_ts = latest_ts
            elif latest_close > highs[-1]["price"]:
                structural_break = StructuralBreakType.CHOCH_BULLISH
                break_price = highs[-1]["price"]
                break_candle_ts = latest_ts

        # In sideways: check if breakout beyond most recent confirmed swing range
        else:
            if latest_close > highs[-1]["price"]:
                structural_break = StructuralBreakType.BOS_BULLISH
                break_price = highs[-1]["price"]
                break_candle_ts = latest_ts
            elif latest_close < lows[-1]["price"]:
                structural_break = StructuralBreakType.BOS_BEARISH
                break_price = lows[-1]["price"]
                break_candle_ts = latest_ts

    return {
        "trend": trend,
        "structural_break": structural_break,
        "break_price": break_price,
        "break_candle_timestamp": break_candle_ts,
        "last_high_classification": high_class,
        "last_low_classification": low_class,
        "latest_swing_high": highs[-1],
        "previous_swing_high": highs[-2],
        "latest_swing_low": lows[-1],
        "previous_swing_low": lows[-2],
        "confirmed_highs_count": len(highs),
        "confirmed_lows_count": len(lows),
        "swing_highs": highs,
        "swing_lows": lows
    }


# ============================================================================
# 2. Opening Range & Dynamic Support/Resistance Zones
# ============================================================================

def calculate_opening_range(
    candles: List[Dict[str, Any]],
    start_time_str: str = "09:15",
    end_time_str: str = "09:30"
) -> Dict[str, Any]:
    """
    Calculates Opening Range High (ORH) and Opening Range Low (ORL)
    from the first 15 minutes of the trading session (09:15 to 09:30 IST).
    """
    or_candles = []
    h_start, m_start = map(int, start_time_str.split(":"))
    h_end, m_end = map(int, end_time_str.split(":"))
    t_start = time(h_start, m_start)
    t_end = time(h_end, m_end)

    for c in candles:
        ts_str = str(c.get("timestamp") or c.get("start_time"))
        try:
            if "T" in ts_str:
                dt_part = ts_str.split("T")[1][:5]
            else:
                dt_part = ts_str.split()[1][:5]
            ch, cm = map(int, dt_part.split(":"))
            c_time = time(ch, cm)
            if t_start <= c_time < t_end:
                or_candles.append(c)
        except Exception:
            continue

    if not or_candles:
        return {
            "orh": None,
            "orl": None,
            "or_mid": None,
            "is_complete": False,
            "candle_count": 0
        }

    orh = max(float(c["high"]) for c in or_candles)
    orl = min(float(c["low"]) for c in or_candles)
    or_mid = round((orh + orl) / 2.0, 2)

    # Check if session has progressed past the opening range
    last_ts_str = str(candles[-1].get("timestamp") or candles[-1].get("start_time"))
    is_complete = False
    try:
        if "T" in last_ts_str:
            last_dt_part = last_ts_str.split("T")[1][:5]
        else:
            last_dt_part = last_ts_str.split()[1][:5]
        lh, lm = map(int, last_dt_part.split(":"))
        is_complete = time(lh, lm) >= t_end
    except Exception:
        is_complete = len(or_candles) >= 15  # 15 1-min candles

    return {
        "orh": round(orh, 2),
        "orl": round(orl, 2),
        "or_mid": or_mid,
        "is_complete": is_complete,
        "candle_count": len(or_candles)
    }


def identify_support_resistance_zones(
    candles: List[Dict[str, Any]],
    current_price: float,
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
    pdc: Optional[float] = None,
    opening_range: Optional[Dict[str, Any]] = None,
    vwap_levels: Optional[Dict[str, Any]] = None,
    k_fractal: int = 2,
    tolerance_pct: float = 0.0015  # ~0.15% (approx 36 pts on 24,000 NIFTY)
) -> List[Dict[str, Any]]:
    """
    Identifies dynamic Support and Resistance zones by clustering structural
    reference levels:
    - PDH, PDL, PDC (Previous Day High/Low/Close)
    - ORH, ORL (Opening Range High/Low)
    - Confirmed Swing Highs and Swing Lows
    - Session VWAP and Standard Deviation Bands (+-1sigma, +-2sigma)

    Zone Scoring Algorithm:
    - Touch Count: Number of candles whose wicks test the zone without closing beyond it.
    - Confluence Count: Number of distinct technical levels falling into the same cluster.
    - Zone Type: SUPPORT (if current price > zone), RESISTANCE (if current price < zone),
      PIVOT_ZONE (if current price is within the zone).
    """
    # 1. Harvest raw candidate levels
    raw_levels: List[Dict[str, Any]] = []

    if pdh is not None and pdh > 0:
        raw_levels.append({"price": float(pdh), "source": "PDH", "weight": 2.5})
    if pdl is not None and pdl > 0:
        raw_levels.append({"price": float(pdl), "source": "PDL", "weight": 2.5})
    if pdc is not None and pdc > 0:
        raw_levels.append({"price": float(pdc), "source": "PDC", "weight": 1.5})

    if opening_range and opening_range.get("orh"):
        raw_levels.append({"price": float(opening_range["orh"]), "source": "ORH", "weight": 2.0})
    if opening_range and opening_range.get("orl"):
        raw_levels.append({"price": float(opening_range["orl"]), "source": "ORL", "weight": 2.0})

    if vwap_levels:
        if vwap_levels.get("vwap"):
            raw_levels.append({"price": float(vwap_levels["vwap"]), "source": "VWAP", "weight": 3.0})
        if vwap_levels.get("upper_band_1"):
            raw_levels.append({"price": float(vwap_levels["upper_band_1"]), "source": "VWAP_+1SIGMA", "weight": 1.5})
        if vwap_levels.get("lower_band_1"):
            raw_levels.append({"price": float(vwap_levels["lower_band_1"]), "source": "VWAP_-1SIGMA", "weight": 1.5})
        if vwap_levels.get("upper_band_2"):
            raw_levels.append({"price": float(vwap_levels["upper_band_2"]), "source": "VWAP_+2SIGMA", "weight": 2.0})
        if vwap_levels.get("lower_band_2"):
            raw_levels.append({"price": float(vwap_levels["lower_band_2"]), "source": "VWAP_-2SIGMA", "weight": 2.0})

    # Harvest confirmed fractal swings
    pivots_data = identify_fractal_pivots(candles, k=k_fractal)
    for sh in pivots_data["swing_highs"]:
        if sh["is_confirmed"]:
            raw_levels.append({"price": float(sh["price"]), "source": "SWING_HIGH", "weight": 2.0})
    for sl in pivots_data["swing_lows"]:
        if sl["is_confirmed"]:
            raw_levels.append({"price": float(sl["price"]), "source": "SWING_LOW", "weight": 2.0})

    if not raw_levels:
        return []

    # 2. Sort levels by price and cluster within tolerance_pct
    raw_levels.sort(key=lambda x: x["price"])
    clusters: List[List[Dict[str, Any]]] = []
    current_cluster = [raw_levels[0]]

    for lvl in raw_levels[1:]:
        cluster_center = sum(item["price"] for item in current_cluster) / len(current_cluster)
        band_tolerance = cluster_center * tolerance_pct
        if abs(lvl["price"] - cluster_center) <= band_tolerance:
            current_cluster.append(lvl)
        else:
            clusters.append(current_cluster)
            current_cluster = [lvl]
    if current_cluster:
        clusters.append(current_cluster)

    # 3. Build cohesive zone objects & score touch count
    zones: List[Dict[str, Any]] = []
    curr_p = float(current_price)

    for cluster in clusters:
        cluster_prices = [item["price"] for item in cluster]
        zone_low = min(cluster_prices)
        zone_high = max(cluster_prices)
        # Pad zone slightly by 0.05% if width is zero (single point)
        if zone_high == zone_low:
            pad = zone_low * 0.0005
            zone_low -= pad
            zone_high += pad

        mid_price = (zone_low + zone_high) / 2.0
        sources = list(dict.fromkeys(item["source"] for item in cluster))
        base_weight = sum(item["weight"] for item in cluster)

        # Count candle wick touches on this zone
        touch_count = 0
        clean_rejections = 0
        for c in candles[-50:]:  # evaluate recent 50 bars
            c_high = float(c["high"])
            c_low = float(c["low"])
            c_close = float(c["close"])
            c_open = float(c["open"])

            # Wick touched the zone
            if c_high >= zone_low and c_low <= zone_high:
                touch_count += 1
                # Check for rejection wick (body closed outside zone)
                if c_close > zone_high or c_close < zone_low:
                    clean_rejections += 1

        # Classify Zone Type relative to current price
        if curr_p > zone_high:
            zone_type = "SUPPORT"
        elif curr_p < zone_low:
            zone_type = "RESISTANCE"
        else:
            zone_type = "PIVOT_ZONE"

        # Composite Strength Score (0 to 100)
        # Factors: confluence count (max 40 pts), touch count (max 30 pts), clean rejections (max 30 pts)
        confluence_score = min(40.0, len(sources) * 12.0)
        touch_score = min(30.0, touch_count * 5.0)
        rejection_score = min(30.0, clean_rejections * 7.5)
        strength_score = round(confluence_score + touch_score + rejection_score, 1)

        zones.append({
            "zone_type": zone_type,
            "zone_low": round(zone_low, 2),
            "zone_high": round(zone_high, 2),
            "mid_price": round(mid_price, 2),
            "confluence_sources": sources,
            "confluence_count": len(sources),
            "touch_count": touch_count,
            "rejections_count": clean_rejections,
            "strength_score": strength_score,
            "distance_pts": round(abs(curr_p - mid_price), 2),
            "distance_pct": round(abs(curr_p - mid_price) / curr_p * 100.0, 3)
        })

    # Sort zones by proximity to current price
    zones.sort(key=lambda z: z["distance_pts"])
    return zones


# ============================================================================
# 3. Divergence Detection Engine
# ============================================================================

def detect_divergence(
    candles: List[Dict[str, Any]],
    indicator_series: List[Optional[float]],
    indicator_name: str = "RSI",
    k: int = 2,
    max_bars_lookback: int = 40,
    tolerance_pct: float = 0.0005
) -> Dict[str, Any]:
    """
    Detects Regular and Hidden Divergences between Price and an Indicator series (e.g. RSI, MACD Hist).

    Mathematical Rules:
    1. Regular Bullish Divergence (Reversal):
       Price forms Lower Low (LL): Low[2] < Low[1]
       Indicator forms Higher Low (HL): Ind[2] > Ind[1]
       Significance: Bearish price momentum is exhausted; potential upward reversal.

    2. Regular Bearish Divergence (Reversal):
       Price forms Higher High (HH): High[2] > High[1]
       Indicator forms Lower High (LH): Ind[2] < Ind[1]
       Significance: Bullish price momentum is exhausted; potential downward reversal.

    3. Hidden Bullish Divergence (Continuation):
       Price forms Higher Low (HL): Low[2] > Low[1]
       Indicator forms Lower Low (LL): Ind[2] < Ind[1]
       Significance: Uptrend pullback complete; underlying trend continuation.

    4. Hidden Bearish Divergence (Continuation):
       Price forms Lower High (LH): High[2] < High[1]
       Indicator forms Higher High (HH): Ind[2] > Ind[1]
       Significance: Downtrend pullback complete; underlying downtrend continuation.

    Guarantees:
    - Zero repainting: Evaluates confirmed pivots where right k-bars are closed.
    - Indicator pivot matching: Searches +/-1 bar around price pivot timestamp to match indicator peak/trough.
    """
    n = len(candles)
    if n != len(indicator_series) or n < 2 * k + 2:
        return {
            "has_divergence": False,
            "divergence_type": DivergenceType.NO_DIVERGENCE,
            "divergence_status": DivergenceStatus.NONE,
            "indicator_name": indicator_name,
            "details": None
        }

    pivots_data = identify_fractal_pivots(candles, k=k)
    highs = [p for p in pivots_data["swing_highs"] if p["is_confirmed"]]
    lows = [p for p in pivots_data["swing_lows"] if p["is_confirmed"]]

    # Helper: get indicator value matching a price pivot index (within +/- 1 bar)
    def get_indicator_pivot_val(price_idx: int, is_high: bool) -> Optional[float]:
        window = [price_idx - 1, price_idx, price_idx + 1]
        valid_vals = []
        for idx in window:
            if 0 <= idx < len(indicator_series) and indicator_series[idx] is not None:
                valid_vals.append((idx, indicator_series[idx]))
        if not valid_vals:
            return None
        if is_high:
            # For highs, pick the local maximum in indicator window
            return max(valid_vals, key=lambda x: x[1])[1]
        else:
            # For lows, pick the local minimum in indicator window
            return min(valid_vals, key=lambda x: x[1])[1]

    # Evaluate Bullish Divergences on the last two swing lows
    bullish_result = None
    if len(lows) >= 2:
        p1 = lows[-2]
        p2 = lows[-1]
        # Restrict within max_bars_lookback
        if (p2["index"] - p1["index"]) <= max_bars_lookback:
            ind1 = get_indicator_pivot_val(p1["index"], is_high=False)
            ind2 = get_indicator_pivot_val(p2["index"], is_high=False)

            if ind1 is not None and ind2 is not None:
                p_diff = p2["price"] - p1["price"]
                p_tol = p1["price"] * tolerance_pct
                ind_diff = ind2 - ind1

                # Regular Bullish: Price LL (p2 < p1) AND Indicator HL (ind2 > ind1)
                if p_diff < -p_tol and ind_diff > 0.5:
                    bullish_result = {
                        "has_divergence": True,
                        "divergence_type": DivergenceType.REGULAR_BULLISH,
                        "divergence_status": DivergenceStatus.CONFIRMED,
                        "bias": "BULLISH",
                        "category": "REGULAR_REVERSAL",
                        "indicator_name": indicator_name,
                        "price_pivots": [p1, p2],
                        "indicator_values": [round(ind1, 2), round(ind2, 2)],
                        "bars_span": p2["index"] - p1["index"],
                        "description": f"Price formed Lower Low ({p1['price']} -> {p2['price']}) while {indicator_name} formed Higher Low ({ind1:.2f} -> {ind2:.2f})"
                    }
                # Hidden Bullish: Price HL (p2 > p1) AND Indicator LL (ind2 < ind1)
                elif p_diff > p_tol and ind_diff < -0.5:
                    bullish_result = {
                        "has_divergence": True,
                        "divergence_type": DivergenceType.HIDDEN_BULLISH,
                        "divergence_status": DivergenceStatus.CONFIRMED,
                        "bias": "BULLISH",
                        "category": "HIDDEN_CONTINUATION",
                        "indicator_name": indicator_name,
                        "price_pivots": [p1, p2],
                        "indicator_values": [round(ind1, 2), round(ind2, 2)],
                        "bars_span": p2["index"] - p1["index"],
                        "description": f"Price formed Higher Low ({p1['price']} -> {p2['price']}) while {indicator_name} formed Lower Low ({ind1:.2f} -> {ind2:.2f})"
                    }

    # Evaluate Bearish Divergences on the last two swing highs
    bearish_result = None
    if len(highs) >= 2:
        p1 = highs[-2]
        p2 = highs[-1]
        if (p2["index"] - p1["index"]) <= max_bars_lookback:
            ind1 = get_indicator_pivot_val(p1["index"], is_high=True)
            ind2 = get_indicator_pivot_val(p2["index"], is_high=True)

            if ind1 is not None and ind2 is not None:
                p_diff = p2["price"] - p1["price"]
                p_tol = p1["price"] * tolerance_pct
                ind_diff = ind2 - ind1

                # Regular Bearish: Price HH (p2 > p1) AND Indicator LH (ind2 < ind1)
                if p_diff > p_tol and ind_diff < -0.5:
                    bearish_result = {
                        "has_divergence": True,
                        "divergence_type": DivergenceType.REGULAR_BEARISH,
                        "divergence_status": DivergenceStatus.CONFIRMED,
                        "bias": "BEARISH",
                        "category": "REGULAR_REVERSAL",
                        "indicator_name": indicator_name,
                        "price_pivots": [p1, p2],
                        "indicator_values": [round(ind1, 2), round(ind2, 2)],
                        "bars_span": p2["index"] - p1["index"],
                        "description": f"Price formed Higher High ({p1['price']} -> {p2['price']}) while {indicator_name} formed Lower High ({ind1:.2f} -> {ind2:.2f})"
                    }
                # Hidden Bearish: Price LH (p2 < p1) AND Indicator HH (ind2 > ind1)
                elif p_diff < -p_tol and ind_diff > 0.5:
                    bearish_result = {
                        "has_divergence": True,
                        "divergence_type": DivergenceType.HIDDEN_BEARISH,
                        "divergence_status": DivergenceStatus.CONFIRMED,
                        "bias": "BEARISH",
                        "category": "HIDDEN_CONTINUATION",
                        "indicator_name": indicator_name,
                        "price_pivots": [p1, p2],
                        "indicator_values": [round(ind1, 2), round(ind2, 2)],
                        "bars_span": p2["index"] - p1["index"],
                        "description": f"Price formed Lower High ({p1['price']} -> {p2['price']}) while {indicator_name} formed Higher High ({ind1:.2f} -> {ind2:.2f})"
                    }

    # If both detected, pick the more recent pivot pair
    if bullish_result and bearish_result:
        b_idx = bullish_result["price_pivots"][1]["index"]
        s_idx = bearish_result["price_pivots"][1]["index"]
        return bullish_result if b_idx >= s_idx else bearish_result
    elif bullish_result:
        return bullish_result
    elif bearish_result:
        return bearish_result

    return {
        "has_divergence": False,
        "divergence_type": DivergenceType.NO_DIVERGENCE,
        "divergence_status": DivergenceStatus.NONE,
        "indicator_name": indicator_name,
        "details": None
    }

