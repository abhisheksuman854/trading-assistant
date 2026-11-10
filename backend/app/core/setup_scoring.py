"""
TradeLens AI - Phase 2C Multi-Timeframe Setup Scoring & Opportunity Engine (Hardened)
====================================================================================
Deterministic, transparent, non-repainting confluence scoring and trade qualification.

Hardened Architectural Standards:
1. Scoring Correlation Protection:
   - Evaluates 7 distinct Evidence Families with explicit ceilings within categories:
     Trend Alignment, Directional Strength, Momentum, Market Structure,
     Value Location, Volatility, Divergence and Timing.
   - Prevents correlated indicators from stacking points for the same market phenomenon.
2. Underlying Versus Option Risk/Reward:
   - Underlying structural R:R (NIFTY index points) is strictly separated from
     Option premium R:R (derivative contract rupee returns).
   - Never conflates underlying points with option payout.
   - If reliable option premium estimation is unavailable, option R:R is marked UNAVAILABLE.
3. Option Contract Selection & Master Validation:
   - Validates Underlying, Option Type (CE/PE), Exact Expiry, Strike Step,
     Lot Size, Contract Status, Bid/Ask, Quote Freshness, and Liquidity.
4. Numerical, Configurable Quote Freshness:
   - Strict intraday entry qualification: <= 15 seconds.
   - Acceptable latency: 16 - 60 seconds (with warning).
   - Absolute hard rejection boundary: > 180 seconds.
   - Uses documented Mid-Price spread reference: (Bid + Ask) / 2.0.
   - Missing timestamps, future timestamps, and invalid order books fail closed.
5. Canonical Eight-Gate Tradability Registry:
   - Standardizes the 8 mandatory gates with stable machine-readable IDs, deterministic
     evaluation, pass/fail status, rejection reasons, and source data.
6. Deterministic Structural Target Selection Hierarchy:
   - Primary: Nearest realistic intermediate opposing fractal swing pivot (5m).
   - Secondary: Macro opposing fractal swing pivot (15m).
   - Obstacle Detection: Flags/clamps intermediate obstacles (1m/3m) between entry and target.
   - Refuses to select distant VWAP bands simply to inflate R:R artificially.
7. Zero Automated Execution; Zero Fabricated Probabilities.
"""
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta, date
import math

from app.data.candle_store import MultiTimeframeCandleStore, Timeframe
from app.data.base import DataStatus
from app.data.futures_manager import FuturesContractManager
from app.data.session_clock import IndianMarketSessionClock
from app.core.indicators import TechnicalIndicatorService
from app.core.market_structure import (
    MarketStructureService,
    StructureType,
    StructuralBreakType,
    MarketRegime,
    DivergenceType,
    ConfirmationStatus,
    BreakLifecycleState,
    DivergenceInvalidationStatus,
    DEFAULT_TIMEFRAME_K
)
from app.core.options_analytics import (
    verify_option_contract_tradability,
    get_strike_step,
    calculate_black_scholes_greeks
)


# ============================================================================
# Enums and Constants
# ============================================================================

class SetupDirection:
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    CONFLICTED = "CONFLICTED"
    NEUTRAL = "NEUTRAL"


class SetupQualityTier:
    TIER_1_HIGH_QUALITY = "TIER_1_HIGH_QUALITY"  # >= 75 points
    TIER_2_MODERATE = "TIER_2_MODERATE"          # 60 - 74 points
    TIER_3_WEAK = "TIER_3_WEAK"                  # < 60 points (Unqualified)


class TradeActionRecommendation:
    QUALIFIED_SETUP = "QUALIFIED_SETUP"          # Meets confluence tier & passes all 8 gates
    NO_TRADE = "NO_TRADE"                        # Fails any gate or insufficient score


class EvidenceFamily:
    """
    Correlation-Control Groups.
    Architectural groupings designed to constrain correlated indicator scoring within categories.
    Note: These are correlation-control partitions, not statistically independent variables.
    """
    TREND_ALIGNMENT = "TREND_ALIGNMENT"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    DIRECTIONAL_STRENGTH = "DIRECTIONAL_STRENGTH"
    MOMENTUM = "MOMENTUM"
    VALUE_LOCATION = "VALUE_LOCATION"
    VOLATILITY = "VOLATILITY"
    DIVERGENCE = "DIVERGENCE"
    EXECUTION_TIMING = "EXECUTION_TIMING"
    # Backward compatibility alias
    DIVERGENCE_TIMING = "DIVERGENCE"


class TargetType:
    """Structural target classification types."""
    STRUCTURAL_TARGET_CONFIRMED = "STRUCTURAL_TARGET_CONFIRMED"
    MODELLED_EXPANSION_TARGET = "MODELLED_EXPANSION_TARGET"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"


class SqueezeLifecycleState:
    """John Carter's Volatility Squeeze lifecycle states."""
    ACTIVE_COMPRESSION = "ACTIVE_COMPRESSION"
    CONFIRMED_RELEASE = "CONFIRMED_RELEASE"
    RELEASE_EXPIRED = "RELEASE_EXPIRED"
    NO_SQUEEZE = "NO_SQUEEZE"
    UNKNOWN = "UNKNOWN"


class CanonicalGateID:
    """Canonical Eight-Gate Registry IDs."""
    GATE_1_SQUEEZE_COMPRESSION = "GATE_1_SQUEEZE_COMPRESSION"
    GATE_2_CONFLICTING_MTF_REGIMES = "GATE_2_CONFLICTING_MTF_REGIMES"
    GATE_3_HIGH_MUTUAL_CONFLUENCE = "GATE_3_HIGH_MUTUAL_CONFLUENCE"
    GATE_4_FEED_GAP_OR_UNRELIABLE_DATA = "GATE_4_FEED_GAP_OR_UNRELIABLE_DATA"
    GATE_5_UNFAVORABLE_RISK_REWARD = "GATE_5_UNFAVORABLE_RISK_REWARD"
    GATE_6_UNTRADABLE_OPTION_CONTRACT = "GATE_6_UNTRADABLE_OPTION_CONTRACT"
    GATE_7_INVALID_STRUCTURAL_LEVELS = "GATE_7_INVALID_STRUCTURAL_LEVELS"
    GATE_8_DEVELOPING_UNCONFIRMED_BAR = "GATE_8_DEVELOPING_UNCONFIRMED_BAR"


class GateFailureReason:
    """Backward compatibility alias class for Gate Failure Reasons."""
    SQUEEZE_COMPRESSION = "SQUEEZE_COMPRESSION"
    CONFLICTING_MTF_REGIMES = "CONFLICTING_MTF_REGIMES"
    HIGH_MUTUAL_CONFLUENCE = "HIGH_MUTUAL_CONFLUENCE"
    FEED_GAP_OR_UNRELIABLE_DATA = "FEED_GAP_OR_UNRELIABLE_DATA"
    UNFAVORABLE_RISK_REWARD = "UNFAVORABLE_RISK_REWARD"
    UNTRADABLE_OPTION_CONTRACT = "UNTRADABLE_OPTION_CONTRACT"
    INVALID_STRUCTURAL_LEVELS = "INVALID_STRUCTURAL_LEVELS"
    DEVELOPING_UNCONFIRMED_BAR = "DEVELOPING_UNCONFIRMED_BAR"


class QuoteFreshnessThresholds:
    """
    Authoritative Numerical Quote Freshness Standards for Indian Intraday Options.
    Single unambiguous qualification boundary: <= 60.0 seconds.
    """
    STRICT_INTRADAY_MAX_SECONDS = 15.0       # Required for FRESH_LIVE_QUOTE status
    MAX_QUALIFICATION_LATENCY_SECONDS = 60.0  # Authoritative tradability qualification boundary (above = STALE_REJECT)
    ACCEPTABLE_LATENCY_MAX_SECONDS = 60.0    # Backward-compatible alias
    DIAGNOSTIC_HARD_STALE_SECONDS = 180.0    # Diagnostic tier for extreme staleness (STALE_HARD_REJECT)
    ABSOLUTE_MAX_STALE_SECONDS = 180.0      # Backward-compatible alias
    ALLOWED_CLOCK_SKEW_SECONDS = 2.0         # Max forward timestamp allowance for exchange clock skew


def get_evidence_family_registry() -> Dict[str, Dict[str, Any]]:
    """
    Returns the complete registry of the 8 Evidence Families, their correlation-control role,
    and their per-category ceilings.
    """
    return {
        EvidenceFamily.TREND_ALIGNMENT: {
            "name": "Trend Alignment",
            "description": "Multi-timeframe moving averages and directional trend filters",
            "category_assignments": {
                "category_1": {"role": "Macro 15m/5m Regimes & EMA Stack", "ceiling": 15.0},
                "category_2": {"role": "Intermediate 3m/1m EMA Stack", "ceiling": 8.0}
            }
        },
        EvidenceFamily.MARKET_STRUCTURE: {
            "name": "Market Structure",
            "description": "Fractal swing highs/lows, BOS, and CHoCH structural state",
            "category_assignments": {
                "category_1": {"role": "Macro 15m/5m Confirmed Swings", "ceiling": 15.0},
                "category_2": {"role": "Intermediate 3m/1m BOS/CHoCH", "ceiling": 12.0}
            }
        },
        EvidenceFamily.DIRECTIONAL_STRENGTH: {
            "name": "Directional Strength",
            "description": "Wilder's ADX and Directional Movement spread (+DI / -DI)",
            "category_assignments": {
                "category_2": {"role": "ADX >= 25 & DI Spread", "ceiling": 5.0}
            }
        },
        EvidenceFamily.MOMENTUM: {
            "name": "Momentum",
            "description": "Oscillator momentum, RSI zones, and MACD histogram expansion",
            "category_assignments": {
                "category_2": {"role": "RSI(14) 1m/5m & MACD Histogram", "ceiling": 5.0}
            }
        },
        EvidenceFamily.VALUE_LOCATION: {
            "name": "Value Location",
            "description": "Session VWAP proximity and intraday value area tests",
            "category_assignments": {
                "category_3": {"role": "Session VWAP relative position", "ceiling": 10.0}
            }
        },
        EvidenceFamily.VOLATILITY: {
            "name": "Volatility",
            "description": "Bollinger Bands width, ATR, and mean-reversion bounds",
            "category_assignments": {
                "category_3": {"role": "Bollinger Bands boundary tests", "ceiling": 10.0}
            }
        },
        EvidenceFamily.DIVERGENCE: {
            "name": "Divergence",
            "description": "Multi-oscillator price-to-indicator regular/hidden divergence catalysts",
            "category_assignments": {
                "category_4": {"role": "Confirmed RSI/MACD Divergence Catalyst", "ceiling": 15.0}
            }
        },
        EvidenceFamily.EXECUTION_TIMING: {
            "name": "Execution Timing",
            "description": "15-second microstructure alignment for entry execution trigger",
            "category_assignments": {
                "category_4": {"role": "15s micro structure trigger", "ceiling": 5.0}
            }
        }
    }


# Scoring Thresholds
MIN_QUALIFIED_SCORE = 60.0
HIGH_QUALITY_SCORE = 75.0
MIN_RISK_REWARD = 1.50


# ============================================================================
# 1. Scoring Correlation Protection: Evidence-Family Scoring Engine
# ============================================================================

def score_directional_confluence(
    direction: str,
    mtf_suite: Dict[str, Any],
    indicators_by_tf: Dict[str, Any],
    cmp_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Computes an independent 100-point confluence score for a single direction (BULLISH or BEARISH).
    Categorizes evidence into 7 distinct Evidence Families within 4 orthogonal categories
    to prevent correlated indicators from stacking full scores for the same market condition.

    Category Structure & Family Ceilings:
      Category 1: Macro Context & Trend (Ceiling: 30 pts)
        - Family Trend Alignment: Max 15 pts
        - Family Market Structure: Max 15 pts
      Category 2: Intermediate Structure & Momentum (Ceiling: 30 pts)
        - Family Market Structure: Max 12 pts
        - Family Trend Alignment: Max 8 pts
        - Family Directional Strength: Max 5 pts
        - Family Momentum: Max 5 pts
      Category 3: Volatility & Value Location (Ceiling: 20 pts)
        - Family Value Location: Max 10 pts
        - Family Volatility: Max 10 pts
      Category 4: Divergence Catalyst & 15s Timing (Ceiling: 20 pts)
        - Family Divergence: Max 15 pts
        - Family Timing: Max 5 pts
    """
    is_bull = (direction == SetupDirection.BULLISH)
    supporting_factors: List[str] = []
    opposing_factors: List[str] = []

    tfa = mtf_suite.get("timeframe_analyses", {})

    # -------------------------------------------------------------------------
    # Category 1: Macro Context & Trend (Ceiling: 30 pts)
    # -------------------------------------------------------------------------
    cat1_families: Dict[str, Dict[str, float]] = {
        EvidenceFamily.TREND_ALIGNMENT: {"raw": 0.0, "ceiling": 15.0},
        EvidenceFamily.MARKET_STRUCTURE: {"raw": 0.0, "ceiling": 15.0}
    }

    data_15m = tfa.get(Timeframe.M15, {})
    data_5m = tfa.get(Timeframe.M5, {})

    reg_15m = data_15m.get("regime", {}).get("regime", MarketRegime.INSUFFICIENT_DATA)
    reg_5m = data_5m.get("regime", {}).get("regime", MarketRegime.INSUFFICIENT_DATA)
    struct_15m = data_15m.get("structure", {}).get("structure_state", StructureType.INSUFFICIENT_DATA)
    struct_5m = data_5m.get("structure", {}).get("structure_state", StructureType.INSUFFICIENT_DATA)

    target_regime = MarketRegime.TRENDING_BULLISH if is_bull else MarketRegime.TRENDING_BEARISH
    opp_regime = MarketRegime.TRENDING_BEARISH if is_bull else MarketRegime.TRENDING_BULLISH
    target_struct = StructureType.BULLISH if is_bull else StructureType.BEARISH
    opp_struct = StructureType.BEARISH if is_bull else StructureType.BULLISH

    # Family: Trend Alignment (15m + 5m Regimes, Raw up to +20, Capped at 15)
    raw_trend_align = 0.0
    if reg_15m == target_regime:
        raw_trend_align += 10.0
        supporting_factors.append(f"15m Macro Regime is confirmed {target_regime}.")
    elif reg_15m == opp_regime:
        opposing_factors.append(f"15m Macro Regime directly opposes direction: {reg_15m} (-10 penalty).")
        raw_trend_align -= 10.0

    if reg_5m == target_regime:
        raw_trend_align += 10.0
        supporting_factors.append(f"5m Macro Regime is confirmed {target_regime}.")
    elif reg_5m == opp_regime:
        opposing_factors.append(f"5m Macro Regime directly opposes direction: {reg_5m} (-10 penalty).")
        raw_trend_align -= 10.0

    cat1_families[EvidenceFamily.TREND_ALIGNMENT]["raw"] = raw_trend_align

    # Family: Market Structure (Macro Swings on 15m/5m, Raw up to +15, Capped at 15)
    raw_macro_struct = 0.0
    if struct_15m == target_struct and struct_5m == target_struct:
        raw_macro_struct += 15.0
        supporting_factors.append(f"Macro swing structure confirmed {target_struct} on both 15m and 5m.")
    elif struct_15m == target_struct or struct_5m == target_struct:
        raw_macro_struct += 10.0
        supporting_factors.append(f"Macro swing structure confirmed {target_struct} on 15m/5m.")
    elif struct_15m == opp_struct and struct_5m == opp_struct:
        opposing_factors.append(f"Both 15m and 5m structures are in opposing state: {opp_struct}.")

    cat1_families[EvidenceFamily.MARKET_STRUCTURE]["raw"] = raw_macro_struct

    # Compute Category 1 Score with Family Caps
    cat1_score = 0.0
    for fam_name, fam_data in cat1_families.items():
        capped_val = max(0.0, min(fam_data["ceiling"], fam_data["raw"]))
        fam_data["capped"] = capped_val
        cat1_score += capped_val
    cat1_score = min(30.0, cat1_score)

    # -------------------------------------------------------------------------
    # Category 2: Intermediate Structure & Momentum (Ceiling: 30 pts)
    # -------------------------------------------------------------------------
    cat2_families: Dict[str, Dict[str, float]] = {
        EvidenceFamily.MARKET_STRUCTURE: {"raw": 0.0, "ceiling": 12.0},
        EvidenceFamily.TREND_ALIGNMENT: {"raw": 0.0, "ceiling": 8.0},
        EvidenceFamily.DIRECTIONAL_STRENGTH: {"raw": 0.0, "ceiling": 5.0},
        EvidenceFamily.MOMENTUM: {"raw": 0.0, "ceiling": 5.0}
    }

    data_3m = tfa.get(Timeframe.M3, {})
    data_1m = tfa.get(Timeframe.M1, {})

    break_3m = data_3m.get("structural_break", {}).get("event_type", StructuralBreakType.NONE)
    break_1m = data_1m.get("structural_break", {}).get("event_type", StructuralBreakType.NONE)

    target_breaks = (StructuralBreakType.BOS_BULLISH, StructuralBreakType.CHOCH_BULLISH) if is_bull else (StructuralBreakType.BOS_BEARISH, StructuralBreakType.CHOCH_BEARISH)
    opp_breaks = (StructuralBreakType.BOS_BEARISH, StructuralBreakType.CHOCH_BEARISH) if is_bull else (StructuralBreakType.BOS_BULLISH, StructuralBreakType.CHOCH_BULLISH)

    # Family: Market Structure (Intermediate Breaks, Cap 12)
    if break_3m in target_breaks or break_1m in target_breaks:
        b_evt = break_3m if break_3m in target_breaks else break_1m
        cat2_families[EvidenceFamily.MARKET_STRUCTURE]["raw"] = 12.0
        supporting_factors.append(f"Confirmed intermediate structural break: {b_evt}.")
    elif break_3m in opp_breaks or break_1m in opp_breaks:
        opposing_factors.append(f"Opposing structural break detected on 3m/1m: {break_3m or break_1m}.")

    # Family: Trend Alignment (1m/3m EMA Stack, Cap 8)
    ind_1m = indicators_by_tf.get(Timeframe.M1, {})
    ind_3m = indicators_by_tf.get(Timeframe.M3, {})
    ind_5m = indicators_by_tf.get(Timeframe.M5, {})

    ma_1m = ind_1m.get("moving_averages", {}).get("alignment", "MIXED")
    ma_3m = ind_3m.get("moving_averages", {}).get("alignment", "MIXED")
    target_ma = "BULLISH_STACK" if is_bull else "BEARISH_STACK"
    opp_ma = "BEARISH_STACK" if is_bull else "BULLISH_STACK"

    if ma_1m == target_ma or ma_3m == target_ma:
        cat2_families[EvidenceFamily.TREND_ALIGNMENT]["raw"] = 8.0
        supporting_factors.append(f"Moving Average Stack is {target_ma} on 1m/3m.")
    elif ma_1m == opp_ma and ma_3m == opp_ma:
        opposing_factors.append(f"Moving averages on 1m and 3m are in opposing stack: {opp_ma}.")

    # Family: Directional Strength (ADX >= 25 & DI separation, Cap 5)
    trend_1m = ind_1m.get("trend_strength", {})
    adx_1m = trend_1m.get("adx")
    p_di_1m = trend_1m.get("plus_di")
    m_di_1m = trend_1m.get("minus_di")

    if adx_1m is not None and adx_1m >= 25.0:
        if is_bull and p_di_1m is not None and m_di_1m is not None and p_di_1m > m_di_1m:
            cat2_families[EvidenceFamily.DIRECTIONAL_STRENGTH]["raw"] = 5.0
            supporting_factors.append(f"Strong Bullish ADX ({adx_1m:.1f} >= 25.0) with +DI > -DI.")
        elif not is_bull and p_di_1m is not None and m_di_1m is not None and m_di_1m > p_di_1m:
            cat2_families[EvidenceFamily.DIRECTIONAL_STRENGTH]["raw"] = 5.0
            supporting_factors.append(f"Strong Bearish ADX ({adx_1m:.1f} >= 25.0) with -DI > +DI.")
        else:
            opposing_factors.append(f"ADX = {adx_1m:.1f} is strong but DI directional spread opposes setup.")

    # Family: Momentum (RSI in healthy non-exhaustion zone, Cap 5)
    mom_1m = ind_1m.get("momentum", {})
    rsi_obj = mom_1m.get("rsi")
    rsi_1m = rsi_obj.get("value") if isinstance(rsi_obj, dict) else rsi_obj

    if rsi_1m is not None:
        if is_bull:
            if 50.0 <= rsi_1m <= 72.0:
                cat2_families[EvidenceFamily.MOMENTUM]["raw"] = 5.0
                supporting_factors.append(f"RSI(14) = {rsi_1m:.1f} in healthy bullish expansion zone (50-72).")
            elif rsi_1m > 75.0:
                opposing_factors.append(f"RSI(14) = {rsi_1m:.1f} is overbought (> 75). Risk of mean reversion.")
            elif rsi_1m < 45.0:
                opposing_factors.append(f"RSI(14) = {rsi_1m:.1f} is below 45 (bearish momentum dominance).")
        else:
            if 28.0 <= rsi_1m <= 50.0:
                cat2_families[EvidenceFamily.MOMENTUM]["raw"] = 5.0
                supporting_factors.append(f"RSI(14) = {rsi_1m:.1f} in healthy bearish expansion zone (28-50).")
            elif rsi_1m < 25.0:
                opposing_factors.append(f"RSI(14) = {rsi_1m:.1f} is oversold (< 25). Risk of snapback rally.")
            elif rsi_1m > 55.0:
                opposing_factors.append(f"RSI(14) = {rsi_1m:.1f} is above 55 (bullish momentum dominance).")

    # Compute Category 2 Score with Family Caps
    cat2_score = 0.0
    for fam_name, fam_data in cat2_families.items():
        capped_val = max(0.0, min(fam_data["ceiling"], fam_data["raw"]))
        fam_data["capped"] = capped_val
        cat2_score += capped_val
    cat2_score = min(30.0, cat2_score)

    # -------------------------------------------------------------------------
    # Category 3: Volatility & Value Location (Ceiling: 20 pts)
    # -------------------------------------------------------------------------
    cat3_families: Dict[str, Dict[str, float]] = {
        EvidenceFamily.VALUE_LOCATION: {"raw": 0.0, "ceiling": 10.0},
        EvidenceFamily.VOLATILITY: {"raw": 0.0, "ceiling": 10.0}
    }

    vwap_data = ind_5m.get("vwap", {})
    vwap_val = vwap_data.get("vwap")
    vol_5m = ind_5m.get("volatility", {})
    atr_5m = vol_5m.get("atr", 10.0)

    # Family: Value Location (Session VWAP, Cap 10)
    if vwap_val is not None and cmp_price is not None:
        if is_bull:
            if cmp_price >= vwap_val:
                cat3_families[EvidenceFamily.VALUE_LOCATION]["raw"] = 10.0
                supporting_factors.append(f"Price (₹{cmp_price:.2f}) trades above Session VWAP (₹{vwap_val:.2f}).")
            elif (vwap_val - cmp_price) <= (0.5 * atr_5m):
                cat3_families[EvidenceFamily.VALUE_LOCATION]["raw"] = 5.0
                supporting_factors.append(f"Price is within 0.5 ATR of VWAP (value area test).")
            else:
                opposing_factors.append(f"Price is below VWAP by > 0.5 ATR (bearish intraday flow).")
        else:
            if cmp_price <= vwap_val:
                cat3_families[EvidenceFamily.VALUE_LOCATION]["raw"] = 10.0
                supporting_factors.append(f"Price (₹{cmp_price:.2f}) trades below Session VWAP (₹{vwap_val:.2f}).")
            elif (cmp_price - vwap_val) <= (0.5 * atr_5m):
                cat3_families[EvidenceFamily.VALUE_LOCATION]["raw"] = 5.0
                supporting_factors.append(f"Price is within 0.5 ATR of VWAP (resistance test).")
            else:
                opposing_factors.append(f"Price is above VWAP by > 0.5 ATR (bullish intraday flow).")

    # Family: Volatility (Squeeze Release / Expansion, Cap 10)
    sqz_3m = ind_3m.get("volatility", {}).get("squeeze", {})
    is_sqz_release = sqz_3m.get("is_squeeze_release", False)
    is_sqz_on = sqz_3m.get("is_squeeze_on", False)

    if is_sqz_release:
        cat3_families[EvidenceFamily.VOLATILITY]["raw"] = 10.0
        supporting_factors.append("Recent Squeeze Release detected on 3m: volatility expanding.")
    elif not is_sqz_on:
        cat3_families[EvidenceFamily.VOLATILITY]["raw"] = 5.0
        supporting_factors.append("Normal volatility expansion (no compression constraint).")
    else:
        opposing_factors.append("Squeeze ON: Volatility is compressing inside bands.")

    # Compute Category 3 Score with Family Caps
    cat3_score = 0.0
    for fam_name, fam_data in cat3_families.items():
        capped_val = max(0.0, min(fam_data["ceiling"], fam_data["raw"]))
        fam_data["capped"] = capped_val
        cat3_score += capped_val
    cat3_score = min(20.0, cat3_score)

    # -------------------------------------------------------------------------
    # Category 4: Divergence Catalyst & 15s Timing (Ceiling: 20 pts)
    # -------------------------------------------------------------------------
    cat4_families: Dict[str, Dict[str, float]] = {
        EvidenceFamily.DIVERGENCE: {"raw": 0.0, "ceiling": 15.0},
        EvidenceFamily.EXECUTION_TIMING: {"raw": 0.0, "ceiling": 5.0}
    }

    divs_1m = data_1m.get("divergences", {})
    rsi_div_1m = divs_1m.get("rsi_divergence", {})
    macd_div_1m = divs_1m.get("macd_divergence", {})

    target_divs = (DivergenceType.REGULAR_BULLISH, DivergenceType.HIDDEN_BULLISH) if is_bull else (DivergenceType.REGULAR_BEARISH, DivergenceType.HIDDEN_BEARISH)
    opp_divs = (DivergenceType.REGULAR_BEARISH, DivergenceType.HIDDEN_BEARISH) if is_bull else (DivergenceType.REGULAR_BULLISH, DivergenceType.HIDDEN_BULLISH)

    raw_div_score = 0.0
    has_confirmed_div = False
    div_name = ""
    inv_status = ""

    for div_obj, name in [(rsi_div_1m, "RSI"), (macd_div_1m, "MACD")]:
        d_type = div_obj.get("divergence_type")
        if d_type in target_divs and div_obj.get("has_divergence", False):
            has_confirmed_div = True
            div_name = f"{d_type} on {name}"
            inv_status = div_obj.get("invalidation_status", "")
            break
        elif d_type in opp_divs and div_obj.get("has_divergence", False):
            opposing_factors.append(f"Opposing {d_type} on {name} is active.")

    if has_confirmed_div:
        raw_div_score += 12.0
        supporting_factors.append(f"Confirmed Divergence catalyst: {div_name}.")
        if inv_status == DivergenceInvalidationStatus.ACTIVE_UNTOUCHED:
            raw_div_score += 3.0
            supporting_factors.append(f"Divergence invalidation level is untouched.")
        elif inv_status == DivergenceInvalidationStatus.ACTIVE_WICK_PROBED:
            raw_div_score += 1.0
            supporting_factors.append(f"Divergence wick probed level and held.")

    # 15s Microstructure Execution Timing (+5 pts)
    raw_timing_score = 0.0
    data_15s = tfa.get(Timeframe.S15, {})
    struct_15s = data_15s.get("structure", {}).get("structure_state", StructureType.INSUFFICIENT_DATA)
    if struct_15s == target_struct:
        raw_timing_score += 5.0
        supporting_factors.append(f"15s execution timeframe structure aligned: {target_struct}.")

    cat4_families[EvidenceFamily.DIVERGENCE]["raw"] = raw_div_score
    cat4_families[EvidenceFamily.DIVERGENCE]["capped"] = min(15.0, max(0.0, raw_div_score))

    cat4_families[EvidenceFamily.EXECUTION_TIMING]["raw"] = raw_timing_score
    cat4_families[EvidenceFamily.EXECUTION_TIMING]["capped"] = min(5.0, max(0.0, raw_timing_score))

    # Backward compatibility alias
    cat4_families["DIVERGENCE_TIMING"] = {
        "raw": raw_div_score + raw_timing_score,
        "ceiling": 20.0,
        "capped": min(20.0, cat4_families[EvidenceFamily.DIVERGENCE]["capped"] + cat4_families[EvidenceFamily.EXECUTION_TIMING]["capped"])
    }

    cat4_score = min(20.0, cat4_families[EvidenceFamily.DIVERGENCE]["capped"] + cat4_families[EvidenceFamily.EXECUTION_TIMING]["capped"])

    # Total Score Calculation
    total_score = round(cat1_score + cat2_score + cat3_score + cat4_score, 1)

    # Determine Quality Tier
    if total_score >= HIGH_QUALITY_SCORE:
        tier = SetupQualityTier.TIER_1_HIGH_QUALITY
    elif total_score >= MIN_QUALIFIED_SCORE:
        tier = SetupQualityTier.TIER_2_MODERATE
    else:
        tier = SetupQualityTier.TIER_3_WEAK

    return {
        "direction": direction,
        "total_score": total_score,
        "quality_tier": tier,
        "is_qualified": (total_score >= MIN_QUALIFIED_SCORE),
        "category_scores": {
            "macro_context_trend": round(cat1_score, 1),
            "intermediate_structure_momentum": round(cat2_score, 1),
            "volatility_value_location": round(cat3_score, 1),
            "divergence_timing": round(cat4_score, 1)
        },
        "evidence_family_breakdown": {
            "category_1": cat1_families,
            "category_2": cat2_families,
            "category_3": cat3_families,
            "category_4": cat4_families
        },
        "supporting_factors": supporting_factors,
        "opposing_factors": opposing_factors
    }


# ============================================================================
# 2. Option Contract Selection & Quote Freshness Validation
# ============================================================================

def validate_option_contract_against_master(
    contract_info: Dict[str, Any],
    underlying_symbol: str = "NIFTY 50",
    expected_option_type: Optional[str] = None,
    expected_expiry_date: Optional[Any] = None,
    current_dt: Optional[datetime] = None,
    contract_master: Optional[Dict[str, Any]] = None,
    strict_freshness_seconds: float = QuoteFreshnessThresholds.STRICT_INTRADAY_MAX_SECONDS,
    max_qualification_seconds: float = QuoteFreshnessThresholds.MAX_QUALIFICATION_LATENCY_SECONDS,
    max_spread_pct: float = 4.0,
    min_oi: int = 50000,
    min_volume: int = 5000
) -> Dict[str, Any]:
    """
    Validates option contract against exchange contract master and authoritative quote freshness standards:
    1. Underlying mapping & symbol format.
    2. Option Type (CE/PE) matching setup direction.
    3. Authoritative Expiry date resolution from exchange contract master (local calendar is secondary check).
    4. Valid strike interval step size.
    5. Lot size verification against exchange master specifications (NIFTY: 65, BANKNIFTY: 30).
    6. Broker instrument token presence.
    7. Orderbook integrity (bid > 0, ask > 0, bid <= ask).
    8. Authoritative quote freshness standards:
         - <= 15.0s: FRESH_LIVE_QUOTE (pass)
         - > 15.0s and <= 60.0s: ACCEPTABLE_LATENCY (pass with latency warning)
         - > 60.0s: STALE_REJECT (fails closed at qualification cutoff)
         - > 180.0s: STALE_HARD_REJECT (diagnostic classification, fails closed)
         - Missing or future timestamp (>2.0s skew): fails closed
    9. Bid-ask spread relative to Mid-Price reference: (Bid + Ask) / 2.0.
    10. Liquidity gates (OI >= 50,000, Volume >= 5,000).
    """
    rejection_reasons: List[str] = []
    warnings: List[str] = []

    sym = str(contract_info.get("symbol", "")).strip().upper()
    opt_type = str(contract_info.get("option_type", "")).strip().upper()
    strike = contract_info.get("strike")
    ltp = contract_info.get("ltp")
    bid = contract_info.get("bid")
    ask = contract_info.get("ask")
    oi = contract_info.get("oi")
    volume = contract_info.get("volume")
    last_trade_time = contract_info.get("last_trade_time") or contract_info.get("timestamp")
    status = str(contract_info.get("status", "ACTIVE")).strip().upper()
    c_expiry = contract_info.get("expiry_date") or contract_info.get("expiry")
    c_lot = contract_info.get("lot_size")
    c_token = contract_info.get("instrument_token") or contract_info.get("token") or contract_info.get("instrument_id")
    c_und = contract_info.get("underlying") or contract_info.get("underlying_symbol")

    now_ist = current_dt or IndianMarketSessionClock.now_ist()
    if now_ist.tzinfo is None:
        now_ist = now_ist.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))

    # 1. Parse Contract Quote Expiry Date
    parsed_c_expiry = None
    if c_expiry:
        try:
            if isinstance(c_expiry, str):
                c_expiry_clean = c_expiry[:10]
                parsed_c_expiry = datetime.strptime(c_expiry_clean, "%Y-%m-%d").date()
            elif isinstance(c_expiry, datetime):
                parsed_c_expiry = c_expiry.date()
            elif isinstance(c_expiry, date):
                parsed_c_expiry = c_expiry
        except Exception:
            rejection_reasons.append(f"Unparseable contract expiry date format: '{c_expiry}'.")

    parsed_exp_expiry = None
    if expected_expiry_date is not None:
        try:
            if isinstance(expected_expiry_date, str):
                parsed_exp_expiry = datetime.strptime(expected_expiry_date[:10], "%Y-%m-%d").date()
            elif isinstance(expected_expiry_date, datetime):
                parsed_exp_expiry = expected_expiry_date.date()
            elif isinstance(expected_expiry_date, date):
                parsed_exp_expiry = expected_expiry_date
        except Exception:
            parsed_exp_expiry = None

    # 2. Exchange Contract Master Resolution (Primary Authority)
    master_dict = contract_master if contract_master is not None else contract_info.get("contract_master")
    master_record = None
    master_validation_status = "UNVALIDATED_STANDALONE"
    is_contract_master_verified = False
    authoritative_expiry = None
    authoritative_lot = None

    if master_dict is not None:
        token_key = str(c_token).strip() if c_token else None
        sym_key = sym
        master_record = (master_dict.get(token_key) if token_key else None) or master_dict.get(sym_key)
        if not master_record:
            rejection_reasons.append(f"Instrument '{sym}' (Token: {c_token}) not found in exchange contract master.")
            master_validation_status = "NOT_FOUND_IN_CONTRACT_MASTER"
        else:
            master_validation_status = "CONTRACT_MASTER_EVALUATED"

            # Master Status Check
            m_status = str(master_record.get("status", "ACTIVE")).strip().upper()
            if m_status not in ("ACTIVE", "TRADABLE"):
                rejection_reasons.append(f"Contract master status is {m_status}; contract is not tradable.")

            # Master Underlying Symbol Check
            m_und = master_record.get("underlying") or master_record.get("name")
            if m_und:
                norm_m_und = str(m_und).strip().upper()
                norm_req = underlying_symbol.strip().upper()
                valid_unds = {norm_req, "NIFTY" if norm_req in ("NIFTY 50", "NIFTY") else norm_req}
                if norm_m_und not in valid_unds:
                    rejection_reasons.append(f"Contract master underlying '{m_und}' does not match requested '{underlying_symbol}'.")

            # Master Option Type Check
            m_type = str(master_record.get("option_type") or master_record.get("instrument_type") or "").strip().upper()
            if m_type:
                norm_m_type = "CE" if m_type.endswith("CE") else ("PE" if m_type.endswith("PE") else m_type)
                if expected_option_type and norm_m_type != expected_option_type.upper():
                    rejection_reasons.append(f"Contract master option type '{norm_m_type}' does not match expected '{expected_option_type}'.")

            # Master Strike Check
            m_strike = master_record.get("strike")
            if m_strike is not None and strike is not None and float(m_strike) != float(strike):
                rejection_reasons.append(f"Strike mismatch: quote strike ₹{strike} does not match master strike ₹{m_strike}.")

            # Master Lot Size (Authoritative - local config must not override)
            m_lot_val = master_record.get("lotsize") or master_record.get("lot_size")
            if m_lot_val is not None:
                authoritative_lot = int(m_lot_val)
                local_conf_lot = FuturesContractManager.SUPPORTED_INDICES.get(underlying_symbol.strip().upper(), {}).get("lot_size")
                if local_conf_lot is not None and int(local_conf_lot) != authoritative_lot:
                    warnings.append(
                        f"LOCAL_CONFIGURATION_OVERRIDDEN: Local config lot size ({local_conf_lot}) "
                        f"overridden by authoritative contract master ({authoritative_lot})."
                    )

            # Master Expiry Date (Authoritative)
            m_exp_raw = master_record.get("expiry") or master_record.get("expiry_date")
            if m_exp_raw:
                try:
                    if isinstance(m_exp_raw, str):
                        if len(m_exp_raw) >= 10 and m_exp_raw[4] == "-":
                            authoritative_expiry = date.fromisoformat(m_exp_raw[:10])
                        else:
                            authoritative_expiry = datetime.strptime(m_exp_raw[:11], "%d-%b-%Y").date()
                    elif isinstance(m_exp_raw, (date, datetime)):
                        authoritative_expiry = m_exp_raw.date() if isinstance(m_exp_raw, datetime) else m_exp_raw
                except Exception:
                    rejection_reasons.append(f"Unparseable contract master expiry date format: '{m_exp_raw}'.")

            if authoritative_expiry:
                if authoritative_expiry < now_ist.date():
                    rejection_reasons.append(f"Contract expired in exchange contract master on {authoritative_expiry.isoformat()}.")

                # Secondary consistency check: Calendar vs Master
                if parsed_exp_expiry and parsed_exp_expiry != authoritative_expiry:
                    warnings.append(
                        f"CALENDAR_MASTER_DISAGREEMENT: Local calendar calculated {parsed_exp_expiry.isoformat()}, "
                        f"but authoritative exchange contract master specifies {authoritative_expiry.isoformat()}. Master governs."
                    )

                # Quote expiry vs Master expiry
                if parsed_c_expiry and parsed_c_expiry != authoritative_expiry:
                    rejection_reasons.append(
                        f"Exact expiry date mismatch: contract quote expiry ({parsed_c_expiry.isoformat()}) "
                        f"does not match authoritative master expiry ({authoritative_expiry.isoformat()})."
                    )

            # Master Token Check
            m_tok = master_record.get("token") or master_record.get("symboltoken") or master_record.get("instrument_token")
            if m_tok and c_token and str(m_tok).strip() != str(c_token).strip():
                rejection_reasons.append(f"Instrument token mismatch: quote token {c_token} does not match master token {m_tok}.")

    else:
        # Fallback to local reference specifications when contract master is unavailable
        master_validation_status = "UNVALIDATED_STANDALONE"
        authoritative_lot = FuturesContractManager.SUPPORTED_INDICES.get(underlying_symbol.strip().upper(), {}).get("lot_size")
        authoritative_expiry = parsed_exp_expiry

        if expected_expiry_date is not None:
            if parsed_c_expiry is None:
                rejection_reasons.append("Missing exact expiry date in option contract quote.")
            elif parsed_exp_expiry is not None and parsed_c_expiry != parsed_exp_expiry:
                rejection_reasons.append(
                    f"Exact expiry date mismatch: contract expiry ({parsed_c_expiry.isoformat()}) "
                    f"does not match expected master expiry ({parsed_exp_expiry.isoformat()})."
                )
        elif parsed_c_expiry is not None:
            if parsed_c_expiry < now_ist.date():
                rejection_reasons.append(f"Contract expired on {parsed_c_expiry.isoformat()}; expired contracts are not tradable.")

    # 3. Status and Underlying Checks for Contract Info
    if status not in ("ACTIVE", "TRADABLE", ""):
        rejection_reasons.append(f"Contract master status is {status}; contract is not tradable.")

    if c_und:
        norm_und = str(c_und).strip().upper()
        norm_req = underlying_symbol.strip().upper()
        valid_unds = {norm_req, "NIFTY" if norm_req in ("NIFTY 50", "NIFTY") else norm_req}
        if norm_und not in valid_unds:
            rejection_reasons.append(f"Underlying symbol mismatch: contract specifies '{c_und}', expected '{underlying_symbol}'.")

    # 4. Option Type Check
    if not opt_type and sym:
        opt_type = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else "")
    if opt_type not in ("CE", "PE"):
        rejection_reasons.append(f"Invalid option type '{opt_type}'. Must be strictly 'CE' or 'PE'.")
    elif expected_option_type and opt_type != expected_option_type.upper():
        rejection_reasons.append(f"Option type mismatch: contract is {opt_type}, but setup requires {expected_option_type}.")

    # 5. Strike Step Check
    if strike is not None:
        step = get_strike_step(float(strike), underlying_symbol)
        if (float(strike) % step) != 0.0:
            rejection_reasons.append(f"Strike ₹{strike} does not align with standard interval step ({step} pts).")

    # 6. Lot Size Check
    if c_lot is not None and authoritative_lot is not None and int(c_lot) != int(authoritative_lot):
        rejection_reasons.append(f"Lot size mismatch: contract specifies lot size {c_lot}, but authoritative contract master requires {authoritative_lot}.")
    elif c_lot is None and authoritative_lot is not None:
        c_lot = authoritative_lot

    # 7. Instrument Token Check
    if c_token is not None and not str(c_token).strip():
        rejection_reasons.append("Empty broker instrument identifier.")

    # 8. Price & Orderbook Check
    if ltp is None or ltp <= 0:
        rejection_reasons.append("No active trade price (LTP <= 0).")

    mid_price = None
    spread = None
    spread_pct = None

    if bid is None or ask is None:
        rejection_reasons.append("Missing bid or ask quote from broker order book.")
    elif bid <= 0 or ask <= 0:
        rejection_reasons.append(f"Zero counterparty quote in order book (Bid: ₹{bid}, Ask: ₹{ask}).")
    elif bid > ask:
        rejection_reasons.append(f"Crossed orderbook anomaly: Bid (₹{bid}) > Ask (₹{ask}). Corrupted quote feed.")
    else:
        mid_price = round((bid + ask) / 2.0, 2)
        spread = round(ask - bid, 2)
        spread_pct = round((spread / max(0.05, mid_price)) * 100.0, 2)

        if spread_pct > max_spread_pct:
            rejection_reasons.append(
                f"Excessive bid-ask spread: {spread_pct}% (₹{spread}) exceeds threshold ({max_spread_pct}%) based on Mid-Price ₹{mid_price}."
            )
        elif spread_pct > (max_spread_pct / 2.0):
            warnings.append(f"Moderate spread: {spread_pct}% based on Mid-Price ₹{mid_price}.")

    # 9. Authoritative Numerical Quote Freshness Check
    freshness_status = "UNKNOWN"
    age_seconds = None

    if last_trade_time is None:
        rejection_reasons.append("Missing quote timestamp. Feed fails closed.")
        freshness_status = "MISSING_TIMESTAMP"
    else:
        try:
            if isinstance(last_trade_time, str):
                if "T" in last_trade_time:
                    q_dt = datetime.fromisoformat(last_trade_time.replace("Z", "+00:00"))
                else:
                    q_dt = datetime.strptime(last_trade_time[:19], "%Y-%m-%d %H:%M:%S")
            elif isinstance(last_trade_time, datetime):
                q_dt = last_trade_time
            else:
                q_dt = None

            if q_dt:
                if q_dt.tzinfo is None:
                    q_dt = q_dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))

                if q_dt > (now_ist + timedelta(seconds=QuoteFreshnessThresholds.ALLOWED_CLOCK_SKEW_SECONDS)):
                    rejection_reasons.append(f"Future timestamp anomaly: quote time ({q_dt.isoformat()}) ahead of exchange clock.")
                    freshness_status = "FUTURE_TIMESTAMP_ANOMALY"
                else:
                    age_seconds = max(0.0, (now_ist - q_dt).total_seconds())

                    # Authoritative numerical quote freshness standards:
                    # <= 15.0s: FRESH_LIVE_QUOTE (pass)
                    # > 15.0s and <= 60.0s: ACCEPTABLE_LATENCY (pass with warning)
                    # > 60.0s: STALE_REJECT (fails closed at qualification cutoff)
                    # > 180.0s: STALE_HARD_REJECT (diagnostic tier, fails closed)
                    if age_seconds > QuoteFreshnessThresholds.DIAGNOSTIC_HARD_STALE_SECONDS:
                        rejection_reasons.append(f"Quote is severely stale: age {age_seconds:.1f}s exceeds hard staleness limit (180.0s).")
                        freshness_status = "STALE_HARD_REJECT"
                    elif age_seconds > QuoteFreshnessThresholds.MAX_QUALIFICATION_LATENCY_SECONDS:
                        rejection_reasons.append(f"Quote is stale: age {age_seconds:.1f}s exceeds intraday qualification limit (60.0s).")
                        freshness_status = "STALE_REJECT"
                    elif age_seconds > strict_freshness_seconds:
                        warnings.append(f"Quote age ({age_seconds:.1f}s) exceeds optimal threshold (15.0s). Latency warning.")
                        freshness_status = "ACCEPTABLE_LATENCY"
                    else:
                        freshness_status = "FRESH_LIVE_QUOTE"
            else:
                rejection_reasons.append("Corrupted quote timestamp format.")
                freshness_status = "CORRUPTED_TIMESTAMP"
        except Exception as e:
            rejection_reasons.append(f"Timestamp verification error: {str(e)}")
            freshness_status = "PARSE_ERROR"

    # 10. Liquidity Thresholds
    if oi is not None and oi < min_oi:
        rejection_reasons.append(f"Open Interest ({oi:,}) below liquidity threshold ({min_oi:,}).")
    if volume is not None and volume < min_volume:
        rejection_reasons.append(f"Daily volume ({volume:,}) below liquidity threshold ({min_volume:,}).")

    if master_record and len(rejection_reasons) == 0:
        master_validation_status = "VERIFIED_IN_CONTRACT_MASTER"
        is_contract_master_verified = True

    is_tradable = (len(rejection_reasons) == 0)

    final_expiry = authoritative_expiry or parsed_c_expiry

    return {
        "contract_symbol": sym,
        "option_type": opt_type,
        "strike": strike,
        "expiry_date": final_expiry.isoformat() if final_expiry else None,
        "lot_size": authoritative_lot or c_lot,
        "instrument_token": c_token,
        "is_tradable": is_tradable,
        "tradability_status": "TRADABLE_LIQUID" if is_tradable else "UNTRADABLE_REJECTED",
        "master_validation_status": master_validation_status,
        "is_contract_master_verified": is_contract_master_verified,
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "mid_price": mid_price,
        "bid_ask_spread": spread,
        "spread_pct": spread_pct,
        "spread_reference": "MID_PRICE",
        "freshness_status": freshness_status,
        "quote_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "open_interest": oi,
        "daily_volume": volume,
        "rejection_reasons": rejection_reasons,
        "warnings": warnings
    }


def round_to_exchange_tick(price: float, tick_size: float = 0.05, direction: str = "NEAREST") -> float:
    """
    Rounds a price to a valid exchange quotation tick increment.
    For NSE index options, standard tick size is ₹0.05.

    direction options:
      - 'NEAREST': Standard nearest-tick rounding.
      - 'DOWN' / 'FLOOR': Conservative floor to lower tick (e.g. conservative stop loss or target).
      - 'UP' / 'CEIL': Ceiling to higher tick.
    """
    if price is None:
        return 0.0
    dir_clean = str(direction).strip().upper()
    if dir_clean in ("DOWN", "FLOOR"):
        return round(math.floor((price + 1e-9) / tick_size) * tick_size, 2)
    elif dir_clean in ("UP", "CEIL"):
        return round(math.ceil((price - 1e-9) / tick_size) * tick_size, 2)
    return round(round(price / tick_size) * tick_size, 2)


def project_option_premium_change(
    delta: float,
    gamma: float,
    delta_spot: float
) -> Dict[str, Any]:
    """
    Computes theoretical option price displacement using second-order Taylor expansion:
    ΔP ≈ Delta * ΔS + 0.5 * Gamma * (ΔS)^2

    Preserves signed Delta:
      CE Delta > 0: Long Call gains when underlying rises, loses when it falls.
      PE Delta < 0: Long Put gains when underlying falls, loses when it rises.
    Positive Gamma contributes a positive second-order term relative to linear Delta projection.
    """
    if not (math.isfinite(delta) and math.isfinite(gamma) and math.isfinite(delta_spot)):
        raise ValueError("Delta, Gamma, and delta_spot must be finite numbers.")
    if gamma < 0.0:
        raise ValueError(f"Gamma ({gamma}) cannot be negative for long options.")

    first_order_delta = delta * delta_spot
    second_order_gamma = 0.5 * gamma * (delta_spot ** 2)
    delta_premium = round(first_order_delta + second_order_gamma, 4)

    return {
        "delta_premium": delta_premium,
        "first_order_delta_effect": round(first_order_delta, 4),
        "second_order_gamma_effect": round(second_order_gamma, 4),
        "signed_delta_applied": delta,
        "gamma_applied": gamma,
        "delta_spot": delta_spot,
        "model_classification": "OPTION_GREEK_APPROXIMATION",
        "formula": "ΔP ≈ Delta * ΔS + 0.5 * Gamma * (ΔS)^2",
        "disclaimer": (
            "Positive Gamma contributes a positive second-order term relative to the linear Delta-only projection. "
            "Approximation valid only for local price moves under declared assumptions. "
            "Not an execution-guaranteed option price."
        )
    }


# ============================================================================
# 3. Structural Target Selection & Separate R:R Engine
# ============================================================================

def calculate_structural_target(
    direction: str,
    entry_price: float,
    mtf_suite: Dict[str, Any],
    atr: float = 12.0
) -> Dict[str, Any]:
    """
    Deterministic Structural Target Selection Hierarchy:
    1. Primary Target (Tier 1): Nearest confirmed opposing fractal swing pivot on intermediate timeframe (5m).
    2. Secondary Target (Tier 2): Nearest confirmed opposing fractal swing pivot on macro timeframe (15m).
    3. Obstacle Detection: Flags intermediate 1m/3m swing pivots between entry and target.
       Clamps target to obstacle if within close proximity to prevent unrealistic R:R inflation.
    4. Fallback: Only when in pure price discovery (no opposing historical pivots), target is entry +/- 1.5 ATR.
    """
    is_bull = (direction == SetupDirection.BULLISH)
    tfa = mtf_suite.get("timeframe_analyses", {})

    struct_5m = tfa.get(Timeframe.M5, {}).get("structure", {})
    struct_15m = tfa.get(Timeframe.M15, {}).get("structure", {})
    struct_1m = tfa.get(Timeframe.M1, {}).get("structure", {})

    target_price: Optional[float] = None
    target_tier = "NONE"
    target_source = "NONE"
    has_obstacle = False
    obstacle_price: Optional[float] = None

    if is_bull:
        # Tier 1: Nearest confirmed 5m swing high above entry
        cand_5m = list(struct_5m.get("swing_highs", []))
        if struct_5m.get("latest_confirmed_high"):
            cand_5m.append(struct_5m["latest_confirmed_high"])
        high_5m_pivots = [
            float(p["pivot_price"]) for p in cand_5m
            if p and p.get("pivot_price") is not None and float(p["pivot_price"]) > entry_price
        ]

        # Tier 2: Nearest confirmed 15m swing high above entry
        cand_15m = list(struct_15m.get("swing_highs", []))
        if struct_15m.get("latest_confirmed_high"):
            cand_15m.append(struct_15m["latest_confirmed_high"])
        high_15m_pivots = [
            float(p["pivot_price"]) for p in cand_15m
            if p and p.get("pivot_price") is not None and float(p["pivot_price"]) > entry_price
        ]

        if high_5m_pivots:
            target_price = min(high_5m_pivots)
            target_tier = "TIER_1_INTERMEDIATE_5M"
            target_source = "5m_nearest_swing_high"
        elif high_15m_pivots:
            target_price = min(high_15m_pivots)
            target_tier = "TIER_2_MACRO_15M"
            target_source = "15m_nearest_swing_high"
        else:
            # Fallback expansion when no historical highs exist
            target_price = round(entry_price + (1.5 * atr), 2)
            target_tier = "TIER_3_FALLBACK_EXPANSION"
            target_source = "atr_1.5_expansion"

        # Check for intermediate obstacles on 1m
        obs_candidates = [
            float(p["pivot_price"]) for p in struct_1m.get("swing_highs", [])
            if entry_price < float(p["pivot_price"]) < target_price
        ]
        if obs_candidates:
            has_obstacle = True
            obstacle_price = min(obs_candidates)
            # If obstacle is within 1.0 ATR of entry, clamp target to prevent unrealistic expectations
            if (obstacle_price - entry_price) <= (1.0 * atr):
                target_price = obstacle_price
                target_tier = "CLAMPED_TO_OBSTACLE"

    else:
        # Tier 1: Nearest confirmed 5m swing low below entry
        cand_low_5m = list(struct_5m.get("swing_lows", []))
        if struct_5m.get("latest_confirmed_low"):
            cand_low_5m.append(struct_5m["latest_confirmed_low"])
        low_5m_pivots = [
            float(p["pivot_price"]) for p in cand_low_5m
            if p and p.get("pivot_price") is not None and float(p["pivot_price"]) < entry_price
        ]

        # Tier 2: Nearest confirmed 15m swing low below entry
        cand_low_15m = list(struct_15m.get("swing_lows", []))
        if struct_15m.get("latest_confirmed_low"):
            cand_low_15m.append(struct_15m["latest_confirmed_low"])
        low_15m_pivots = [
            float(p["pivot_price"]) for p in cand_low_15m
            if p and p.get("pivot_price") is not None and float(p["pivot_price"]) < entry_price
        ]

        if low_5m_pivots:
            target_price = max(low_5m_pivots)
            target_tier = "TIER_1_INTERMEDIATE_5M"
            target_source = "5m_nearest_swing_low"
        elif low_15m_pivots:
            target_price = max(low_15m_pivots)
            target_tier = "TIER_2_MACRO_15M"
            target_source = "15m_nearest_swing_low"
        else:
            target_price = round(entry_price - (1.5 * atr), 2)
            target_tier = "TIER_3_FALLBACK_EXPANSION"
            target_source = "atr_1.5_expansion"

        # Check for intermediate obstacles on 1m
        obs_candidates = [
            float(p["pivot_price"]) for p in struct_1m.get("swing_lows", [])
            if target_price < float(p["pivot_price"]) < entry_price
        ]
        if obs_candidates:
            has_obstacle = True
            obstacle_price = max(obs_candidates)
            if (entry_price - obstacle_price) <= (1.0 * atr):
                target_price = obstacle_price
                target_tier = "CLAMPED_TO_OBSTACLE"

    if target_price is None:
        target_type = TargetType.TARGET_UNAVAILABLE
        is_struct_confirmed = False
    elif target_tier in ("TIER_1_INTERMEDIATE_5M", "TIER_2_MACRO_15M", "CLAMPED_TO_OBSTACLE"):
        target_type = TargetType.STRUCTURAL_TARGET_CONFIRMED
        is_struct_confirmed = True
    elif target_tier == "TIER_3_FALLBACK_EXPANSION":
        target_type = TargetType.MODELLED_EXPANSION_TARGET
        is_struct_confirmed = False
    else:
        target_type = TargetType.MODELLED_EXPANSION_TARGET
        is_struct_confirmed = False

    return {
        "target_price": round(target_price, 2) if target_price is not None else None,
        "target_tier": target_tier,
        "target_type": target_type,
        "target_source": target_source,
        "is_structurally_confirmed": is_struct_confirmed,
        "has_intermediate_obstacle": has_obstacle,
        "obstacle_price": round(obstacle_price, 2) if obstacle_price is not None else None
    }


def calculate_structural_trade_levels(
    direction: str,
    cmp_price: float,
    mtf_suite: Dict[str, Any],
    indicators: Dict[str, Any],
    option_quote: Optional[Dict[str, Any]] = None,
    is_closed: bool = True
) -> Dict[str, Any]:
    """
    Computes separate Underlying Structural Risk/Reward and Option Premium Risk/Reward.
    Never presents underlying NIFTY points-based R:R as the option's expected R:R.
    If reliable option estimation is unavailable, option R:R is marked UNAVAILABLE.
    """
    tfa = mtf_suite.get("timeframe_analyses", {})
    vol_5m = indicators.get("volatility", {})
    atr = vol_5m.get("atr") or 12.0
    buffer = round(0.2 * atr, 2)

    struct_5m = tfa.get(Timeframe.M5, {}).get("structure", {})
    struct_15m = tfa.get(Timeframe.M15, {}).get("structure", {})

    last_high_5m = struct_5m.get("latest_confirmed_high")
    last_low_5m = struct_5m.get("latest_confirmed_low")
    last_high_15m = struct_15m.get("latest_confirmed_high")
    last_low_15m = struct_15m.get("latest_confirmed_low")

    entry_price = round(cmp_price, 2)
    stop_loss: Optional[float] = None
    is_valid_levels = False

    # 1. Stop-Loss Selection (Nearest confirmed structural pivot)
    if direction == SetupDirection.BULLISH:
        low_pivot = last_low_5m or last_low_15m
        if low_pivot:
            stop_loss = round(float(low_pivot["pivot_price"]) - buffer, 2)
    elif direction == SetupDirection.BEARISH:
        high_pivot = last_high_5m or last_high_15m
        if high_pivot:
            stop_loss = round(float(high_pivot["pivot_price"]) + buffer, 2)

    # 2. Target Selection via Deterministic Hierarchy
    tgt_info = calculate_structural_target(direction, entry_price, mtf_suite, atr=atr)
    target_price = tgt_info["target_price"]

    # 3. Geometric Boundary Validation
    if stop_loss is not None and target_price is not None:
        if direction == SetupDirection.BULLISH:
            if stop_loss < entry_price < target_price:
                is_valid_levels = True
        elif direction == SetupDirection.BEARISH:
            if target_price < entry_price < stop_loss:
                is_valid_levels = True

    risk_pts = round(abs(entry_price - stop_loss), 2) if stop_loss is not None else None
    reward_pts = round(abs(target_price - entry_price), 2) if target_price is not None else None

    underlying_rr = None
    underlying_rr_str = "UNAVAILABLE"
    if is_valid_levels and risk_pts is not None and reward_pts is not None and risk_pts > 0:
        underlying_rr = round(reward_pts / risk_pts, 2)
        underlying_rr_str = f"1 : {underlying_rr:.2f}"

    passes_min_rr = bool(underlying_rr is not None and underlying_rr >= MIN_RISK_REWARD)

    underlying_rr_block = {
        "entry_price": entry_price,
        "stop_loss_price": stop_loss,
        "target_price": target_price,
        "risk_points": risk_pts,
        "reward_points": reward_pts,
        "risk_reward_ratio": underlying_rr,
        "risk_reward_str": underlying_rr_str,
        "is_valid_levels": is_valid_levels,
        "passes_minimum_rr": passes_min_rr,
        "target_type": tgt_info["target_type"],
        "target_source": tgt_info["target_source"],
        "is_structurally_confirmed": tgt_info["is_structurally_confirmed"],
        "atr_buffer_applied": buffer,
        "target_selection_tier": tgt_info["target_tier"],
        "has_intermediate_obstacle": tgt_info["has_intermediate_obstacle"],
        "obstacle_price": tgt_info["obstacle_price"],
        "confirmation_status": ConfirmationStatus.CONFIRMED if is_closed else ConfirmationStatus.DEVELOPING
    }

    # 4. Separate Option Premium Risk/Reward Engine
    option_rr_block: Dict[str, Any] = {
        "status": "UNAVAILABLE",
        "reason": "OPTION_QUOTE_NOT_EVALUATED",
        "model_classification": "OPTION_GREEK_APPROXIMATION",
        "contract_symbol": None,
        "strike": None,
        "option_type": None,
        "option_entry_price": None,
        "option_stop_loss_estimate": None,
        "option_target_estimate": None,
        "option_risk_points": None,
        "option_reward_points": None,
        "option_risk_reward_ratio": None,
        "option_risk_reward_str": "UNAVAILABLE",
        "signed_delta_applied": None,
        "delta_applied": None,
        "gamma_applied": None,
        "provenance": None,
        "assumptions": None,
        "explanation": "Option premium risk/reward is UNAVAILABLE: Option expected returns require verified delta/IV models and cannot be fabricated from underlying points."
    }

    if option_quote is not None and is_valid_levels and risk_pts is not None and reward_pts is not None:
        opt_sym = option_quote.get("symbol")
        opt_strike = option_quote.get("strike")
        opt_type = (option_quote.get("option_type") or ("CE" if direction == SetupDirection.BULLISH else "PE")).upper()
        opt_ask = option_quote.get("ask")
        opt_bid = option_quote.get("bid")
        last_trade_time = option_quote.get("last_trade_time") or option_quote.get("timestamp")

        now_ist = IndianMarketSessionClock.now_ist()
        now_ist_str = now_ist.isoformat()

        # Strict Executable Ask Verification (NO LTP FALLBACK)
        if opt_ask is None or opt_ask <= 0:
            option_rr_block["status"] = "UNAVAILABLE"
            option_rr_block["reason"] = "ASK_UNAVAILABLE"
            option_rr_block["explanation"] = "Executable option entry requires valid Ask price (Ask <= 0 or missing). LTP fallback strictly prohibited."
        elif opt_bid is None or opt_bid <= 0:
            option_rr_block["status"] = "UNAVAILABLE"
            option_rr_block["reason"] = "BID_UNAVAILABLE"
            option_rr_block["explanation"] = "Executable option valuation requires valid Bid price (Bid <= 0 or missing)."
        elif opt_bid > opt_ask:
            option_rr_block["status"] = "UNAVAILABLE"
            option_rr_block["reason"] = "CROSSED_ORDERBOOK"
            option_rr_block["explanation"] = f"Crossed orderbook anomaly: Bid (₹{opt_bid}) > Ask (₹{opt_ask}). Corrupted quote feed."
        elif not opt_strike or not entry_price or float(opt_strike) <= 0:
            option_rr_block["status"] = "UNAVAILABLE"
            option_rr_block["reason"] = "MISSING_REQUIRED_INPUTS"
            option_rr_block["explanation"] = "Required inputs for Black-Scholes Greek approximation (strike or spot) are missing."
        else:
            # Quote Freshness Verification (<= 60.0s qualifies, > 60.0s rejected)
            quote_age_s = None
            is_quote_stale = False
            if last_trade_time:
                try:
                    if isinstance(last_trade_time, str):
                        if "T" in last_trade_time:
                            q_dt = datetime.fromisoformat(last_trade_time.replace("Z", "+00:00"))
                        else:
                            q_dt = datetime.strptime(last_trade_time[:19], "%Y-%m-%d %H:%M:%S")
                    elif isinstance(last_trade_time, datetime):
                        q_dt = last_trade_time
                    else:
                        q_dt = None

                    if q_dt:
                        if q_dt.tzinfo is None:
                            q_dt = q_dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
                        quote_age_s = max(0.0, (now_ist - q_dt).total_seconds())
                        if quote_age_s > QuoteFreshnessThresholds.MAX_QUALIFICATION_LATENCY_SECONDS:
                            is_quote_stale = True
                    else:
                        is_quote_stale = True
                except Exception:
                    is_quote_stale = True
            else:
                is_quote_stale = True

            if is_quote_stale:
                option_rr_block["status"] = "UNAVAILABLE"
                option_rr_block["reason"] = "STALE_QUOTE"
                age_display = f"{quote_age_s:.1f}s" if quote_age_s is not None else "missing timestamp"
                option_rr_block["explanation"] = f"Option quote is stale ({age_display} > 60.0s qualification cutoff). Fresh executable quote required."
            else:
                # Valid executable Ask entry price
                opt_entry = round(float(opt_ask), 2)
                mid_exit_basis = round((float(opt_bid) + float(opt_ask)) / 2.0, 2)

                # Time to expiry convention (ACT/365 with exact intraday settlement timestamp)
                exp_raw = option_quote.get("expiry_date") or option_quote.get("expiry")
                t_years = 7.0 / 365.0
                is_same_day_expiry = False
                if exp_raw:
                    try:
                        if isinstance(exp_raw, str):
                            exp_date = date.fromisoformat(exp_raw[:10])
                        elif isinstance(exp_raw, (date, datetime)):
                            exp_date = exp_raw.date() if isinstance(exp_raw, datetime) else exp_raw
                        else:
                            exp_date = None
                        if exp_date:
                            ist_tz = timezone(timedelta(hours=5, minutes=30))
                            # Standard NSE equity derivative session closes at 15:30:00 IST on expiry date
                            exp_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 15, 30, 0, tzinfo=ist_tz)
                            if exp_date == now_ist.date():
                                is_same_day_expiry = True

                            rem_seconds = (exp_dt - now_ist).total_seconds()
                            if rem_seconds > 0:
                                t_years = rem_seconds / (365.0 * 86400.0)
                            else:
                                t_years = 0.0
                    except Exception:
                        pass

                if t_years <= 0.0:
                    option_rr_block = {
                        "status": "UNAVAILABLE",
                        "reason": "CONTRACT_EXPIRED_SAME_DAY",
                        "option_risk_reward_ratio": None,
                        "option_risk_reward_str": "UNAVAILABLE"
                    }
                else:
                    iv_val = float(option_quote.get("iv") or 0.15)
                    iv_src = "contract_market_iv" if option_quote.get("iv") else "constant_fallback_iv_0.15"

                    greeks = calculate_black_scholes_greeks(
                        spot=entry_price,
                        strike=float(opt_strike),
                        time_to_expiry_years=t_years,
                        risk_free_rate=0.065,
                        volatility=iv_val,
                        option_type=opt_type
                    )
                    signed_delta = greeks.get("delta", 0.50 if opt_type == "CE" else -0.50)
                    gamma = greeks.get("gamma", 0.001)

                    # Greek input sanity check
                    is_valid_greeks = True
                    greek_err = None
                    if not (math.isfinite(signed_delta) and math.isfinite(gamma)):
                        is_valid_greeks = False
                        greek_err = "NON_FINITE_GREEK_VALUES"
                    elif gamma < 0.0:
                        is_valid_greeks = False
                        greek_err = f"NEGATIVE_GAMMA_ANOMALY: Gamma ({gamma}) must be non-negative for long options."
                    elif opt_type == "CE" and not (-0.01 <= signed_delta <= 1.01):
                        is_valid_greeks = False
                        greek_err = f"CALL_DELTA_OUT_OF_BOUNDS: Delta ({signed_delta}) outside [0, 1]."
                    elif opt_type == "PE" and not (-1.01 <= signed_delta <= 0.01):
                        is_valid_greeks = False
                        greek_err = f"PUT_DELTA_OUT_OF_BOUNDS: Delta ({signed_delta}) outside [-1, 0]."

                    if not is_valid_greeks:
                        option_rr_block = {
                            "status": "UNAVAILABLE",
                            "reason": greek_err or "INVALID_GREEK_INPUTS",
                            "option_risk_reward_ratio": None,
                            "option_risk_reward_str": "UNAVAILABLE"
                        }
                    else:
                        dS_stop = stop_loss - entry_price
                        dS_target = target_price - entry_price

                        # Taylor expansion with signed delta: ΔP ≈ Delta * ΔS + 0.5 * Gamma * (ΔS)^2
                        dOpt_tgt_res = project_option_premium_change(signed_delta, gamma, dS_target)
                        dOpt_stop_res = project_option_premium_change(signed_delta, gamma, dS_stop)

                        dOpt_tgt = dOpt_tgt_res["delta_premium"]
                        dOpt_stop = dOpt_stop_res["delta_premium"]

                        # 1. Theoretical continuous unrounded estimates
                        theoretical_target = round(opt_entry + dOpt_tgt, 4)
                        theoretical_stop = max(0.0, round(opt_entry + dOpt_stop, 4))

                        # 2. Exchange tick-size rounded quotation (NSE minimum tick size = ₹0.05)
                        exchange_rounded_target = round_to_exchange_tick(theoretical_target, 0.05)
                        exchange_rounded_stop = max(0.05, round_to_exchange_tick(theoretical_stop, 0.05))

                        opt_risk = round(opt_entry - exchange_rounded_stop, 2)
                        opt_reward = round(exchange_rounded_target - opt_entry, 2)

                        if opt_risk > 0 and opt_reward > 0:
                            opt_rr_ratio = round(opt_reward / opt_risk, 2)
                            option_rr_block = {
                                "status": "VALID_ESTIMATE",
                                "reason": "CALCULATED_OPTION_GREEK_APPROXIMATION",
                                "model_classification": "OPTION_GREEK_APPROXIMATION",
                                "contract_symbol": opt_sym,
                                "strike": float(opt_strike),
                                "option_type": opt_type,
                                "option_entry_price": opt_entry,
                                "theoretical_option_target": theoretical_target,
                                "theoretical_option_stop_loss": theoretical_stop,
                                "exchange_rounded_target_price": exchange_rounded_target,
                                "exchange_rounded_stop_loss": exchange_rounded_stop,
                                "exchange_tick_size": 0.05,
                                "option_stop_loss_estimate": exchange_rounded_stop,
                                "option_target_estimate": exchange_rounded_target,
                                "option_risk_points": opt_risk,
                                "option_reward_points": opt_reward,
                                "option_risk_reward_ratio": opt_rr_ratio,
                                "option_risk_reward_str": f"1 : {opt_rr_ratio:.2f}",
                                "signed_delta_applied": signed_delta,
                                "delta_applied": signed_delta,
                                "gamma_applied": gamma,
                                "provenance": {
                                    "underlying_spot_price": entry_price,
                                    "spot_source": "multi_timeframe_candle_store_cmp",
                                    "spot_timestamp": now_ist_str,
                                    "instrument_token": str(option_quote.get("instrument_token") or option_quote.get("token") or "UNAVAILABLE"),
                                    "contract_symbol": opt_sym,
                                    "option_type": opt_type,
                                    "strike": float(opt_strike),
                                    "exact_expiry_date": str(exp_raw) if exp_raw else "UNAVAILABLE",
                                    "is_same_day_expiry": is_same_day_expiry,
                                    "implied_volatility": iv_val,
                                    "iv_source": iv_src,
                                    "greeks_calculation_timestamp": now_ist_str,
                                    "time_to_expiry_convention": "ACT/365",
                                    "settlement_time_convention": "15:30:00_IST",
                                    "time_to_expiry_years": round(t_years, 6),
                                    "risk_free_rate": 0.065,
                                    "risk_free_rate_source": "ASSUMED_RISK_FREE_RATE_6.5_PERCENT",
                                    "model_name": "Black-Scholes-Merton Greek Model (Signed Delta + Second-Order Gamma Taylor Approximation)"
                                },
                                "assumptions": {
                                    "model": "Second-order Taylor series Delta/Gamma local approximation",
                                    "formula": "ΔP ≈ Delta * ΔS + 0.5 * Gamma * (ΔS)^2",
                                    "signed_delta_convention": "CE: N(d1) > 0 | PE: N(d1) - 1 < 0",
                                    "gamma_second_order_contribution": "Positive Gamma contributes a positive second-order term relative to linear Delta projection",
                                    "theta_decay": "EXCLUDED",
                                    "implied_volatility": "HELD_CONSTANT (Vega = 0)",
                                    "entry_quote_basis": "EXECUTABLE_ASK_PRICE",
                                    "exit_valuation_basis": "THEORETICAL_MID_PROJECTION (spread decay and slippage excluded)",
                                    "tick_size_quotation_floor": "₹0.05 minimum tick constraint on exchange (not theoretical fair value)",
                                    "transaction_costs": "EXCLUDED (Brokerage, STT, exchange turnover fees, and GST not deducted)",
                                    "higher_order_greeks": "TRUNCATED_AT_GAMMA",
                                    "greeks_timestamp": now_ist_str,
                                    "disclaimer": (
                                        "Positive Gamma contributes a positive second-order term relative to the linear Delta-only projection. "
                                        "Approximation valid only for local price moves under declared assumptions. "
                                        "Not an execution-guaranteed option price."
                                    )
                                },
                                "explanation": (
                                    f"Option premium R:R estimated via signed Delta ({signed_delta:+.3f}) and "
                                    f"Gamma ({gamma:.5f}) Taylor approximation from Ask ₹{opt_entry:.2f}. IV and Theta held constant."
                                )
                            }

    return {
        "direction": direction,
        "underlying_risk_reward": underlying_rr_block,
        "option_premium_risk_reward": option_rr_block,
        "option_risk_reward": option_rr_block,  # Backward-compatible alias
        # Backward-compatible convenience aliases
        "entry_price": entry_price,
        "stop_loss_price": stop_loss,
        "target_price": target_price,
        "risk_reward_ratio": underlying_rr,
        "risk_reward_str": underlying_rr_str,
        "is_valid_levels": is_valid_levels,
        "passes_minimum_rr": passes_min_rr,
        "target_type": tgt_info["target_type"],
        "is_structurally_confirmed": tgt_info["is_structurally_confirmed"]
    }


# ============================================================================
# 4. Canonical Eight-Gate Tradability Registry Engine
# ============================================================================

def evaluate_canonical_eight_gates(
    direction: str,
    bullish_eval: Dict[str, Any],
    bearish_eval: Dict[str, Any],
    mtf_suite: Dict[str, Any],
    structural_levels: Dict[str, Any],
    option_tradability: Optional[Dict[str, Any]] = None,
    is_candle_closed: bool = True,
    setup_depends_on_developing_bar: bool = False
) -> Dict[str, Any]:
    """
    Standardizes the system around the Canonical Eight Mandatory Tradability Gates.
    Each gate has:
      - Stable machine-readable ID (GATE_1 .. GATE_8)
      - Deterministic evaluation
      - Pass/Fail status
      - Specific rejection reason
      - Relevant source data
    """
    tfa = mtf_suite.get("timeframe_analyses", {})
    gate_registry: Dict[str, Dict[str, Any]] = {}
    rejection_reasons: List[str] = []

    # -------------------------------------------------------------------------
    # Gate 1: Squeeze Compression Gate
    # Distinguishes ACTIVE_COMPRESSION (reject) from CONFIRMED_RELEASE (pass)
    # -------------------------------------------------------------------------
    g1_passed = True
    g1_reason = None
    g1_source = {}
    for tf in [Timeframe.M15, Timeframe.M5]:
        reg_dict = tfa.get(tf, {}).get("regime", {})
        reg = reg_dict.get("regime")
        metrics = reg_dict.get("metrics", {})
        is_sq_on = (
            metrics.get("is_squeeze_on", False)
            or reg_dict.get("is_squeeze", False)
            or reg_dict.get("is_squeeze_on", False)
            or (reg == MarketRegime.VOLATILITY_COMPRESSION)
        )
        is_sq_release = (
            metrics.get("is_squeeze_release", False)
            or reg_dict.get("is_squeeze_release", False)
            or (reg == MarketRegime.VOLATILITY_EXPANSION)
        )

        if is_sq_on:
            sq_state = SqueezeLifecycleState.ACTIVE_COMPRESSION
        elif is_sq_release:
            sq_state = SqueezeLifecycleState.CONFIRMED_RELEASE
        elif reg == MarketRegime.INSUFFICIENT_DATA:
            sq_state = SqueezeLifecycleState.UNKNOWN
        else:
            sq_state = SqueezeLifecycleState.NO_SQUEEZE

        g1_source[f"{tf}_regime"] = reg
        g1_source[f"{tf}_squeeze_lifecycle"] = sq_state

        if sq_state == SqueezeLifecycleState.ACTIVE_COMPRESSION:
            g1_passed = False
            g1_reason = f"Market is in ACTIVE_COMPRESSION (Carter's Squeeze ON) on {tf}. Directional trading prohibited while energy coils."
            break

    gate_registry[CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION] = {
        "gate_id": CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION,
        "gate_name": "Squeeze Compression Gate",
        "passed": g1_passed,
        "rejection_reason": g1_reason,
        "source_data": g1_source
    }
    if not g1_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_1_SQUEEZE_COMPRESSION}: {g1_reason}")

    # -------------------------------------------------------------------------
    # Gate 2: Conflicting MTF Regimes Gate
    # -------------------------------------------------------------------------
    g2_passed = True
    g2_reason = None
    reg_15m = tfa.get(Timeframe.M15, {}).get("regime", {}).get("regime")
    g2_source = {"15m_anchor_regime": reg_15m, "candidate_direction": direction}

    if direction == SetupDirection.BULLISH and reg_15m == MarketRegime.TRENDING_BEARISH:
        g2_passed = False
        g2_reason = "15m Macro Anchor regime is confirmed TRENDING_BEARISH, directly opposing Bullish setup."
    elif direction == SetupDirection.BEARISH and reg_15m == MarketRegime.TRENDING_BULLISH:
        g2_passed = False
        g2_reason = "15m Macro Anchor regime is confirmed TRENDING_BULLISH, directly opposing Bearish setup."

    gate_registry[CanonicalGateID.GATE_2_CONFLICTING_MTF_REGIMES] = {
        "gate_id": CanonicalGateID.GATE_2_CONFLICTING_MTF_REGIMES,
        "gate_name": "Conflicting MTF Regimes Gate",
        "passed": g2_passed,
        "rejection_reason": g2_reason,
        "source_data": g2_source
    }
    if not g2_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_2_CONFLICTING_MTF_REGIMES}: {g2_reason}")

    # -------------------------------------------------------------------------
    # Gate 3: High Mutual Confluence Gate
    # -------------------------------------------------------------------------
    bull_score = bullish_eval.get("total_score", 0.0)
    bear_score = bearish_eval.get("total_score", 0.0)
    g3_passed = True
    g3_reason = None
    g3_source = {"bullish_score": bull_score, "bearish_score": bear_score, "threshold": MIN_QUALIFIED_SCORE}

    if bull_score >= MIN_QUALIFIED_SCORE and bear_score >= MIN_QUALIFIED_SCORE:
        g3_passed = False
        g3_reason = f"High mutual confluence (Bullish: {bull_score}, Bearish: {bear_score}). Signals in violent contradiction."

    gate_registry[CanonicalGateID.GATE_3_HIGH_MUTUAL_CONFLUENCE] = {
        "gate_id": CanonicalGateID.GATE_3_HIGH_MUTUAL_CONFLUENCE,
        "gate_name": "High Mutual Confluence Gate",
        "passed": g3_passed,
        "rejection_reason": g3_reason,
        "source_data": g3_source
    }
    if not g3_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_3_HIGH_MUTUAL_CONFLUENCE}: {g3_reason}")

    # -------------------------------------------------------------------------
    # Gate 4: Feed Gap or Unreliable Data Gate
    # -------------------------------------------------------------------------
    g4_passed = True
    g4_reason = None
    g4_source: Dict[str, Any] = {}
    for tf, data in tfa.items():
        q_break = data.get("structural_break", {}).get("data_quality_status")
        q_struct = data.get("structure", {}).get("data_quality_status")
        g4_source[f"{tf}_break_quality"] = q_break
        g4_source[f"{tf}_struct_quality"] = q_struct
        if q_break == "FEED_GAP" or q_struct == "FEED_GAP":
            g4_passed = False
            g4_reason = f"Data quality degraded by unverified feed gap on {tf}."
            break

    gate_registry[CanonicalGateID.GATE_4_FEED_GAP_OR_UNRELIABLE_DATA] = {
        "gate_id": CanonicalGateID.GATE_4_FEED_GAP_OR_UNRELIABLE_DATA,
        "gate_name": "Feed Gap & Data Quality Gate",
        "passed": g4_passed,
        "rejection_reason": g4_reason,
        "source_data": g4_source
    }
    if not g4_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_4_FEED_GAP_OR_UNRELIABLE_DATA}: {g4_reason}")

    # -------------------------------------------------------------------------
    # Gate 5: Unfavorable Risk/Reward Gate
    # -------------------------------------------------------------------------
    rr = structural_levels.get("risk_reward_ratio")
    g5_passed = True
    g5_reason = None
    g5_source = {"underlying_risk_reward": rr, "min_required": MIN_RISK_REWARD}

    if rr is None or rr < MIN_RISK_REWARD:
        g5_passed = False
        rr_str = f"1 : {rr:.2f}" if rr is not None else "UNAVAILABLE"
        g5_reason = f"Structural R:R ({rr_str}) is below minimum threshold (1 : {MIN_RISK_REWARD:.2f})."

    gate_registry[CanonicalGateID.GATE_5_UNFAVORABLE_RISK_REWARD] = {
        "gate_id": CanonicalGateID.GATE_5_UNFAVORABLE_RISK_REWARD,
        "gate_name": "Unfavorable Risk/Reward Gate",
        "passed": g5_passed,
        "rejection_reason": g5_reason,
        "source_data": g5_source
    }
    if not g5_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_5_UNFAVORABLE_RISK_REWARD}: {g5_reason}")

    # -------------------------------------------------------------------------
    # Gate 6: Untradable Option Contract Gate
    # -------------------------------------------------------------------------
    g6_passed = True
    g6_reason = None
    g6_source = {}

    if option_tradability is not None:
        g6_passed = option_tradability.get("is_tradable", False)
        g6_source = {
            "contract_symbol": option_tradability.get("contract_symbol"),
            "spread_pct": option_tradability.get("spread_pct"),
            "quote_age_seconds": option_tradability.get("quote_age_seconds"),
            "freshness_status": option_tradability.get("freshness_status")
        }
        if not g6_passed:
            opt_reasons = option_tradability.get("rejection_reasons", ["Option contract liquidity gate failed."])
            g6_reason = "; ".join(opt_reasons)

    gate_registry[CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT] = {
        "gate_id": CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT,
        "gate_name": "Untradable Option Contract Gate",
        "passed": g6_passed,
        "rejection_reason": g6_reason,
        "source_data": g6_source
    }
    if not g6_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_6_UNTRADABLE_OPTION_CONTRACT}: {g6_reason}")

    # -------------------------------------------------------------------------
    # Gate 7: Invalid Structural Levels Gate
    # -------------------------------------------------------------------------
    is_valid_levels = structural_levels.get("is_valid_levels", False)
    g7_passed = is_valid_levels
    g7_reason = None
    g7_source = {
        "entry_price": structural_levels.get("entry_price"),
        "stop_loss_price": structural_levels.get("stop_loss_price"),
        "target_price": structural_levels.get("target_price")
    }

    if not g7_passed:
        g7_reason = "Structural stop-loss or target price is invalid or placed on the wrong side of entry."

    gate_registry[CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS] = {
        "gate_id": CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS,
        "gate_name": "Invalid Structural Levels Gate",
        "passed": g7_passed,
        "rejection_reason": g7_reason,
        "source_data": g7_source
    }
    if not g7_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_7_INVALID_STRUCTURAL_LEVELS}: {g7_reason}")

    # -------------------------------------------------------------------------
    # Gate 8: Developing Unconfirmed Bar Gate
    # Rejects ONLY when the candidate setup depends on an active/unclosed bar
    # -------------------------------------------------------------------------
    candidate_depends_on_unclosed = setup_depends_on_developing_bar or (not is_candle_closed)
    g8_passed = not candidate_depends_on_unclosed
    g8_reason = None
    g8_source = {
        "setup_depends_on_developing_bar": setup_depends_on_developing_bar,
        "is_candle_closed": is_candle_closed,
        "candidate_depends_on_unclosed": candidate_depends_on_unclosed
    }

    if not g8_passed:
        g8_reason = "Candidate setup depends on an active, unconfirmed developing candle. Awaiting completed bar closure."

    gate_registry[CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR] = {
        "gate_id": CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR,
        "gate_name": "Developing Unconfirmed Bar Gate",
        "passed": g8_passed,
        "rejection_reason": g8_reason,
        "source_data": g8_source
    }
    if not g8_passed:
        rejection_reasons.append(f"{CanonicalGateID.GATE_8_DEVELOPING_UNCONFIRMED_BAR}: {g8_reason}")

    all_passed = (len(rejection_reasons) == 0)

    return {
        "all_gates_passed": all_passed,
        "gate_count": 8,
        "passed_gate_count": sum(1 for g in gate_registry.values() if g["passed"]),
        "failed_gate_count": len(rejection_reasons),
        "rejection_reasons": rejection_reasons,
        "gate_registry": gate_registry
    }


# Backward-compatible bridge
def evaluate_fail_closed_gates(
    direction: str,
    bullish_eval: Dict[str, Any],
    bearish_eval: Dict[str, Any],
    mtf_suite: Dict[str, Any],
    structural_levels: Dict[str, Any],
    option_tradability: Optional[Dict[str, Any]] = None,
    is_candle_closed: bool = True,
    setup_depends_on_developing_bar: bool = False
) -> Tuple[bool, List[str]]:
    """Backward-compatible tuple wrapper around evaluate_canonical_eight_gates."""
    res = evaluate_canonical_eight_gates(
        direction, bullish_eval, bearish_eval, mtf_suite,
        structural_levels, option_tradability, is_candle_closed,
        setup_depends_on_developing_bar=setup_depends_on_developing_bar
    )
    return res["all_gates_passed"], res["rejection_reasons"]


# ============================================================================
# 5. Opportunity Evaluation Coordinator
# ============================================================================

class SetupScoringService:
    """
    Coordinates Multi-Timeframe Confluence Scoring, Tradability Gates,
    Risk/Reward derivation, and Option Contract Tradability validation.
    """

    def __init__(
        self,
        candle_store: MultiTimeframeCandleStore,
        structure_service: Optional[MarketStructureService] = None,
        indicator_service: Optional[TechnicalIndicatorService] = None
    ):
        self.candle_store = candle_store
        self.indicator_service = indicator_service or TechnicalIndicatorService(candle_store)
        self.structure_service = structure_service or MarketStructureService(candle_store, self.indicator_service)

    def evaluate_opportunity(
        self,
        symbol: str,
        option_quote: Optional[Dict[str, Any]] = None,
        is_candle_closed: bool = True,
        setup_depends_on_developing_bar: bool = False
    ) -> Dict[str, Any]:
        """
        Executes complete Phase 2C Multi-Timeframe Setup Evaluation.
        Returns independent bullish and bearish scores, canonical 8-gate registry,
        separate underlying and option risk/rewards, and human-readable explanation.
        """
        sym = symbol.strip().upper()
        timeframes = [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]

        # 1. Compute Multi-Timeframe Market Structure Suite
        mtf_suite = self.structure_service.analyze_multi_timeframe_suite(sym, timeframes)

        # 2. Gather Indicators across all timeframes
        indicators_by_tf: Dict[str, Any] = {}
        for tf in timeframes:
            indicators_by_tf[tf] = self.indicator_service.compute_all_indicators(sym, tf, include_developing=not is_candle_closed)

        # 3. Determine current market price from latest closed bar
        c_1m = self.candle_store.get_closed_candles(sym, Timeframe.M1, limit=1)
        c_5m = self.candle_store.get_closed_candles(sym, Timeframe.M5, limit=1)
        if c_1m:
            cmp_price = float(c_1m[-1]["close"])
        elif c_5m:
            cmp_price = float(c_5m[-1]["close"])
        else:
            cmp_price = 24000.0

        # 4. Independent Confluence Scoring with Evidence Families
        bullish_eval = score_directional_confluence(SetupDirection.BULLISH, mtf_suite, indicators_by_tf, cmp_price=cmp_price)
        bearish_eval = score_directional_confluence(SetupDirection.BEARISH, mtf_suite, indicators_by_tf, cmp_price=cmp_price)

        # 5. Determine Candidate Direction based on Rule Alignment
        bull_score = bullish_eval["total_score"]
        bear_score = bearish_eval["total_score"]

        if bull_score >= MIN_QUALIFIED_SCORE and bear_score >= MIN_QUALIFIED_SCORE:
            candidate_dir = SetupDirection.CONFLICTED
        elif bull_score >= MIN_QUALIFIED_SCORE and bull_score > bear_score:
            candidate_dir = SetupDirection.BULLISH
        elif bear_score >= MIN_QUALIFIED_SCORE and bear_score > bull_score:
            candidate_dir = SetupDirection.BEARISH
        else:
            candidate_dir = SetupDirection.NEUTRAL

        # 6. Validate Option Contract (if quote supplied)
        option_tradability = None
        if option_quote is not None:
            expected_opt_type = "CE" if candidate_dir == SetupDirection.BULLISH else ("PE" if candidate_dir == SetupDirection.BEARISH else None)
            option_tradability = validate_option_contract_against_master(
                contract_info=option_quote,
                underlying_symbol=sym,
                expected_option_type=expected_opt_type
            )

        # 7. Calculate Structural Trade Levels (Separate Underlying & Option R:R)
        ind_5m = indicators_by_tf.get(Timeframe.M5, {})
        structural_levels = calculate_structural_trade_levels(
            direction=candidate_dir if candidate_dir in (SetupDirection.BULLISH, SetupDirection.BEARISH) else SetupDirection.BULLISH,
            cmp_price=cmp_price,
            mtf_suite=mtf_suite,
            indicators=ind_5m,
            option_quote=option_quote if (option_tradability and option_tradability.get("is_tradable")) else None,
            is_closed=is_candle_closed
        )

        # 8. Evaluate Canonical Eight Tradability Gates
        gate_eval = evaluate_canonical_eight_gates(
            direction=candidate_dir,
            bullish_eval=bullish_eval,
            bearish_eval=bearish_eval,
            mtf_suite=mtf_suite,
            structural_levels=structural_levels,
            option_tradability=option_tradability,
            is_candle_closed=is_candle_closed,
            setup_depends_on_developing_bar=setup_depends_on_developing_bar
        )

        gates_passed = gate_eval["all_gates_passed"]
        gate_rejections = gate_eval["rejection_reasons"]

        # 9. Determine Final Trade Action
        if candidate_dir in (SetupDirection.BULLISH, SetupDirection.BEARISH) and gates_passed:
            trade_action = TradeActionRecommendation.QUALIFIED_SETUP
        else:
            trade_action = TradeActionRecommendation.NO_TRADE

        # 10. Synthesize Transparent Explanation
        explanation = self._build_explanation(
            sym, candidate_dir, trade_action, bull_score, bear_score,
            gates_passed, gate_rejections, structural_levels, option_tradability
        )

        return {
            "symbol": sym,
            "timestamp": datetime.now().isoformat(),
            "candidate_direction": candidate_dir,
            "trade_action": trade_action,
            "gates_passed": gates_passed,
            "gate_rejection_reasons": gate_rejections,
            "canonical_eight_gates": gate_eval,
            "confirmation_status": ConfirmationStatus.CONFIRMED if is_candle_closed else ConfirmationStatus.DEVELOPING,
            "bullish_assessment": bullish_eval,
            "bearish_assessment": bearish_eval,
            "structural_trade_levels": structural_levels,
            "underlying_risk_reward": structural_levels["underlying_risk_reward"],
            "option_premium_risk_reward": structural_levels["option_premium_risk_reward"],
            "option_contract_tradability": option_tradability,
            "explanation": explanation
        }

    def _build_explanation(
        self,
        symbol: str,
        direction: str,
        action: str,
        bull_score: float,
        bear_score: float,
        gates_passed: bool,
        gate_rejections: List[str],
        levels: Dict[str, Any],
        opt_trad: Optional[Dict[str, Any]]
    ) -> str:
        """Constructs an audited, human-readable qualitative explanation of the setup evaluation."""
        lines = []
        lines.append(f"TradeLens AI Opportunity Evaluation for {symbol}:")
        lines.append(f"- Confluence Scores: Bullish = {bull_score:.1f}/100 | Bearish = {bear_score:.1f}/100.")

        u_rr = levels.get("underlying_risk_reward", {})
        o_rr = levels.get("option_premium_risk_reward", {})

        if action == TradeActionRecommendation.QUALIFIED_SETUP:
            entry = u_rr.get("entry_price")
            sl = u_rr.get("stop_loss_price")
            tgt = u_rr.get("target_price")
            rr_str = u_rr.get("risk_reward_str", "N/A")
            lines.append(f"- Status: QUALIFIED {direction} SETUP. All 8 canonical gates passed.")
            lines.append(f"- Underlying NIFTY Structural Execution: Entry = ₹{entry:.2f}, Stop-Loss = ₹{sl:.2f}, Target = ₹{tgt:.2f} (Underlying R:R = {rr_str}).")
            if o_rr.get("status") == "VALID_ESTIMATE":
                lines.append(f"- Option Premium Execution ({o_rr.get('contract_symbol')}): Entry = ₹{o_rr.get('option_entry_price')}, Stop = ₹{o_rr.get('option_stop_loss_estimate')}, Target = ₹{o_rr.get('option_target_estimate')} (Option R:R = {o_rr.get('option_risk_reward_str')}).")
            else:
                lines.append(f"- Option Premium R:R: UNAVAILABLE (Option return requires verified non-linear model).")
        else:
            lines.append(f"- Status: NO TRADE.")
            if gate_rejections:
                lines.append("- Disqualifying Gate Rejection Reasons:")
                for r in gate_rejections:
                    lines.append(f"  * {r}")
            elif direction == SetupDirection.NEUTRAL:
                lines.append("- Reason: Insufficient confluence score (< 60.0 on both sides).")
            elif direction == SetupDirection.CONFLICTED:
                lines.append("- Reason: Conflicted market signals (both Bullish and Bearish scores exceed threshold).")

        return "\n".join(lines)
