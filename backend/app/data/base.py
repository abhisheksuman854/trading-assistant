"""
TradeLens AI - Abstract Market Data Provider Interface
Ensures zero vendor lock-in. Switch seamlessly between yfinance, NSE Bhavcopy, Angel One, or Zerodha Kite.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class MarketDataProvider(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """Fetch latest LTP, OHLC, and Volume."""
        pass

    @abstractmethod
    def get_candles(self, symbol: str, interval: str = "1d", limit: int = 100) -> List[Dict[str, float]]:
        """Fetch historical OHLCV candle records."""
        pass

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """Fetch balance sheet, P&L, valuation, and governance ratios."""
        pass

    @abstractmethod
    def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict[float, Dict[str, Any]]]:
        """Fetch strike-level option chain (LTP, OI, Volume, IV)."""
        pass

