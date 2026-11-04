"""
TradeLens AI - Yahoo Finance Live Data Provider
Pulls live & delayed OHLCV candles for any NSE ticker (e.g. RELIANCE.NS, NESTLEIND.NS)
Completely free, no login or API keys required.
"""
from typing import List, Dict, Any, Optional
from .base import MarketDataProvider


class YahooFinanceProvider(MarketDataProvider):
    def get_candles(self, symbol: str, interval: str = "1d", limit: int = 60) -> List[Dict[str, float]]:
        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        try:
            import yfinance as yf
            ticker = yf.Ticker(ticker_sym)
            df = ticker.history(period="3mo", interval=interval)
            
            candles = []
            for idx, row in df.iterrows():
                candles.append({
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"])
                })
            return candles[-limit:]
        except Exception as e:
            print(f"[!] Error fetching from Yahoo Finance for {symbol}: {e}")
            from .sample_universe import SampleUniverseProvider
            return SampleUniverseProvider().get_candles(symbol, limit=limit)

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        candles = self.get_candles(symbol, limit=1)
        if candles:
            last = candles[-1]
            return {
                "symbol": symbol,
                "ltp": last["close"],
                "open": last["open"],
                "high": last["high"],
                "low": last["low"],
                "volume": last["volume"]
            }
        return {}

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        from .sample_universe import SampleUniverseProvider
        return SampleUniverseProvider().get_fundamentals(symbol)

    def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict[float, Dict[str, Any]]]:
        from .sample_universe import SampleUniverseProvider
        return SampleUniverseProvider().get_option_chain(symbol, expiry)

