"""
TradeLens AI - Technical Indicator Library (Phase 2A)
Deterministic, mathematically sound, pure-Python technical indicator engine.
Consumes MultiTimeframeCandleStore across all supported intraday timeframes (15s, 1m, 3m, 5m, 15m).

Guarantees:
1. Full 64-bit IEEE 754 floating-point precision throughout all internal recursions.
2. Complete segregation of confirmed closed bars from developing active bars (Zero Repainting).
3. Explicit metadata schema for every calculation:
   (indicator_name, parameters, symbol, timeframe, timestamp, is_closed, calculation_status, warmup_status, data_source, data_status).
4. Strictly non-predictive: never inspects future candle data.
5. Never fabricates, forward-fills, or substitutes missing data.
"""
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Any, Optional, Tuple, Union
import math
import zoneinfo

from app.data.base import DataStatus
from app.data.session_clock import IndianMarketSessionClock, IST_ZONE
from app.data.candle_store import (
    Timeframe,
    MultiTimeframeCandleStore,
    Candle
)
from app.data.futures_manager import FuturesContractManager


class CalculationStatus:
    VALID = "VALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNAVAILABLE_NO_DATA = "UNAVAILABLE_NO_DATA"
    UNAVAILABLE_NO_VOLUME = "UNAVAILABLE_NO_VOLUME"
    UNAVAILABLE_NO_FUTURES_FEED = "UNAVAILABLE_NO_FUTURES_FEED"
    UNAVAILABLE_STALE_FEED = "UNAVAILABLE_STALE_FEED"


class WarmupStatus:
    WARMUP_COMPLETE = "WARMUP_COMPLETE"
    PARTIAL_WARMUP = "PARTIAL_WARMUP"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# ============================================================================
# Pure Mathematical Calculation Functions (Full 64-bit Precision)
# ============================================================================

def calculate_sma(prices: List[float], period: int, round_output: bool = True) -> List[Optional[float]]:
    """
    Calculates Simple Moving Average (SMA).
    Maintains full internal float precision.
    """
    if len(prices) < period or period <= 0:
        return [None] * len(prices)
    sma: List[Optional[float]] = [None] * (period - 1)
    window_sum = sum(prices[:period])
    val = window_sum / float(period)
    sma.append(round(val, 2) if round_output else val)
    for i in range(period, len(prices)):
        window_sum += prices[i] - prices[i - period]
        val = window_sum / float(period)
        sma.append(round(val, 2) if round_output else val)
    return sma


def calculate_ema(prices: List[float], period: int, round_output: bool = True) -> List[Optional[float]]:
    """
    Calculates Exponential Moving Average (EMA).
    Formula:
    EMA_0 = SMA(P, N)  (exact arithmetic mean of first N bars)
    Multiplier alpha = 2 / (N + 1)
    EMA_t = P_t * alpha + EMA_{t-1} * (1 - alpha)
    Maintains full 64-bit IEEE float precision without intermediate loop rounding.
    """
    if len(prices) < period or period <= 0:
        return [None] * len(prices)

    ema_raw: List[Optional[float]] = [None] * (period - 1)
    first_sma = sum(prices[:period]) / float(period)
    ema_raw.append(first_sma)
    multiplier = 2.0 / (period + 1.0)

    for price in prices[period:]:
        prev = ema_raw[-1]
        current_ema = (price - prev) * multiplier + prev
        ema_raw.append(current_ema)

    if round_output:
        return [round(v, 2) if v is not None else None for v in ema_raw]
    return ema_raw


def calculate_dema(prices: List[float], period: int = 9, round_output: bool = True) -> List[Optional[float]]:
    """
    Calculates Double Exponential Moving Average (DEMA).
    Formula: DEMA = 2 * EMA(P, N) - EMA(EMA(P, N), N)
    Maintains full internal float precision.
    """
    if len(prices) < period:
        return [None] * len(prices)

    ema1_raw = calculate_ema(prices, period, round_output=False)
    valid_ema1 = [v for v in ema1_raw if v is not None]

    if len(valid_ema1) < period:
        return [None] * len(prices)

    ema2_sub = calculate_ema(valid_ema1, period, round_output=False)
    leading_nones = len(prices) - len(valid_ema1) + (period - 1)
    ema2_raw = [None] * leading_nones + [v for v in ema2_sub if v is not None]

    dema_raw: List[Optional[float]] = []
    for e1, e2 in zip(ema1_raw, ema2_raw):
        if e1 is not None and e2 is not None:
            dema_raw.append(2.0 * e1 - e2)
        else:
            dema_raw.append(None)

    if round_output:
        return [round(v, 2) if v is not None else None for v in dema_raw]
    return dema_raw


def calculate_tema(prices: List[float], period: int = 9, round_output: bool = True) -> List[Optional[float]]:
    """
    Calculates Triple Exponential Moving Average (TEMA).
    Formula: TEMA = 3 * EMA1 - 3 * EMA2 + EMA3
    where EMA1 = EMA(P), EMA2 = EMA(EMA1), EMA3 = EMA(EMA2)
    Maintains full internal float precision.
    """
    if len(prices) < period:
        return [None] * len(prices)

    ema1_raw = calculate_ema(prices, period, round_output=False)
    valid_ema1 = [v for v in ema1_raw if v is not None]
    if len(valid_ema1) < period:
        return [None] * len(prices)

    ema2_sub = calculate_ema(valid_ema1, period, round_output=False)
    valid_ema2 = [v for v in ema2_sub if v is not None]
    if len(valid_ema2) < period:
        return [None] * len(prices)

    ema3_sub = calculate_ema(valid_ema2, period, round_output=False)

    leading_nones_ema2 = len(prices) - len(valid_ema2)
    leading_nones_ema3 = len(prices) - len([v for v in ema3_sub if v is not None])

    ema2_raw = [None] * leading_nones_ema2 + valid_ema2
    ema3_raw = [None] * leading_nones_ema3 + [v for v in ema3_sub if v is not None]

    tema_raw: List[Optional[float]] = []
    for e1, e2, e3 in zip(ema1_raw, ema2_raw, ema3_raw):
        if e1 is not None and e2 is not None and e3 is not None:
            tema_raw.append(3.0 * e1 - 3.0 * e2 + e3)
        else:
            tema_raw.append(None)

    if round_output:
        return [round(v, 2) if v is not None else None for v in tema_raw]
    return tema_raw


def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Calculates Wilder's Relative Strength Index (RSI).
    Formula:
    Change = P_t - P_{t-1}
    Gain = max(0, Change), Loss = max(0, -Change)
    Initial average gain/loss over first 'period' bars (arithmetic mean).
    Subsequent: Wilder's smoothing (RMA) with alpha = 1 / period:
    AvgGain_t = (AvgGain_{t-1} * (period - 1) + Gain_t) / period
    AvgLoss_t = (AvgLoss_{t-1} * (period - 1) + Loss_t) / period
    RS = AvgGain / AvgLoss
    RSI = 100 - (100 / (1 + RS))

    Flat Market & Numerical Edge Conventions:
    - Completely Flat Series (AvgGain == 0 and AvgLoss == 0): Defined as RSI = 50.0 (neutral momentum equilibrium).
    - Strictly Rising Series (AvgGain > 0 and AvgLoss == 0): RSI = 100.0.
    - Strictly Falling Series (AvgGain == 0 and AvgLoss > 0): RSI = 0.0.
    - Uses EPSILON = 1e-12 to safeguard against IEEE 754 precision noise.
    """
    if len(prices) <= period or period <= 0:
        return [None] * len(prices)

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]

    rsi: List[Optional[float]] = [None] * period
    avg_gain = sum(gains[:period]) / float(period)
    avg_loss = sum(losses[:period]) / float(period)

    EPSILON = 1e-12
    if abs(avg_loss) < EPSILON and abs(avg_gain) < EPSILON:
        rsi.append(50.0)
    elif abs(avg_loss) < EPSILON:
        rsi.append(100.0)
    elif abs(avg_gain) < EPSILON:
        rsi.append(0.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / float(period)
        avg_loss = (avg_loss * (period - 1) + losses[i]) / float(period)

        if abs(avg_loss) < EPSILON and abs(avg_gain) < EPSILON:
            rsi.append(50.0)
        elif abs(avg_loss) < EPSILON:
            rsi.append(100.0)
        elif abs(avg_gain) < EPSILON:
            rsi.append(0.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(round(100.0 - (100.0 / (1.0 + rs)), 2))

    return rsi


def calculate_macd(
    prices: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    last_bar_is_closed: bool = True,
    candles_metadata: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Calculates MACD Line, Signal Line, and Histogram.
    Maintains full IEEE float precision throughout recursion.
    Preserves strict separation between confirmed closed crossovers and developing crosses.
    """
    if candles_metadata and len(candles_metadata) > 0:
        last_c = candles_metadata[-1]
        if "is_closed" in last_c:
            last_bar_is_closed = bool(last_c["is_closed"])

    if len(prices) < slow:
        return {
            "macd": None,
            "signal": None,
            "hist": None,
            "is_confirmed_bullish_cross": False,
            "is_confirmed_bearish_cross": False,
            "is_developing_bullish_cross": False,
            "is_developing_bearish_cross": False,
            "last_bar_is_closed": last_bar_is_closed,
            "crossover_status": "NO_CROSSOVER",
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    ema_fast_raw = calculate_ema(prices, fast, round_output=False)
    ema_slow_raw = calculate_ema(prices, slow, round_output=False)

    macd_line_raw: List[Optional[float]] = []
    for f, s in zip(ema_fast_raw, ema_slow_raw):
        if f is not None and s is not None:
            macd_line_raw.append(f - s)
        else:
            macd_line_raw.append(None)

    valid_macd = [v for v in macd_line_raw if v is not None]
    if len(valid_macd) >= signal:
        sig_ema_raw = calculate_ema(valid_macd, signal, round_output=False)
        leading_nones = len(macd_line_raw) - len(valid_macd)
        signal_line_raw = [None] * leading_nones + sig_ema_raw
    else:
        signal_line_raw = [None] * len(macd_line_raw)

    histogram_raw: List[Optional[float]] = []
    for m, s in zip(macd_line_raw, signal_line_raw):
        if m is not None and s is not None:
            histogram_raw.append(m - s)
        else:
            histogram_raw.append(None)

    # Crossover detection governed by candle closure metadata
    is_confirmed_bull = False
    is_confirmed_bear = False
    is_developing_bull = False
    is_developing_bear = False
    confirmed_idx = None
    confirmed_ts = None

    if last_bar_is_closed:
        # Latest bar [-1] is completed and finalized
        if len(histogram_raw) >= 2:
            h_curr = histogram_raw[-1]
            h_prev = histogram_raw[-2]
            if h_curr is not None and h_prev is not None:
                if h_curr > 0 and h_prev <= 0:
                    is_confirmed_bull = True
                elif h_curr < 0 and h_prev >= 0:
                    is_confirmed_bear = True
                confirmed_idx = len(histogram_raw) - 1
                if candles_metadata and len(candles_metadata) >= 1:
                    confirmed_ts = candles_metadata[-1].get("timestamp")
    else:
        # Latest bar [-1] is actively forming (unclosed); latest completed bar is [-2]
        if len(histogram_raw) >= 3:
            h_live = histogram_raw[-1]
            h_closed = histogram_raw[-2]
            h_prev = histogram_raw[-3]

            if h_closed is not None and h_prev is not None:
                if h_closed > 0 and h_prev <= 0:
                    is_confirmed_bull = True
                elif h_closed < 0 and h_prev >= 0:
                    is_confirmed_bear = True
                confirmed_idx = len(histogram_raw) - 2
                if candles_metadata and len(candles_metadata) >= 2:
                    confirmed_ts = candles_metadata[-2].get("timestamp")

            if h_live is not None and h_closed is not None:
                if h_live > 0 and h_closed <= 0:
                    is_developing_bull = True
                elif h_live < 0 and h_closed >= 0:
                    is_developing_bear = True
        elif len(histogram_raw) == 2:
            h_live = histogram_raw[-1]
            h_closed = histogram_raw[-2]
            if h_live is not None and h_closed is not None:
                is_developing_bull = (h_live > 0 and h_closed <= 0)
                is_developing_bear = (h_live < 0 and h_closed >= 0)

    last_macd = round(macd_line_raw[-1], 2) if macd_line_raw and macd_line_raw[-1] is not None else 0.0
    last_sig = round(signal_line_raw[-1], 2) if signal_line_raw and signal_line_raw[-1] is not None else 0.0
    last_hist = round(histogram_raw[-1], 2) if histogram_raw and histogram_raw[-1] is not None else 0.0

    if not last_bar_is_closed and (is_developing_bull or is_developing_bear):
        crossover_status = "DEVELOPING_BULLISH" if is_developing_bull else "DEVELOPING_BEARISH"
    elif is_confirmed_bull:
        crossover_status = "CONFIRMED_BULLISH"
    elif is_confirmed_bear:
        crossover_status = "CONFIRMED_BEARISH"
    else:
        crossover_status = "NO_CROSSOVER"

    return {
        "macd": last_macd,
        "signal": last_sig,
        "hist": last_hist,
        "is_confirmed_bullish_cross": is_confirmed_bull,
        "is_confirmed_bearish_cross": is_confirmed_bear,
        "is_developing_bullish_cross": is_developing_bull,
        "is_developing_bearish_cross": is_developing_bear,
        "last_bar_is_closed": last_bar_is_closed,
        "confirmed_candle_index": confirmed_idx,
        "confirmed_candle_timestamp": confirmed_ts,
        "crossover_status": crossover_status,
        "calculation_status": CalculationStatus.VALID
    }


def calculate_macd_series(
    prices: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    round_output: bool = True
) -> Dict[str, List[Optional[float]]]:
    """
    Calculates full time-series of MACD line, signal line, and histogram in O(N) time.
    Maintains full internal 64-bit float precision across the entire series.
    """
    n = len(prices)
    if n < slow or fast <= 0 or slow <= 0 or signal <= 0:
        return {
            "macd": [None] * n,
            "signal": [None] * n,
            "hist": [None] * n
        }

    ema_fast_raw = calculate_ema(prices, fast, round_output=False)
    ema_slow_raw = calculate_ema(prices, slow, round_output=False)

    macd_line_raw: List[Optional[float]] = []
    for f, s in zip(ema_fast_raw, ema_slow_raw):
        if f is not None and s is not None:
            macd_line_raw.append(f - s)
        else:
            macd_line_raw.append(None)

    valid_macd = [v for v in macd_line_raw if v is not None]
    if len(valid_macd) >= signal:
        sig_ema_raw = calculate_ema(valid_macd, signal, round_output=False)
        leading_nones = len(macd_line_raw) - len(valid_macd)
        signal_line_raw = [None] * leading_nones + sig_ema_raw
    else:
        signal_line_raw = [None] * len(macd_line_raw)

    histogram_raw: List[Optional[float]] = []
    for m, s in zip(macd_line_raw, signal_line_raw):
        if m is not None and s is not None:
            histogram_raw.append(m - s)
        else:
            histogram_raw.append(None)

    if round_output:
        return {
            "macd": [round(v, 2) if v is not None else None for v in macd_line_raw],
            "signal": [round(v, 2) if v is not None else None for v in signal_line_raw],
            "hist": [round(v, 2) if v is not None else None for v in histogram_raw]
        }
    return {
        "macd": macd_line_raw,
        "signal": signal_line_raw,
        "hist": histogram_raw
    }


def calculate_roc(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Calculates Rate of Change (ROC) momentum oscillator.
    Formula: ROC_t = ((Price_t - Price_{t-N}) / Price_{t-N}) * 100
    """
    if len(prices) <= period or period <= 0:
        return [None] * len(prices)

    roc: List[Optional[float]] = [None] * period
    for i in range(period, len(prices)):
        denom = prices[i - period]
        if denom == 0.0:
            roc.append(0.0)
        else:
            roc.append(round(((prices[i] - denom) / denom) * 100.0, 2))
    return roc


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """
    Calculates Average True Range (ATR) using Wilder's smoothing.
    Formula:
    TR_t = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|)
    ATR_0 = SMA(TR, period)
    ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period
    """
    if len(closes) < 2 or period <= 0:
        return 0.0

    tr_list = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        prev_c = closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

    if len(tr_list) < period:
        return round(sum(tr_list) / max(1, len(tr_list)), 2)

    atr = sum(tr_list[:period]) / float(period)
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / float(period)
    return round(atr, 2)


def calculate_adx(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> Dict[str, Any]:
    """
    Calculates Welles Wilder's Average Directional Index (ADX) and Directional Movement (+DI, -DI).
    Maintains full IEEE float precision throughout recursion.
    Trend strength evaluation:
    - ADX >= 25: STRONG_TREND
    - ADX < 20: WEAK_TREND_CHOP
    - 20 <= ADX < 25: DEVELOPING_TREND
    """
    if len(closes) < period * 2 or period <= 0:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "INSUFFICIENT_DATA",
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        prev_h = highs[i - 1]
        prev_l = lows[i - 1]
        prev_c = closes[i - 1]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

        up_move = h - prev_h
        down_move = prev_l - l

        if up_move > down_move and up_move > 0:
            plus_dm_list.append(up_move)
        else:
            plus_dm_list.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm_list.append(down_move)
        else:
            minus_dm_list.append(0.0)

    # Initial Wilder sum for first 'period' bars (candle index 'period', 15th candle for period=14)
    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    EPSILON = 1e-12
    if smoothed_tr > EPSILON:
        init_p_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
        init_m_di = 100.0 * (smoothed_minus_dm / smoothed_tr)
    else:
        init_p_di = 0.0
        init_m_di = 0.0

    init_di_sum = init_p_di + init_m_di
    if init_di_sum > EPSILON:
        init_dx = 100.0 * (abs(init_p_di - init_m_di) / init_di_sum)
    else:
        init_dx = 0.0

    dx_list = [(init_dx, init_p_di, init_m_di)]

    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / float(period)) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / float(period)) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / float(period)) + minus_dm_list[i]

        if smoothed_tr > EPSILON:
            p_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
            m_di = 100.0 * (smoothed_minus_dm / smoothed_tr)
        else:
            p_di = 0.0
            m_di = 0.0

        di_sum = p_di + m_di
        if di_sum > EPSILON:
            dx = 100.0 * (abs(p_di - m_di) / di_sum)
        else:
            dx = 0.0

        dx_list.append((dx, p_di, m_di))

    if len(dx_list) < period:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "INSUFFICIENT_DATA",
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    adx_val = sum(item[0] for item in dx_list[:period]) / float(period)
    for dx_item in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx_item[0]) / float(period)

    last_plus_di = dx_list[-1][1]
    last_minus_di = dx_list[-1][2]

    trend_strength = (
        "STRONG_TREND" if adx_val >= 25.0 else (
            "WEAK_TREND_CHOP" if adx_val < 20.0 else "DEVELOPING_TREND"
        )
    )

    return {
        "adx": round(adx_val, 2),
        "plus_di": round(last_plus_di, 2),
        "minus_di": round(last_minus_di, 2),
        "trend_strength": trend_strength,
        "calculation_status": CalculationStatus.VALID
    }


def calculate_bollinger_bands(
    prices: List[float],
    period: int = 20,
    num_std: float = 2.0,
    ddof: int = 0
) -> Dict[str, Any]:
    """
    Calculates Bollinger Bands (Middle, Upper, Lower, Bandwidth, %B).
    Formula:
    Middle = SMA(period)
    sigma = sqrt(sum((P - Middle)^2) / (period - ddof))
    Upper = Middle + num_std * sigma
    Lower = Middle - num_std * sigma
    Bandwidth = (Upper - Lower) / Middle
    %B = (Close - Lower) / (Upper - Lower)

    Population vs Sample Standard Deviation:
    John Bollinger's original specification specifies Population Standard Deviation (ddof=0).
    Sample standard deviation (ddof=1) is supported via the ddof parameter.
    """
    if len(prices) < period or period <= 0:
        return {
            "middle": None,
            "upper": None,
            "lower": None,
            "bandwidth": None,
            "pct_b": None,
            "sigma": None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    sma_list = calculate_sma(prices, period, round_output=False)
    middle = sma_list[-1]
    if middle is None:
        return {
            "middle": None, "upper": None, "lower": None,
            "bandwidth": None, "pct_b": None, "sigma": None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    recent_window = prices[-period:]
    denom = float(period - ddof) if (period - ddof) > 0 else 1.0
    variance = sum((p - middle) ** 2 for p in recent_window) / denom
    sigma = math.sqrt(variance)

    upper = middle + num_std * sigma
    lower = middle - num_std * sigma
    bw = (upper - lower) / max(0.0001, middle)

    last_price = prices[-1]
    band_width = upper - lower
    pct_b = (last_price - lower) / band_width if band_width > 0 else 0.5

    return {
        "middle": round(middle, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "bandwidth": round(bw, 4),
        "pct_b": round(pct_b, 4),
        "sigma": round(sigma, 2),
        "calculation_status": CalculationStatus.VALID
    }


def calculate_keltner_channels(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5
) -> Dict[str, Any]:
    """
    Calculates Keltner Channels.
    Formula:
    Middle = EMA(closes, ema_period)
    ATR = Wilder's ATR(highs, lows, closes, atr_period)
    Upper = Middle + multiplier * ATR
    Lower = Middle - multiplier * ATR
    """
    if len(closes) < max(ema_period, atr_period + 1):
        return {
            "middle": None,
            "upper": None,
            "lower": None,
            "atr": None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    ema_list = calculate_ema(closes, ema_period, round_output=False)
    middle = ema_list[-1]
    if middle is None:
        return {
            "middle": None, "upper": None, "lower": None,
            "atr": None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    atr = calculate_atr(highs, lows, closes, atr_period)
    upper = middle + multiplier * atr
    lower = middle - multiplier * atr

    return {
        "middle": round(middle, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "atr": round(atr, 2),
        "calculation_status": CalculationStatus.VALID
    }


def detect_volatility_squeeze(
    bollinger: Dict[str, Any],
    keltner: Dict[str, Any],
    prior_bollinger: Optional[Dict[str, Any]] = None,
    prior_keltner: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Detects John Carter's Volatility Squeeze (TTM Squeeze).
    States:
    - SQUEEZE_ON: Bollinger Bands contract inside Keltner Channels (BB Upper < KC Upper and BB Lower > KC Lower).
    - SQUEEZE_OFF: Bollinger Bands expand outside Keltner Channels.
    - SQUEEZE_RELEASE: Transition event where prior bar was SQUEEZE_ON and current bar is SQUEEZE_OFF.
    - INSUFFICIENT_DATA: Insufficient history to determine bands.

    Note: A squeeze release represents volatility expansion, not an automatic breakout direction.
    """
    if (
        bollinger.get("calculation_status") != CalculationStatus.VALID
        or keltner.get("calculation_status") != CalculationStatus.VALID
        or bollinger.get("upper") is None
        or keltner.get("upper") is None
    ):
        return {
            "is_squeeze_on": False,
            "is_squeeze_release": False,
            "squeeze_state": "INSUFFICIENT_DATA",
            "bandwidth": None,
            "calculation_status": CalculationStatus.INSUFFICIENT_DATA
        }

    bb_upper = bollinger["upper"]
    bb_lower = bollinger["lower"]
    kc_upper = keltner["upper"]
    kc_lower = keltner["lower"]

    is_squeeze_on = (bb_upper < kc_upper) and (bb_lower > kc_lower)

    # Check for SQUEEZE_RELEASE transition
    is_squeeze_release = False
    if prior_bollinger and prior_keltner:
        p_bbu = prior_bollinger.get("upper")
        p_bbl = prior_bollinger.get("lower")
        p_kcu = prior_keltner.get("upper")
        p_kcl = prior_keltner.get("lower")
        if p_bbu is not None and p_kcu is not None:
            prior_was_squeeze_on = (p_bbu < p_kcu) and (p_bbl > p_kcl)
            if prior_was_squeeze_on and not is_squeeze_on:
                is_squeeze_release = True

    squeeze_state = (
        "SQUEEZE_ON" if is_squeeze_on else (
            "SQUEEZE_RELEASE" if is_squeeze_release else "SQUEEZE_OFF"
        )
    )

    return {
        "is_squeeze_on": is_squeeze_on,
        "is_squeeze_release": is_squeeze_release,
        "squeeze_state": squeeze_state,
        "bandwidth": bollinger.get("bandwidth"),
        "calculation_status": CalculationStatus.VALID
    }


def calculate_session_vwap(
    candles: List[Dict[str, Any]],
    futures_candles: Optional[List[Dict[str, Any]]] = None,
    is_spot_index: bool = False,
    underlying_contract: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calculates Intraday Session VWAP with Weighted Standard Deviation Bands (+-1sigma, +-2sigma).
    Formulas:
    P_typical = (High + Low + Close) / 3
    VWAP = sum(P_typical * V) / sum(V)
    Weighted Variance = sum(V * (P_typical - VWAP)^2) / sum(V)
    sigma = sqrt(Weighted Variance)
    Upper_1 = VWAP + 1*sigma, Lower_1 = VWAP - 1*sigma
    Upper_2 = VWAP + 2*sigma, Lower_2 = VWAP - 2*sigma

    Guarantees:
    - If is_spot_index is True (e.g. NIFTY 50), volume is sourced strictly from futures_candles.
    - Explicitly labeled as 'NIFTY Futures VWAP' with volume_source='FUTURES_TRADED_PROXY'.
    - If volume is missing or zero, returns CalculationStatus.UNAVAILABLE_NO_VOLUME without synthetic volume.
    """
    if not candles:
        return {
            "vwap": None,
            "upper_band_1": None,
            "lower_band_1": None,
            "upper_band_2": None,
            "lower_band_2": None,
            "sigma": None,
            "total_session_volume": 0,
            "vwap_name": "NIFTY Futures VWAP" if is_spot_index else "Session VWAP",
            "volume_source": "FUTURES_TRADED_PROXY" if is_spot_index else "DIRECT_CANDLE_VOLUME",
            "calculation_status": CalculationStatus.UNAVAILABLE_NO_DATA
        }

    # If spot index, volume must come from futures proxy
    if is_spot_index:
        if not futures_candles:
            return {
                "vwap": None,
                "upper_band_1": None,
                "lower_band_1": None,
                "upper_band_2": None,
                "lower_band_2": None,
                "sigma": None,
                "total_session_volume": 0,
                "vwap_name": "NIFTY Futures VWAP",
                "volume_source": "FUTURES_TRADED_PROXY",
                "underlying_contract": underlying_contract or "UNSPECIFIED",
                "is_spot_proxy": True,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_FUTURES_FEED
            }
        source_candles = futures_candles
    else:
        source_candles = candles

    has_volume = any(c.get("volume") and int(c["volume"]) > 0 for c in source_candles)
    if not has_volume:
        return {
            "vwap": None,
            "upper_band_1": None,
            "lower_band_1": None,
            "upper_band_2": None,
            "lower_band_2": None,
            "sigma": None,
            "total_session_volume": 0,
            "vwap_name": "NIFTY Futures VWAP" if is_spot_index else "Session VWAP",
            "volume_source": "FUTURES_TRADED_PROXY" if is_spot_index else "DIRECT_CANDLE_VOLUME",
            "calculation_status": CalculationStatus.UNAVAILABLE_NO_VOLUME
        }

    cum_vol = 0
    cum_pv = 0.0
    typical_prices: List[float] = []
    volumes: List[int] = []

    for c in source_candles:
        vol = int(c.get("volume", 0) or 0)
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        cum_pv += tp * vol
        cum_vol += vol
        typical_prices.append(tp)
        volumes.append(vol)

    if cum_vol == 0:
        return {
            "vwap": None,
            "upper_band_1": None,
            "lower_band_1": None,
            "upper_band_2": None,
            "lower_band_2": None,
            "sigma": None,
            "total_session_volume": 0,
            "vwap_name": "NIFTY Futures VWAP" if is_spot_index else "Session VWAP",
            "volume_source": "FUTURES_TRADED_PROXY" if is_spot_index else "DIRECT_CANDLE_VOLUME",
            "calculation_status": CalculationStatus.UNAVAILABLE_NO_VOLUME
        }

    current_vwap = cum_pv / cum_vol
    variance_sum = sum(v * ((tp - current_vwap) ** 2) for tp, v in zip(typical_prices, volumes))
    current_variance = variance_sum / cum_vol
    current_sigma = math.sqrt(current_variance)

    return {
        "vwap": round(current_vwap, 2),
        "variance": round(current_variance, 2),
        "sigma": round(current_sigma, 2),
        "upper_band_1": round(current_vwap + current_sigma, 2),
        "lower_band_1": round(current_vwap - current_sigma, 2),
        "upper_band_2": round(current_vwap + 2.0 * current_sigma, 2),
        "lower_band_2": round(current_vwap - 2.0 * current_sigma, 2),
        "total_session_volume": cum_vol,
        "vwap_name": "NIFTY Futures VWAP" if is_spot_index else "Session VWAP",
        "volume_source": "FUTURES_TRADED_PROXY" if is_spot_index else "DIRECT_CANDLE_VOLUME",
        "underlying_contract": underlying_contract if is_spot_index else None,
        "is_spot_proxy": is_spot_index,
        "calculation_status": CalculationStatus.VALID
    }


class SessionVWAPAccumulator:
    """
    Intraday Session VWAP Accumulator with Weighted Variance & Standard Deviation Bands.

    Guarantees:
    1. Full Session Retention: Maintains session-wide sums (cum_vol, cum_pv, cum_pv2)
       so that VWAP remains exact across the entire trading day (e.g. 1,500 15s candles)
       even if older candles are evicted from memory buffers.
    2. Session Date Isolation: Automatically detects session boundaries (date change
       or crossing 09:15:00 IST) and resets accumulators. Prior-session volume NEVER
       leaks into the active session.
    3. Futures Contract Isolation: For proxy attribution, guarantees that volume is
       never mixed between different futures contract expiries within a session.
    4. Zero and Missing Volume Safety: Returns UNAVAILABLE_NO_VOLUME without synthetic
       fallbacks when volume is zero or missing.
    5. Candle-Based Approximation Notice: Typical-price (H+L+C)/3 candle-volume VWAP
       is a standard discrete approximation of trade-level tick VWAP.
    """
    def __init__(self, symbol: str, is_spot_index: bool = False):
        self.symbol = symbol.strip().upper()
        self.is_spot_index = is_spot_index
        self.session_date: Optional[date] = None
        self.cum_vol: int = 0
        self.cum_pv: float = 0.0
        self.cum_pv2: float = 0.0
        self.candle_count: int = 0
        self.underlying_contract: Optional[str] = None
        self._processed_timestamps: set = set()

    def reset(self, new_session_date: Optional[date] = None) -> None:
        self.session_date = new_session_date
        self.cum_vol = 0
        self.cum_pv = 0.0
        self.cum_pv2 = 0.0
        self.candle_count = 0
        self._processed_timestamps.clear()

    def add_candle(
        self,
        high: float,
        low: float,
        close: float,
        volume: int,
        timestamp_dt: datetime,
        contract_symbol: Optional[str] = None
    ) -> bool:
        c_date = timestamp_dt.date()
        if self.session_date is None:
            self.session_date = c_date
        elif c_date != self.session_date:
            self.reset(c_date)

        if contract_symbol:
            if self.underlying_contract is None:
                self.underlying_contract = contract_symbol
            elif self.underlying_contract != contract_symbol:
                # Contract change: reset accumulator to prevent mixing two different contract volumes
                self.reset(c_date)
                self.underlying_contract = contract_symbol

        ts_key = timestamp_dt.isoformat()
        if ts_key in self._processed_timestamps:
            return False
        self._processed_timestamps.add(ts_key)

        vol = int(volume or 0)
        tp = (float(high) + float(low) + float(close)) / 3.0
        self.cum_vol += vol
        self.cum_pv += tp * vol
        self.cum_pv2 += (tp ** 2) * vol
        self.candle_count += 1
        return True

    def get_vwap_state(self) -> Dict[str, Any]:
        if self.cum_vol <= 0:
            return {
                "vwap": None,
                "variance": None,
                "sigma": None,
                "upper_band_1": None,
                "lower_band_1": None,
                "upper_band_2": None,
                "lower_band_2": None,
                "total_session_volume": 0,
                "candle_count": self.candle_count,
                "vwap_name": "NIFTY Futures VWAP" if self.is_spot_index else "Session VWAP",
                "volume_source": "FUTURES_TRADED_PROXY" if self.is_spot_index else "DIRECT_CANDLE_VOLUME",
                "underlying_contract": self.underlying_contract if self.is_spot_index else None,
                "is_spot_proxy": self.is_spot_index,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_VOLUME
            }

        vwap = self.cum_pv / self.cum_vol
        variance = max(0.0, (self.cum_pv2 / self.cum_vol) - (vwap ** 2))
        sigma = math.sqrt(variance)

        return {
            "vwap": round(vwap, 2),
            "variance": round(variance, 2),
            "sigma": round(sigma, 2),
            "upper_band_1": round(vwap + sigma, 2),
            "lower_band_1": round(vwap - sigma, 2),
            "upper_band_2": round(vwap + 2.0 * sigma, 2),
            "lower_band_2": round(vwap - 2.0 * sigma, 2),
            "total_session_volume": self.cum_vol,
            "candle_count": self.candle_count,
            "vwap_name": "NIFTY Futures VWAP" if self.is_spot_index else "Session VWAP",
            "volume_source": "FUTURES_TRADED_PROXY" if self.is_spot_index else "DIRECT_CANDLE_VOLUME",
            "underlying_contract": self.underlying_contract if self.is_spot_index else None,
            "is_spot_proxy": self.is_spot_index,
            "calculation_status": CalculationStatus.VALID
        }


# ============================================================================
# TechnicalIndicatorService: High-Level Candle Store Consumer
# ============================================================================

class TechnicalIndicatorService:
    """
    High-level, deterministic Technical Indicator Service.
    Consumes MultiTimeframeCandleStore strictly via get_closed_candles() for confirmed values,
    and optionally get_active_candle() for developing signals without repainting.
    """

    def __init__(
        self,
        candle_store: MultiTimeframeCandleStore,
        futures_manager: Optional[FuturesContractManager] = None
    ):
        self.candle_store = candle_store
        self.futures_manager = futures_manager or FuturesContractManager()

    def get_warmup_status(self, available_bars: int, min_required: int, recommended_warmup: int) -> str:
        """Determines indicator warmup state."""
        if available_bars < min_required:
            return WarmupStatus.INSUFFICIENT_DATA
        elif available_bars < recommended_warmup:
            return WarmupStatus.PARTIAL_WARMUP
        return WarmupStatus.WARMUP_COMPLETE

    def compute_moving_averages(
        self,
        symbol: str,
        timeframe: str,
        include_developing: bool = False
    ) -> Dict[str, Any]:
        """
        Computes EMA 9, 15, 20, 50, 200, DEMA 9, TEMA 9 for specified symbol and timeframe.
        """
        closed_bars = self.candle_store.get_closed_candles(symbol, timeframe, limit=500)
        active_bar = self.candle_store.get_active_candle(symbol, timeframe) if include_developing else None

        eval_bars = list(closed_bars)
        is_closed = True
        if include_developing and active_bar:
            eval_bars.append(active_bar)
            is_closed = False

        if not eval_bars:
            return {
                "indicator_name": "MOVING_AVERAGES",
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": None,
                "is_closed": True,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_DATA,
                "warmup_status": WarmupStatus.INSUFFICIENT_DATA
            }

        closes = [float(b["close"]) for b in eval_bars]
        latest_ts = eval_bars[-1].get("timestamp") or eval_bars[-1].get("start_time")
        n_bars = len(closes)

        ema9_series = calculate_ema(closes, 9, round_output=False)
        ema15_series = calculate_ema(closes, 15, round_output=False)
        ema20_series = calculate_ema(closes, 20, round_output=False)
        ema50_series = calculate_ema(closes, 50, round_output=False)
        ema200_series = calculate_ema(closes, 200, round_output=False)

        dema9_series = calculate_dema(closes, 9, round_output=False)
        tema9_series = calculate_tema(closes, 9, round_output=False)

        ema9 = round(ema9_series[-1], 2) if ema9_series and ema9_series[-1] is not None else None
        ema15 = round(ema15_series[-1], 2) if ema15_series and ema15_series[-1] is not None else None
        ema20 = round(ema20_series[-1], 2) if ema20_series and ema20_series[-1] is not None else None
        ema50 = round(ema50_series[-1], 2) if ema50_series and ema50_series[-1] is not None else None
        ema200 = round(ema200_series[-1], 2) if ema200_series and ema200_series[-1] is not None else None

        dema9 = round(dema9_series[-1], 2) if dema9_series and dema9_series[-1] is not None else None
        tema9 = round(tema9_series[-1], 2) if tema9_series and tema9_series[-1] is not None else None

        # Determine alignment
        alignment = "MIXED"
        if ema9 is not None and ema15 is not None and ema50 is not None:
            if ema9 > ema15 > ema50:
                alignment = "BULLISH_STACK"
            elif ema9 < ema15 < ema50:
                alignment = "BEARISH_STACK"

        return {
            "indicator_name": "MOVING_AVERAGES",
            "parameters": {"periods": [9, 15, 20, 50, 200]},
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": latest_ts,
            "is_closed": is_closed,
            "values": {
                "ema9": ema9,
                "ema15": ema15,
                "ema20": ema20,
                "ema50": ema50,
                "ema200": ema200,
                "dema9": dema9,
                "tema9": tema9
            },
            "alignment": alignment,
            "calculation_status": CalculationStatus.VALID if ema9 is not None else CalculationStatus.INSUFFICIENT_DATA,
            "warmup_status": self.get_warmup_status(n_bars, min_required=9, recommended_warmup=50)
        }

    def compute_momentum(
        self,
        symbol: str,
        timeframe: str,
        include_developing: bool = False,
        roc_period: int = 14
    ) -> Dict[str, Any]:
        """
        Computes RSI (14), MACD (12, 26, 9), and ROC (14).
        """
        closed_bars = self.candle_store.get_closed_candles(symbol, timeframe, limit=500)
        active_bar = self.candle_store.get_active_candle(symbol, timeframe) if include_developing else None

        eval_bars = list(closed_bars)
        is_closed = True
        if include_developing and active_bar:
            eval_bars.append(active_bar)
            is_closed = False

        if not eval_bars:
            return {
                "indicator_name": "MOMENTUM",
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": None,
                "is_closed": True,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_DATA,
                "warmup_status": WarmupStatus.INSUFFICIENT_DATA
            }

        closes = [float(b["close"]) for b in eval_bars]
        latest_ts = eval_bars[-1].get("timestamp") or eval_bars[-1].get("start_time")
        n_bars = len(closes)

        # RSI (14)
        rsi_series = calculate_rsi(closes, 14)
        rsi_val = rsi_series[-1] if rsi_series and rsi_series[-1] is not None else None

        # MACD (12, 26, 9)
        macd_data = calculate_macd(closes, 12, 26, 9, last_bar_is_closed=is_closed, candles_metadata=eval_bars)

        # ROC
        roc_series = calculate_roc(closes, roc_period)
        roc_val = roc_series[-1] if roc_series and roc_series[-1] is not None else None

        return {
            "indicator_name": "MOMENTUM",
            "parameters": {"rsi_period": 14, "macd": [12, 26, 9], "roc_period": roc_period},
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": latest_ts,
            "is_closed": is_closed,
            "rsi": {
                "value": rsi_val,
                "warmup_status": self.get_warmup_status(n_bars, min_required=15, recommended_warmup=42),
                "calculation_status": CalculationStatus.VALID if rsi_val is not None else CalculationStatus.INSUFFICIENT_DATA
            },
            "macd": macd_data,
            "roc": {
                "value": roc_val,
                "period": roc_period,
                "calculation_status": CalculationStatus.VALID if roc_val is not None else CalculationStatus.INSUFFICIENT_DATA
            },
            "calculation_status": CalculationStatus.VALID if (rsi_val is not None and macd_data.get("macd") is not None) else CalculationStatus.INSUFFICIENT_DATA
        }

    def compute_trend_strength(
        self,
        symbol: str,
        timeframe: str,
        include_developing: bool = False
    ) -> Dict[str, Any]:
        """
        Computes Welles Wilder's ADX (14), +DI, -DI.
        """
        closed_bars = self.candle_store.get_closed_candles(symbol, timeframe, limit=500)
        active_bar = self.candle_store.get_active_candle(symbol, timeframe) if include_developing else None

        eval_bars = list(closed_bars)
        is_closed = True
        if include_developing and active_bar:
            eval_bars.append(active_bar)
            is_closed = False

        if not eval_bars:
            return {
                "indicator_name": "ADX",
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": None,
                "is_closed": True,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_DATA,
                "warmup_status": WarmupStatus.INSUFFICIENT_DATA
            }

        highs = [float(b["high"]) for b in eval_bars]
        lows = [float(b["low"]) for b in eval_bars]
        closes = [float(b["close"]) for b in eval_bars]
        latest_ts = eval_bars[-1].get("timestamp") or eval_bars[-1].get("start_time")
        n_bars = len(closes)

        adx_res = calculate_adx(highs, lows, closes, period=14)

        return {
            "indicator_name": "ADX",
            "parameters": {"period": 14},
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": latest_ts,
            "is_closed": is_closed,
            "adx": adx_res.get("adx"),
            "plus_di": adx_res.get("plus_di"),
            "minus_di": adx_res.get("minus_di"),
            "trend_strength": adx_res.get("trend_strength"),
            "calculation_status": adx_res.get("calculation_status", CalculationStatus.VALID),
            "warmup_status": self.get_warmup_status(n_bars, min_required=28, recommended_warmup=50)
        }

    def compute_volatility(
        self,
        symbol: str,
        timeframe: str,
        include_developing: bool = False
    ) -> Dict[str, Any]:
        """
        Computes Bollinger Bands (20, 2), Keltner Channels (20, 1.5), and Carter's Volatility Squeeze.
        """
        closed_bars = self.candle_store.get_closed_candles(symbol, timeframe, limit=500)
        active_bar = self.candle_store.get_active_candle(symbol, timeframe) if include_developing else None

        eval_bars = list(closed_bars)
        is_closed = True
        if include_developing and active_bar:
            eval_bars.append(active_bar)
            is_closed = False

        if not eval_bars:
            return {
                "indicator_name": "VOLATILITY",
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": None,
                "is_closed": True,
                "calculation_status": CalculationStatus.UNAVAILABLE_NO_DATA,
                "warmup_status": WarmupStatus.INSUFFICIENT_DATA
            }

        highs = [float(b["high"]) for b in eval_bars]
        lows = [float(b["low"]) for b in eval_bars]
        closes = [float(b["close"]) for b in eval_bars]
        latest_ts = eval_bars[-1].get("timestamp") or eval_bars[-1].get("start_time")
        n_bars = len(closes)

        # Current Bar
        bollinger = calculate_bollinger_bands(closes, period=20, num_std=2.0)
        keltner = calculate_keltner_channels(highs, lows, closes, ema_period=20, atr_period=10, multiplier=1.5)

        # Prior Bar for Squeeze Release detection
        prior_bollinger = None
        prior_keltner = None
        if len(closes) >= 21:
            prior_bollinger = calculate_bollinger_bands(closes[:-1], period=20, num_std=2.0)
            prior_keltner = calculate_keltner_channels(highs[:-1], lows[:-1], closes[:-1], ema_period=20, atr_period=10, multiplier=1.5)

        squeeze = detect_volatility_squeeze(bollinger, keltner, prior_bollinger, prior_keltner)
        atr_val = calculate_atr(highs, lows, closes, period=14)

        return {
            "indicator_name": "VOLATILITY",
            "parameters": {"bollinger": [20, 2.0], "keltner": [20, 1.5, 10], "atr": 14},
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": latest_ts,
            "is_closed": is_closed,
            "atr": atr_val,
            "bollinger": bollinger,
            "keltner": keltner,
            "squeeze": squeeze,
            "calculation_status": bollinger.get("calculation_status", CalculationStatus.VALID),
            "warmup_status": self.get_warmup_status(n_bars, min_required=20, recommended_warmup=40)
        }

    def compute_session_vwap(
        self,
        symbol: str,
        timeframe: str = Timeframe.M5,
        current_dt: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Computes Intraday Session VWAP with Standard Deviation Bands at +-1sigma and +-2sigma.
        For NIFTY spot ('NIFTY 50'), associates the near-month futures contract to source volume.
        """
        sym_clean = symbol.strip().upper()
        is_spot_index = sym_clean in ("NIFTY 50", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

        spot_candles = self.candle_store.get_closed_candles(sym_clean, timeframe, limit=None)
        futures_candles: Optional[List[Dict[str, Any]]] = None
        underlying_contract: Optional[str] = None

        if is_spot_index:
            active_info = self.futures_manager.get_active_contracts(symbol=sym_clean, current_dt=current_dt)
            near_exp = active_info["near_expiry"]
            fut_sym = self.futures_manager.format_trading_symbol(sym_clean, near_exp)
            underlying_contract = fut_sym
            futures_candles = self.candle_store.get_closed_candles(fut_sym, timeframe, limit=None)

        vwap_res = calculate_session_vwap(
            candles=spot_candles,
            futures_candles=futures_candles,
            is_spot_index=is_spot_index,
            underlying_contract=underlying_contract
        )

        latest_ts = spot_candles[-1].get("timestamp") if spot_candles else None

        return {
            "indicator_name": "SESSION_VWAP",
            "parameters": {"bands_sigma": [1.0, 2.0]},
            "symbol": sym_clean,
            "timeframe": timeframe,
            "timestamp": latest_ts,
            "is_closed": True,
            "vwap": vwap_res.get("vwap"),
            "variance": vwap_res.get("variance"),
            "upper_band_1": vwap_res.get("upper_band_1"),
            "lower_band_1": vwap_res.get("lower_band_1"),
            "upper_band_2": vwap_res.get("upper_band_2"),
            "lower_band_2": vwap_res.get("lower_band_2"),
            "sigma": vwap_res.get("sigma"),
            "total_session_volume": vwap_res.get("total_session_volume", 0),
            "vwap_name": vwap_res.get("vwap_name"),
            "volume_source": vwap_res.get("volume_source"),
            "underlying_contract": vwap_res.get("underlying_contract"),
            "is_spot_proxy": vwap_res.get("is_spot_proxy", False),
            "calculation_status": vwap_res.get("calculation_status", CalculationStatus.VALID)
        }

    def compute_all_indicators(
        self,
        symbol: str,
        timeframe: str,
        include_developing: bool = False
    ) -> Dict[str, Any]:
        """
        Computes the complete Phase 2A technical indicator suite for an instrument and timeframe.
        """
        ma = self.compute_moving_averages(symbol, timeframe, include_developing=include_developing)
        momentum = self.compute_momentum(symbol, timeframe, include_developing=include_developing)
        trend = self.compute_trend_strength(symbol, timeframe, include_developing=include_developing)
        volatility = self.compute_volatility(symbol, timeframe, include_developing=include_developing)
        vwap = self.compute_session_vwap(symbol, timeframe)

        return {
            "symbol": symbol.strip().upper(),
            "timeframe": timeframe,
            "timestamp": ma.get("timestamp"),
            "is_closed": not include_developing,
            "moving_averages": ma,
            "momentum": momentum,
            "trend_strength": trend,
            "volatility": volatility,
            "session_vwap": vwap
        }

    def compute_multi_timeframe_suite(
        self,
        symbol: str,
        timeframes: Optional[List[str]] = None,
        include_developing: bool = False
    ) -> Dict[str, Any]:
        """
        Computes indicators across all requested intraday timeframes (15s, 1m, 3m, 5m, 15m).
        """
        tfs = timeframes or [Timeframe.S15, Timeframe.M1, Timeframe.M3, Timeframe.M5, Timeframe.M15]
        results = {}
        for tf in tfs:
            results[tf] = self.compute_all_indicators(symbol, tf, include_developing=include_developing)
        return {
            "symbol": symbol.strip().upper(),
            "multi_timeframe_indicators": results
        }
