"""
TradeLens AI - Abstract Market Data Provider Interface
Ensures zero vendor lock-in. Switch seamlessly between yfinance, NSE Bhavcopy, Angel One, or Zerodha Kite.
Explicitly defines data statuses (LIVE, HISTORICAL, SIMULATED, UNAVAILABLE) to guarantee data integrity.
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class DataStatus:
    LIVE = "LIVE"                       # Verified real-time streaming tick/quote from broker
    HISTORICAL = "HISTORICAL"           # Verified historical candle records from broker API
    SIMULATED = "SIMULATED"             # Explicitly identified benchmark / offline sample data
    UNAVAILABLE = "UNAVAILABLE"         # Data could not be retrieved from provider


class MarketDataProvider(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch latest LTP, OHLC, Volume, data_status, and last_updated_at.
        Returns schema:
        {
            "symbol": str,
            "ltp": float,
            "open": float,
            "high": float,
            "low": float,
            "close": float,
            "volume": Optional[int],
            "volume_status": str,  # 'LIVE', 'NOT_APPLICABLE_FOR_SPOT_INDEX', 'UNAVAILABLE'
            "data_status": str,    # DataStatus.LIVE | SIMULATED | UNAVAILABLE
            "last_updated_at": str # ISO 8601 timestamp
        }
        """
        pass

    @abstractmethod
    def get_candles(self, symbol: str, interval: str = "1d", limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch genuine historical OHLCV candle records.
        Must NOT fabricate synthetic candles if broker data is unavailable.
        Returns list of candle dicts with 'timestamp', 'open', 'high', 'low', 'close', 'volume', 'candle_status'.
        """
        pass

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        """Fetch balance sheet, P&L, valuation, and governance ratios."""
        pass

    @abstractmethod
    def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict[float, Dict[str, Any]]]:
        """Fetch strike-level option chain (LTP, OI, Volume, IV)."""
        pass


