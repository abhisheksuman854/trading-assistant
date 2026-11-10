"""
TradeLens AI - Technical Analysis Engine
High-precision, pure-Python technical indicators and price action patterns.
Supports EMA ribbons, RSI, MACD, ATR, Pivot levels, RVOL, and Candlestick detection.
Ensures full IEEE 754 64-bit float precision throughout recursive calculations.
Separates developing (forming bar) and confirmed (closed bar) crossover states.
Calculates Support/Resistance pivots strictly from completed prior session/bar.
"""
from typing import List, Dict, Any, Optional, Tuple
import math


def calculate_sma(prices: List[float], period: int, round_output: bool = True) -> List[Optional[float]]:
    """Calculates Simple Moving Average maintaining full internal float precision."""
    if len(prices) < period:
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
    Calculates Exponential Moving Average with standard multiplier 2 / (N + 1).
    Maintains full 64-bit IEEE floating-point precision throughout recursion.
    Rounds only for final presentation if round_output is True.
    """
    if len(prices) < period:
        return [None] * len(prices)
    
    ema_raw: List[Optional[float]] = [None] * (period - 1)
    # First EMA is exact arithmetic mean of first 'period' values
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
    Maintains full internal 64-bit float precision.
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
    Maintains full internal 64-bit float precision.
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


def calculate_roc(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Calculates Rate of Change (ROC) momentum oscillator.
    Formula: ROC_t = ((Price_t - Price_{t-N}) / Price_{t-N}) * 100
    """
    if len(prices) <= period:
        return [None] * len(prices)

    roc: List[Optional[float]] = [None] * period
    for i in range(period, len(prices)):
        denom = prices[i - period]
        if denom == 0.0:
            roc.append(0.0)
        else:
            roc.append(round(((prices[i] - denom) / denom) * 100.0, 2))
    return roc


def calculate_session_vwap(
    candles: List[Dict[str, Any]],
    reset_daily: bool = True
) -> Dict[str, Any]:
    """
    Calculates intraday Session VWAP with Standard Deviation Bands (+-1sigma, +-2sigma).
    Formula:
    P_typical = (High + Low + Close) / 3
    VWAP = sum(P_typical * Volume) / sum(Volume)
    sigma = sqrt(sum(Volume * (P_typical - VWAP)^2) / sum(Volume))
    Upper_1 = VWAP + 1*sigma, Lower_1 = VWAP - 1*sigma
    Upper_2 = VWAP + 2*sigma, Lower_2 = VWAP - 2*sigma

    Guarantees:
    - Anchored to the trading session (resets at 09:15:00 IST or session date change).
    - If volume is zero or missing (e.g. spot indices), returns UNAVAILABLE without inventing volume.
    """
    if not candles:
        return {
            "vwap": None,
            "upper_band_1": None,
            "lower_band_1": None,
            "upper_band_2": None,
            "lower_band_2": None,
            "sigma": None,
            "vwap_status": "UNAVAILABLE_NO_DATA"
        }

    # Verify if volume exists
    has_volume = any(c.get("volume") and c["volume"] > 0 for c in candles)
    if not has_volume:
        return {
            "vwap": None,
            "upper_band_1": None,
            "lower_band_1": None,
            "upper_band_2": None,
            "lower_band_2": None,
            "sigma": None,
            "vwap_status": "UNAVAILABLE_NO_VOLUME"
        }

    # Group candles by session date if reset_daily is True
    # If timestamps have date, isolate the latest session
    latest_session_candles = candles
    if reset_daily and candles and "timestamp" in candles[0]:
        dates_seen = []
        candles_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for c in candles:
            d_str = str(c.get("timestamp", ""))[:10]
            if d_str not in candles_by_date:
                dates_seen.append(d_str)
                candles_by_date[d_str] = []
            candles_by_date[d_str].append(c)
        if dates_seen:
            latest_session_candles = candles_by_date[dates_seen[-1]]

    cum_vol = 0
    cum_pv = 0.0
    typical_prices: List[float] = []
    volumes: List[int] = []

    for c in latest_session_candles:
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
            "vwap_status": "UNAVAILABLE_ZERO_VOLUME"
        }

    current_vwap = cum_pv / cum_vol

    # Standard deviation calculation
    variance_sum = sum(v * ((tp - current_vwap) ** 2) for tp, v in zip(typical_prices, volumes))
    current_sigma = math.sqrt(variance_sum / cum_vol)

    return {
        "vwap": round(current_vwap, 2),
        "upper_band_1": round(current_vwap + current_sigma, 2),
        "lower_band_1": round(current_vwap - current_sigma, 2),
        "upper_band_2": round(current_vwap + 2.0 * current_sigma, 2),
        "lower_band_2": round(current_vwap - 2.0 * current_sigma, 2),
        "sigma": round(current_sigma, 2),
        "total_session_volume": cum_vol,
        "vwap_status": "VALID_SESSION_VWAP"
    }


def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """
    Calculates Wilder's Relative Strength Index (RSI) using Wilder's smoothing (RMA).
    Maintains full float precision throughout recursion.
    """
    if len(prices) <= period:
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


def get_rsi_warmup_status(price_count: int, period: int = 14) -> Dict[str, Any]:
    """
    Exposes RSI initialization and warmup status.
    Wilder's RMA requires ~3 * period (42 bars) to fully stabilize.
    """
    if price_count <= period:
        return {
            "status": "INSUFFICIENT_DATA",
            "is_warmup_complete": False,
            "bars_available": price_count,
            "min_required": period + 1,
            "recommended_warmup": period * 3
        }
    elif price_count < period * 3:
        return {
            "status": "PARTIAL_WARMUP",
            "is_warmup_complete": False,
            "bars_available": price_count,
            "min_required": period + 1,
            "recommended_warmup": period * 3
        }
    return {
        "status": "WARMUP_COMPLETE",
        "is_warmup_complete": True,
        "bars_available": price_count,
        "min_required": period + 1,
        "recommended_warmup": period * 3
    }


def calculate_macd(
    prices: List[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    last_bar_is_closed: bool = False,
    candles_metadata: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Calculates MACD Line, Signal Line, and Histogram.
    Maintains full float precision internally.
    Uses explicit candle closure metadata (is_closed, timestamps) to determine
    confirmed vs developing crossover states instead of relying solely on array positions.
    """
    # If candle metadata is supplied, extract explicit closure state
    if candles_metadata and len(candles_metadata) > 0:
        last_c = candles_metadata[-1]
        if "is_closed" in last_c:
            last_bar_is_closed = bool(last_c["is_closed"])

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
            
    # Crossover detection governed by candle closure metadata:
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
            
            # Confirmed crossover on closed bar [-2] relative to [-3]
            if h_closed is not None and h_prev is not None:
                if h_closed > 0 and h_prev <= 0:
                    is_confirmed_bull = True
                elif h_closed < 0 and h_prev >= 0:
                    is_confirmed_bear = True
                confirmed_idx = len(histogram_raw) - 2
                if candles_metadata and len(candles_metadata) >= 2:
                    confirmed_ts = candles_metadata[-2].get("timestamp")
                    
            # Developing crossover on active bar [-1] relative to [-2]
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

    return {
        "macd": last_macd,
        "signal": last_sig,
        "hist": last_hist,
        "is_bullish_cross": is_confirmed_bull,
        "is_confirmed_bullish_cross": is_confirmed_bull,
        "is_confirmed_bearish_cross": is_confirmed_bear,
        "is_developing_bullish_cross": is_developing_bull,
        "is_developing_bearish_cross": is_developing_bear,
        "last_bar_is_closed": last_bar_is_closed,
        "confirmed_candle_index": confirmed_idx,
        "confirmed_candle_timestamp": confirmed_ts,
        "crossover_status": (
            "CONFIRMED_BULLISH" if is_confirmed_bull else (
                "CONFIRMED_BEARISH" if is_confirmed_bear else (
                    "DEVELOPING_BULLISH" if is_developing_bull else (
                        "DEVELOPING_BEARISH" if is_developing_bear else "NO_CROSSOVER"
                    )
                )
            )
        )
    }


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Calculates Average True Range using Wilder's smoothing."""
    if len(closes) < 2:
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
    
    # Wilder's smoothing
    atr = sum(tr_list[:period]) / float(period)
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / float(period)
    return round(atr, 2)


def calculate_bollinger_bands(
    prices: List[float],
    period: int = 20,
    num_std: float = 2.0
) -> Dict[str, Any]:
    """
    Calculates Bollinger Bands (Middle, Upper, Lower, Bandwidth, %B).
    Formula:
    Middle = SMA(period)
    sigma = sqrt(sum((P - Middle)^2) / period)
    Upper = Middle + num_std * sigma
    Lower = Middle - num_std * sigma
    Bandwidth = (Upper - Lower) / Middle
    %B = (Close - Lower) / (Upper - Lower)
    """
    if len(prices) < period:
        return {
            "middle": None,
            "upper": None,
            "lower": None,
            "bandwidth": None,
            "pct_b": None,
            "sigma": None,
            "status": "INSUFFICIENT_DATA"
        }

    sma_list = calculate_sma(prices, period, round_output=False)
    middle = sma_list[-1]
    if middle is None:
        return {
            "middle": None, "upper": None, "lower": None,
            "bandwidth": None, "pct_b": None, "sigma": None,
            "status": "INSUFFICIENT_DATA"
        }

    recent_window = prices[-period:]
    variance = sum((p - middle) ** 2 for p in recent_window) / float(period)
    sigma = math.sqrt(variance)

    upper = middle + num_std * sigma
    lower = middle - num_std * sigma
    bw = (upper - lower) / max(0.0001, middle)

    last_price = prices[-1]
    denom = upper - lower
    pct_b = (last_price - lower) / denom if denom > 0 else 0.5

    return {
        "middle": round(middle, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "bandwidth": round(bw, 4),
        "pct_b": round(pct_b, 4),
        "sigma": round(sigma, 2),
        "status": "VALID_BOLLINGER_BANDS"
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
            "status": "INSUFFICIENT_DATA"
        }

    ema_list = calculate_ema(closes, ema_period, round_output=False)
    middle = ema_list[-1]
    if middle is None:
        return {
            "middle": None, "upper": None, "lower": None,
            "atr": None,
            "status": "INSUFFICIENT_DATA"
        }

    atr = calculate_atr(highs, lows, closes, atr_period)
    upper = middle + multiplier * atr
    lower = middle - multiplier * atr

    return {
        "middle": round(middle, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "atr": round(atr, 2),
        "status": "VALID_KELTNER_CHANNELS"
    }


def detect_volatility_squeeze(
    bollinger: Dict[str, Any],
    keltner: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Detects John Carter's Volatility Squeeze (TTM Squeeze).
    Squeeze ON: Bollinger Bands contract inside Keltner Channels
      (BB Upper < KC Upper and BB Lower > KC Lower).
    Squeeze OFF / Fired: Bollinger Bands expand outside Keltner Channels.
    """
    if (
        bollinger.get("status") != "VALID_BOLLINGER_BANDS"
        or keltner.get("status") != "VALID_KELTNER_CHANNELS"
        or bollinger.get("upper") is None
        or keltner.get("upper") is None
    ):
        return {
            "is_squeeze_on": False,
            "squeeze_state": "UNAVAILABLE",
            "bandwidth": None
        }

    bb_upper = bollinger["upper"]
    bb_lower = bollinger["lower"]
    kc_upper = keltner["upper"]
    kc_lower = keltner["lower"]

    is_squeeze_on = (bb_upper < kc_upper) and (bb_lower > kc_lower)

    return {
        "is_squeeze_on": is_squeeze_on,
        "squeeze_state": "SQUEEZE_ON_COMPRESSION" if is_squeeze_on else "SQUEEZE_OFF_EXPANSION",
        "bandwidth": bollinger.get("bandwidth")
    }


def calculate_adx(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14
) -> Dict[str, Any]:
    """
    Calculates Welles Wilder's Average Directional Index (ADX) and Directional Movement (+DI, -DI).
    Maintains full IEEE float precision throughout recursion.
    Evaluates trend strength:
    - ADX >= 25: Strong directional trend.
    - ADX < 20: Weak trend / consolidation / chop.
    - 20 <= ADX < 25: Developing / transition.
    """
    if len(closes) < period * 2:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "INSUFFICIENT_DATA",
            "status": "INSUFFICIENT_DATA"
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

        # True Range
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

        # Directional Movement
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

    # Wilder's initial smoothing for first 'period' bars
    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    dx_list = []
    for i in range(period, len(tr_list)):
        # Wilder's smoothing formula: Sm_t = Sm_{t-1} - (Sm_{t-1} / N) + Val_t
        smoothed_tr = smoothed_tr - (smoothed_tr / float(period)) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / float(period)) + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / float(period)) + minus_dm_list[i]

        p_di = 100.0 * (smoothed_plus_dm / max(0.0001, smoothed_tr))
        m_di = 100.0 * (smoothed_minus_dm / max(0.0001, smoothed_tr))

        di_sum = p_di + m_di
        dx = 100.0 * (abs(p_di - m_di) / max(0.0001, di_sum))
        dx_list.append((dx, p_di, m_di))

    if len(dx_list) < period:
        return {
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "INSUFFICIENT_DATA",
            "status": "INSUFFICIENT_DATA"
        }

    # ADX is Wilder's average of DX values
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
        "status": "VALID_ADX"
    }


def calculate_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Calculates Classic Floor Pivot Points (Support and Resistance).
    CRITICAL: Must be called with PREVIOUS COMPLETED SESSION or PREVIOUS COMPLETED BAR values.
    """
    p = (high + low + close) / 3.0
    r1 = 2 * p - low
    s1 = 2 * p - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    r3 = high + 2 * (p - low)
    s3 = low - 2 * (high - p)
    return {
        "pivot": round(p, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "r3": round(r3, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "s3": round(s3, 2),
    }


def analyze_candlesticks(opens: List[float], highs: List[float], lows: List[float], closes: List[float]) -> List[str]:
    """Identifies key candlestick reversal & continuation patterns on completed bars."""
    patterns = []
    if len(closes) < 3:
        return patterns
        
    c = closes[-1]
    o = opens[-1]
    h = highs[-1]
    l = lows[-1]
    body = abs(c - o)
    total_range = h - l or 0.0001
    
    prev_c = closes[-2]
    prev_o = opens[-2]
    
    # Hammer / Bullish Pin Bar (Long lower shadow, small body near top)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    if lower_shadow >= 2 * body and upper_shadow <= 0.3 * body and body / total_range >= 0.15:
        patterns.append("Hammer / Bullish Pin Bar")
        
    # Bullish Engulfing
    if prev_c < prev_o and c > o and c > prev_o and o < prev_c:
        patterns.append("Bullish Engulfing")
        
    # Bearish Engulfing
    if prev_c > prev_o and c < o and c < prev_o and o > prev_c:
        patterns.append("Bearish Engulfing")
        
    # Inside Bar (consolidation)
    if h <= highs[-2] and l >= lows[-2]:
        patterns.append("Inside Bar (Volatility Compression)")
        
    return patterns


def resolve_daily_pivots(
    candles: List[Dict[str, Any]],
    prior_session_ohlc: Optional[Dict[str, float]] = None
) -> Tuple[Optional[Dict[str, float]], str, str]:
    """
    Resolves Daily Classic Floor Pivots strictly from completed full-day trading session data.
    Guarantees that an arbitrary intraday (e.g. 1m or 5m) bar is NEVER used as a daily pivot.
    Returns (pivots_dict, pivot_source, pivot_status).
    """
    if prior_session_ohlc and "high" in prior_session_ohlc and "low" in prior_session_ohlc and "close" in prior_session_ohlc:
        pivots = calculate_pivots(prior_session_ohlc["high"], prior_session_ohlc["low"], prior_session_ohlc["close"])
        return pivots, "PRIOR_SESSION_OHLC", "VALID_DAILY_PIVOTS"

    if not candles:
        return None, "NONE", "UNAVAILABLE_NO_CANDLE_DATA"

    # Check if candles contain timestamps with distinct dates
    dates_seen = []
    candles_by_date: Dict[str, List[Dict[str, Any]]] = {}
    
    for c in candles:
        ts = c.get("timestamp")
        if ts:
            d_str = str(ts)[:10]  # Extracts YYYY-MM-DD
            if d_str not in candles_by_date:
                dates_seen.append(d_str)
                candles_by_date[d_str] = []
            candles_by_date[d_str].append(c)

    if len(dates_seen) >= 2:
        # Latest date is current session; previous date is completed prior session
        prior_date = dates_seen[-2]
        prior_bars = candles_by_date[prior_date]
        if prior_bars:
            p_high = max(b["high"] for b in prior_bars)
            p_low = min(b["low"] for b in prior_bars)
            p_close = prior_bars[-1]["close"]
            pivots = calculate_pivots(p_high, p_low, p_close)
            return pivots, "AGGREGATED_PRIOR_INTRADAY_SESSION", "VALID_DAILY_PIVOTS"

    # If all candles belong to a single intraday date with timestamps
    if len(dates_seen) == 1 and ("T" in str(candles[0].get("timestamp", "")) or ":" in str(candles[0].get("timestamp", ""))):
        return None, "UNAVAILABLE", "UNAVAILABLE_MISSING_PRIOR_SESSION_DATA"

    # Fallback for daily candle series without timestamps or explicit daily timeframe
    is_explicit_daily = any(c.get("timeframe") in ("1d", "1D", "D") for c in candles)
    if is_explicit_daily or (len(candles) >= 2 and not dates_seen):
        last_closed = bool(candles[-1].get("is_closed", False))
        ref_idx = -1 if last_closed else -2
        if abs(ref_idx) <= len(candles):
            pivots = calculate_pivots(candles[ref_idx]["high"], candles[ref_idx]["low"], candles[ref_idx]["close"])
            source = "PRIOR_DAILY_CANDLE" if is_explicit_daily else "COMPLETED_BAR_FALLBACK"
            status = "VALID_DAILY_PIVOTS" if is_explicit_daily else "CAUTION_ASSUMED_DAILY_BAR"
            return pivots, source, status

    return None, "UNAVAILABLE", "UNAVAILABLE_MISSING_PRIOR_SESSION_DATA"


def evaluate_technicals(
    candles: List[Dict[str, Any]],
    prior_session_ohlc: Optional[Dict[str, float]] = None
) -> Dict[str, Any]:
    """
    Evaluates complete technical health.
    Takes list of dicts: [{'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}, ...]
    prior_session_ohlc: Optional prior completed session {'high': float, 'low': float, 'close': float}.
    Returns complete indicator suite, score (0-100), and setup signals.
    """
    if not candles:
        return {"score": 50, "summary": "Insufficient data"}
        
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    volumes = [c.get("volume", 0) for c in candles]
    
    cmp_price = closes[-1]
    
    # EMAs maintaining full float precision
    ema9 = calculate_ema(closes, 9)[-1] or cmp_price
    ema15 = calculate_ema(closes, 15)[-1] or cmp_price
    ema20 = calculate_ema(closes, 20)[-1] or cmp_price
    ema50 = calculate_ema(closes, 50)[-1] or cmp_price
    ema200 = calculate_ema(closes, 200)[-1] if len(closes) >= 200 else ema50

    if ema9 > ema15 > ema50:
        ema_alignment = "BULLISH_STACK"
    elif ema9 < ema15 < ema50:
        ema_alignment = "BEARISH_STACK"
    else:
        ema_alignment = "MIXED"

    # Volatility Indicators: Bollinger Bands, Keltner Channels & Squeeze
    bollinger = calculate_bollinger_bands(closes, period=20, num_std=2.0)
    keltner = calculate_keltner_channels(highs, lows, closes, ema_period=20, atr_period=10, multiplier=1.5)
    squeeze = detect_volatility_squeeze(bollinger, keltner)

    # Trend Strength: Welles Wilder's ADX (14)
    adx_data = calculate_adx(highs, lows, closes, period=14)

    # Session VWAP & Bands
    vwap_data = calculate_session_vwap(candles)
    
    # RSI & Warmup Metadata
    rsi_list = calculate_rsi(closes, 14)
    rsi = rsi_list[-1] if rsi_list and rsi_list[-1] is not None else 50.0
    rsi_warmup = get_rsi_warmup_status(len(closes), 14)
    
    # MACD governed by explicit candle closure metadata
    last_bar_closed = bool(candles[-1].get("is_closed", False)) if candles else False
    macd_data = calculate_macd(closes, last_bar_is_closed=last_bar_closed, candles_metadata=candles)
    
    # ATR
    atr = calculate_atr(highs, lows, closes, 14)
    
    # Daily Classic Pivots resolved with strict session-level validation
    pivots, pivot_source, pivot_status = resolve_daily_pivots(candles, prior_session_ohlc=prior_session_ohlc)
    
    # Volume & RVOL (Handles Spot Indices vs Equities cleanly)
    is_zero_volume_index = all((v == 0 or v is None) for v in volumes)
    if is_zero_volume_index:
        rvol = None
        rvol_status = "NOT_APPLICABLE_FOR_SPOT_INDEX"
        vol_sma20 = None
    elif len(volumes) >= 2:
        completed_vols = volumes[:-1]
        vol_sma_list = calculate_sma(completed_vols, min(20, len(completed_vols)))
        vol_sma20 = vol_sma_list[-1] or (sum(completed_vols) / max(1, len(completed_vols)))
        rvol = round(volumes[-2] / vol_sma20, 2) if vol_sma20 > 0 else 1.0
        rvol_status = "CONFIRMED_COMPLETED_BAR"
    else:
        vol_sma20 = volumes[0] if volumes else 0
        rvol = 1.0
        rvol_status = "INSUFFICIENT_VOLUME_HISTORY"
    
    patterns = analyze_candlesticks(opens, highs, lows, closes)
    
    # Technical Health Scoring (0 - 100)
    score = 0
    bullish_factors = []
    risk_factors = []
    
    # 1. EMA Stack (Max 30 pts)
    if cmp_price > ema20:
        score += 10
        bullish_factors.append(f"Price above 20 EMA (₹{ema20})")
    else:
        risk_factors.append(f"Price below 20 EMA (₹{ema20})")
        
    if ema20 > ema50:
        score += 10
        bullish_factors.append("20 EMA stacked above 50 EMA (Short-term trend up)")
    if cmp_price > ema200:
        score += 10
        bullish_factors.append("Price above 200 EMA (Macro Bullish)")
    else:
        risk_factors.append("Price below 200 EMA (Macro Bearish)")
        
    # 2. RSI (Max 25 pts)
    if 55 <= rsi <= 70:
        score += 25
        bullish_factors.append(f"RSI in optimal bullish momentum zone ({rsi})")
    elif 45 <= rsi < 55:
        score += 15
        bullish_factors.append(f"RSI neutral-constructive ({rsi})")
    elif rsi > 75:
        score += 8
        risk_factors.append(f"RSI Overbought ({rsi}) - Chasing risk")
    elif rsi < 35:
        score += 5
        risk_factors.append(f"RSI Weak / Oversold ({rsi})")
        
    # 3. MACD (Max 20 pts)
    if macd_data["hist"] > 0:
        score += 15
        bullish_factors.append("MACD histogram positive")
        if macd_data["is_confirmed_bullish_cross"]:
            score += 5
            bullish_factors.append("Confirmed MACD bullish crossover on closed candle")
        elif macd_data["is_developing_bullish_cross"]:
            bullish_factors.append("Developing MACD bullish crossover (awaiting candle close)")
    else:
        risk_factors.append("MACD histogram negative")
        
    # 4. Volume / RVOL (Max 25 pts)
    if rvol is not None:
        if rvol >= 1.8:
            score += 25
            bullish_factors.append(f"Institutional volume surge: RVOL is {rvol}x 20-period average")
        elif rvol >= 1.2:
            score += 15
            bullish_factors.append(f"Healthy volume: RVOL {rvol}x")
        else:
            risk_factors.append(f"Subdued volume: RVOL {rvol}x")
    else:
        # Index spot without direct volume
        score += 15  # Neutral allocation
        bullish_factors.append("Volume: Spot index (monitor NIFTY Futures for traded volume)")
        
    # Chasing Check: Distance from 20 EMA
    dist_from_ema20_pct = round(((cmp_price - ema20) / ema20) * 100, 2)
    is_overextended = dist_from_ema20_pct > 7.0 or (cmp_price - ema20) > (2.5 * atr)
    if is_overextended:
        risk_factors.append(f"Overextended: Price is {dist_from_ema20_pct}% above 20 EMA. High pullback risk.")
        
    return {
        "score": min(100, score),
        "cmp": cmp_price,
        "ema": {"ema9": ema9, "ema15": ema15, "ema20": ema20, "ema50": ema50, "ema200": ema200},
        "ema_alignment": ema_alignment,
        "rsi": rsi,
        "rsi_warmup": rsi_warmup,
        "macd": macd_data,
        "atr": atr,
        "bollinger": bollinger,
        "keltner": keltner,
        "squeeze": squeeze,
        "adx": adx_data,
        "vwap": vwap_data,
        "rvol": rvol,
        "rvol_status": rvol_status,
        "pivots": pivots,
        "pivot_source": pivot_source,
        "pivot_status": pivot_status,
        "patterns": patterns,
        "dist_from_ema20_pct": dist_from_ema20_pct,
        "is_overextended": is_overextended,
        "bullish_factors": bullish_factors,
        "risk_factors": risk_factors
    }


