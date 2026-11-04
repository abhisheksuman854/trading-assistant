"""
TradeLens AI - Technical Analysis Engine
High-precision, pure-Python technical indicators and price action patterns.
Supports EMA ribbons, RSI, MACD, ATR, Pivot levels, RVOL, and Candlestick detection.
"""
from typing import List, Dict, Any, Optional
import math


def calculate_sma(prices: List[float], period: int) -> List[Optional[float]]:
    """Calculates Simple Moving Average."""
    sma = []
    for i in range(len(prices)):
        if i + 1 < period:
            sma.append(None)
        else:
            window = prices[i + 1 - period : i + 1]
            sma.append(round(sum(window) / period, 2))
    return sma


def calculate_ema(prices: List[float], period: int) -> List[Optional[float]]:
    """Calculates Exponential Moving Average with standard multiplier 2 / (N + 1)."""
    if len(prices) < period:
        return [None] * len(prices)
    
    ema = [None] * (period - 1)
    # First EMA is SMA of first 'period' values
    first_sma = sum(prices[:period]) / period
    ema.append(round(first_sma, 2))
    multiplier = 2.0 / (period + 1.0)
    
    for price in prices[period:]:
        prev = ema[-1]
        current_ema = (price - prev) * multiplier + prev
        ema.append(round(current_ema, 2))
    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> List[Optional[float]]:
    """Calculates Wilder's Relative Strength Index (RSI)."""
    if len(prices) <= period:
        return [None] * len(prices)
    
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    
    rsi = [None] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rsi.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi.append(round(100.0 - (100.0 / (1.0 + rs)), 2))
        
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(round(100.0 - (100.0 / (1.0 + rs)), 2))
            
    return rsi


def calculate_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Any]:
    """Calculates MACD Line, Signal Line, and Histogram."""
    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        if f is not None and s is not None:
            macd_line.append(round(f - s, 2))
        else:
            macd_line.append(None)
            
    # Extract non-None values to compute signal EMA
    valid_macd = [v for v in macd_line if v is not None]
    if len(valid_macd) >= signal:
        sig_ema = calculate_ema(valid_macd, signal)
        signal_line = [None] * (len(macd_line) - len(valid_macd) + signal - 1) + sig_ema[signal - 1:]
    else:
        signal_line = [None] * len(macd_line)
        
    histogram = []
    for m, s in zip(macd_line, signal_line):
        if m is not None and s is not None:
            histogram.append(round(m - s, 2))
        else:
            histogram.append(None)
            
    return {
        "macd": macd_line[-1] if macd_line else 0.0,
        "signal": signal_line[-1] if signal_line else 0.0,
        "hist": histogram[-1] if histogram else 0.0,
        "is_bullish_cross": (histogram[-1] or 0) > 0 and (histogram[-2] or 0) <= 0 if len(histogram) > 1 else False
    }


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """Calculates Average True Range."""
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
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 2)


def calculate_pivots(high: float, low: float, close: float) -> Dict[str, float]:
    """Calculates Classic Pivot Points (Support and Resistance)."""
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
    """Identifies key candlestick reversal & continuation patterns."""
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
    prev_body = abs(prev_c - prev_o)
    
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


def evaluate_technicals(candles: List[Dict[str, float]]) -> Dict[str, Any]:
    """
    Evaluates complete technical health:
    Takes list of dicts: [{'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}, ...]
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
    
    # EMAs
    ema9 = calculate_ema(closes, 9)[-1] or cmp_price
    ema20 = calculate_ema(closes, 20)[-1] or cmp_price
    ema50 = calculate_ema(closes, 50)[-1] or cmp_price
    ema200 = calculate_ema(closes, 200)[-1] if len(closes) >= 200 else ema50
    
    # RSI & MACD
    rsi_list = calculate_rsi(closes, 14)
    rsi = rsi_list[-1] if rsi_list and rsi_list[-1] is not None else 50.0
    macd_data = calculate_macd(closes)
    
    # ATR & Pivots
    atr = calculate_atr(highs, lows, closes, 14)
    pivots = calculate_pivots(highs[-1], lows[-1], closes[-1])
    
    # Relative Volume (RVOL) vs 20-day average
    vol_sma_list = calculate_sma(volumes, 20)
    vol_sma20 = vol_sma_list[-1] or (sum(volumes) / max(1, len(volumes)))
    rvol = round(volumes[-1] / vol_sma20, 2) if vol_sma20 > 0 else 1.0
    
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
        if macd_data["is_bullish_cross"]:
            score += 5
            bullish_factors.append("Fresh MACD bullish crossover")
    else:
        risk_factors.append("MACD histogram negative")
        
    # 4. Volume / RVOL (Max 25 pts)
    if rvol >= 1.8:
        score += 25
        bullish_factors.append(f"Institutional volume surge: RVOL is {rvol}x 20-day average")
    elif rvol >= 1.2:
        score += 15
        bullish_factors.append(f"Healthy volume: RVOL {rvol}x")
    else:
        risk_factors.append(f"Subdued volume: RVOL {rvol}x")
        
    # Chasing Check: Distance from 20 EMA
    dist_from_ema20_pct = round(((cmp_price - ema20) / ema20) * 100, 2)
    is_overextended = dist_from_ema20_pct > 7.0 or (cmp_price - ema20) > (2.5 * atr)
    if is_overextended:
        risk_factors.append(f"Overextended: Price is {dist_from_ema20_pct}% above 20 EMA. High pullback risk.")
        
    return {
        "score": min(100, score),
        "cmp": cmp_price,
        "ema": {"ema9": ema9, "ema20": ema20, "ema50": ema50, "ema200": ema200},
        "rsi": rsi,
        "macd": macd_data,
        "atr": atr,
        "rvol": rvol,
        "pivots": pivots,
        "patterns": patterns,
        "dist_from_ema20_pct": dist_from_ema20_pct,
        "is_overextended": is_overextended,
        "bullish_factors": bullish_factors,
        "risk_factors": risk_factors
    }

