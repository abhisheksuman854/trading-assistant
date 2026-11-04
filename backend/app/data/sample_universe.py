"""
TradeLens AI - Sample Indian Market Universe Data Provider
Contains rich, realistic datasets matching real-time Indian indices (NIFTY 23,960.60, BANKNIFTY 55,913.60)
and the user's live watchlist (Nestle India, HCL Tech, National Aluminium, Bank of Baroda, DLF, etc.).
"""
from typing import List, Dict, Any, Optional
from .base import MarketDataProvider


def generate_candle_series(target_cmp: float, setup: str = "pullback_bounce", count: int = 60) -> List[Dict[str, float]]:
    """Generates realistic market candles scaled so the last close equals target_cmp."""
    candles = []
    p = target_cmp * 0.95
    vol = 1400000
    
    for i in range(count):
        if setup == "pullback_bounce":
            # 0 to 45: steady uptrend
            # 46 to 55: healthy pullback towards 20 EMA on declining volume
            # 56 to 59: clean bounce from 20 EMA on heavy institutional volume
            if i < 46:
                change = 0.0025 + (((i % 4) - 1.5) * 0.002)
                c_vol = int(vol * (1.0 + (i % 3) * 0.1))
            elif i < 56:
                change = -0.0035 + (((i % 3) - 1.0) * 0.001)
                c_vol = int(vol * 0.65)
            else:
                change = 0.005 + ((i - 56) * 0.002)
                c_vol = int(vol * 2.2)
        elif setup == "breakout":
            if i < 54:
                change = (((i % 6) - 2.5) * 0.002)
                c_vol = int(vol * 0.8)
            else:
                change = 0.012
                c_vol = int(vol * 2.8)
        elif setup == "overextended":
            change = 0.0065 + (((i % 3) - 1.0) * 0.002)
            c_vol = int(vol * 1.2)
        elif setup == "bearish":
            change = -0.0035 + (((i % 4) - 1.5) * 0.002)
            c_vol = int(vol * 1.3)
        else:
            change = (((i % 5) - 2) * 0.002)
            c_vol = vol

        open_p = round(p, 2)
        close_p = round(p * (1.0 + change), 2)
        high_p = round(max(open_p, close_p) * 1.004, 2)
        low_p = round(min(open_p, close_p) * 0.996, 2)
        
        candles.append({
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": c_vol
        })
        p = close_p
        
    # Scale entire series so candles[-1]["close"] precisely matches target_cmp
    final_c = candles[-1]["close"]
    if final_c > 0:
        scale = target_cmp / final_c
        for c in candles:
            c["open"] = round(c["open"] * scale, 2)
            c["high"] = round(c["high"] * scale, 2)
            c["low"] = round(c["low"] * scale, 2)
            c["close"] = round(c["close"] * scale, 2)
            
    candles[-1]["close"] = round(target_cmp, 2)
    return candles


# Live Market Major Indices (matching real market board)
INDIAN_INDICES: List[Dict[str, Any]] = [
    {
        "symbol": "NIFTY 50",
        "value": 23960.60,
        "change_pts": 149.75,
        "change_pct": 0.63,
        "is_positive": True
    },
    {
        "symbol": "BANKNIFTY",
        "value": 55913.60,
        "change_pts": 373.55,
        "change_pct": 0.67,
        "is_positive": True
    },
    {
        "symbol": "FINNIFTY",
        "value": 24840.25,
        "change_pts": 112.40,
        "change_pct": 0.45,
        "is_positive": True
    },
    {
        "symbol": "MIDCPNIFTY",
        "value": 12450.80,
        "change_pts": 85.30,
        "change_pct": 0.69,
        "is_positive": True
    },
    {
        "symbol": "SENSEX",
        "value": 78850.10,
        "change_pts": 420.15,
        "change_pct": 0.54,
        "is_positive": True
    },
    {
        "symbol": "INDIA VIX",
        "value": 13.25,
        "change_pts": -0.42,
        "change_pct": -3.07,
        "is_positive": False,
        "status": "Normal Volatility"
    }
]


# Realistic Indian Stocks matching User's Watchlist & Major LargeCaps
INDIAN_STOCKS_DATA: List[Dict[str, Any]] = [
    {
        "symbol": "NESTLEIND",
        "name": "Nestle India Ltd",
        "category": "F&O / FMCG",
        "lot_size": 50,
        "days_to_earnings": 45,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=1248.00, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 36.5,
            "bvps": 115.0,
            "pe": 34.2,
            "pb": 10.8,
            "industry_pe": 38.0,
            "shares_cr": 96.4,
            "fcf_cr": 3100.0,
            "cfo_cr": 3600.0,
            "net_profit_cr": 3400.0,
            "revenue_growth_3yr": 12.8,
            "profit_growth_3yr": 14.5,
            "roe": 31.5,
            "roce": 42.0,
            "debt_to_equity": 0.05,
            "interest_coverage": 28.5,
            "current_ratio": 1.45,
            "quick_ratio": 1.10,
            "promoter_holding": 62.8,
            "promoter_pledge": 0.0
        }
    },
    {
        "symbol": "NATIONALUM",
        "name": "National Aluminium Co Ltd",
        "category": "F&O / Metals & Mining",
        "lot_size": 3750,
        "days_to_earnings": 50,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=326.25, setup="breakout", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 22.4,
            "bvps": 98.0,
            "pe": 14.5,
            "pb": 3.3,
            "industry_pe": 18.0,
            "shares_cr": 183.6,
            "fcf_cr": 2400.0,
            "cfo_cr": 3800.0,
            "net_profit_cr": 3200.0,
            "revenue_growth_3yr": 18.2,
            "profit_growth_3yr": 42.0,
            "roe": 22.8,
            "roce": 29.5,
            "debt_to_equity": 0.02,
            "interest_coverage": 45.0,
            "current_ratio": 2.15,
            "quick_ratio": 1.65,
            "promoter_holding": 51.3,
            "promoter_pledge": 0.0
        }
    },
    {
        "symbol": "HCLTECH",
        "name": "HCL Technologies Ltd",
        "category": "F&O / Information Technology",
        "lot_size": 350,
        "days_to_earnings": 42,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=1405.00, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 62.0,
            "bvps": 260.0,
            "pe": 22.6,
            "pb": 5.4,
            "industry_pe": 28.0,
            "shares_cr": 271.0,
            "fcf_cr": 14000.0,
            "cfo_cr": 18000.0,
            "net_profit_cr": 16800.0,
            "revenue_growth_3yr": 11.5,
            "profit_growth_3yr": 12.0,
            "roe": 24.5,
            "roce": 32.0,
            "debt_to_equity": 0.09,
            "interest_coverage": 35.0,
            "current_ratio": 1.85,
            "quick_ratio": 1.85,
            "promoter_holding": 60.8,
            "promoter_pledge": 0.0
        }
    },
    {
        "symbol": "BANKBARODA",
        "name": "Bank of Baroda",
        "category": "F&O / PSU Banking",
        "lot_size": 2925,
        "days_to_earnings": 55,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=245.37, setup="breakout", count=60),
        "fundamentals": {
            "is_banking": True,
            "eps": 36.5,
            "bvps": 220.0,
            "pe": 6.7,
            "pb": 1.1,
            "industry_pe": 10.5,
            "shares_cr": 517.0,
            "fcf_cr": 0.0,
            "cfo_cr": 22000.0,
            "net_profit_cr": 18500.0,
            "revenue_growth_3yr": 22.0,
            "profit_growth_3yr": 35.0,
            "roe": 17.5,
            "roce": 0.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 0.0,
            "current_ratio": 0.0,
            "quick_ratio": 0.0,
            "promoter_holding": 63.9,
            "promoter_pledge": 0.0,
            "crar": 16.3,
            "gross_npa": 2.88,
            "net_npa": 0.69,
            "nim": 3.18,
            "casa_ratio": 40.5
        }
    },
    {
        "symbol": "CANBK",
        "name": "Canara Bank",
        "category": "F&O / PSU Banking",
        "lot_size": 4500,
        "days_to_earnings": 60,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=118.32, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": True,
            "eps": 18.2,
            "bvps": 95.0,
            "pe": 6.5,
            "pb": 1.24,
            "industry_pe": 10.5,
            "shares_cr": 907.0,
            "fcf_cr": 0.0,
            "cfo_cr": 18000.0,
            "net_profit_cr": 16200.0,
            "revenue_growth_3yr": 24.0,
            "profit_growth_3yr": 38.0,
            "roe": 19.8,
            "roce": 0.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 0.0,
            "current_ratio": 0.0,
            "quick_ratio": 0.0,
            "promoter_holding": 62.9,
            "promoter_pledge": 0.0,
            "crar": 16.2,
            "gross_npa": 4.10,
            "net_npa": 1.15,
            "nim": 3.05,
            "casa_ratio": 32.8
        }
    },
    {
        "symbol": "DLF",
        "name": "DLF Ltd",
        "category": "F&O / Realty",
        "lot_size": 825,
        "days_to_earnings": 48,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=720.00, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 14.5,
            "bvps": 185.0,
            "pe": 49.6,
            "pb": 3.89,
            "industry_pe": 35.0,
            "shares_cr": 247.0,
            "fcf_cr": 3200.0,
            "cfo_cr": 4500.0,
            "net_profit_cr": 3600.0,
            "revenue_growth_3yr": 16.5,
            "profit_growth_3yr": 28.0,
            "roe": 8.5,
            "roce": 9.8,
            "debt_to_equity": 0.06,
            "interest_coverage": 12.5,
            "current_ratio": 2.8,
            "quick_ratio": 1.1,
            "promoter_holding": 74.0,
            "promoter_pledge": 0.0
        }
    },
    {
        "symbol": "FEDERALBNK",
        "name": "Federal Bank Ltd",
        "category": "F&O / Private Banking",
        "lot_size": 2500,
        "days_to_earnings": 52,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=335.20, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": True,
            "eps": 17.5,
            "bvps": 130.0,
            "pe": 19.1,
            "pb": 2.57,
            "industry_pe": 18.0,
            "shares_cr": 245.0,
            "fcf_cr": 0.0,
            "cfo_cr": 4500.0,
            "net_profit_cr": 4200.0,
            "revenue_growth_3yr": 21.0,
            "profit_growth_3yr": 26.0,
            "roe": 14.5,
            "roce": 0.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 0.0,
            "current_ratio": 0.0,
            "quick_ratio": 0.0,
            "promoter_holding": 0.0,
            "promoter_pledge": 0.0,
            "crar": 15.8,
            "gross_npa": 2.10,
            "net_npa": 0.55,
            "nim": 3.25,
            "casa_ratio": 31.5
        }
    },
    {
        "symbol": "RELIANCE",
        "name": "Reliance Industries Ltd",
        "category": "F&O / Energy & Telecom",
        "lot_size": 250,
        "days_to_earnings": 45,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=2835.50, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 104.5,
            "bvps": 1150.0,
            "pe": 27.1,
            "pb": 2.46,
            "industry_pe": 28.5,
            "shares_cr": 676.0,
            "fcf_cr": 32000.0,
            "cfo_cr": 138000.0,
            "net_profit_cr": 70000.0,
            "revenue_growth_3yr": 16.4,
            "profit_growth_3yr": 14.8,
            "roe": 10.2,
            "roce": 11.5,
            "debt_to_equity": 0.42,
            "interest_coverage": 6.8,
            "current_ratio": 1.25,
            "quick_ratio": 0.92,
            "promoter_holding": 50.3,
            "promoter_pledge": 0.0
        }
    },
    {
        "symbol": "HDFCBANK",
        "name": "HDFC Bank Ltd",
        "category": "F&O / Banking",
        "lot_size": 550,
        "days_to_earnings": 60,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=1745.20, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": True,
            "eps": 88.0,
            "bvps": 650.0,
            "pe": 19.8,
            "pb": 2.68,
            "industry_pe": 21.0,
            "shares_cr": 760.0,
            "fcf_cr": 0.0,
            "cfo_cr": 65000.0,
            "net_profit_cr": 67000.0,
            "revenue_growth_3yr": 22.5,
            "profit_growth_3yr": 24.0,
            "roe": 16.8,
            "roce": 0.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 0.0,
            "current_ratio": 0.0,
            "quick_ratio": 0.0,
            "promoter_holding": 0.0,
            "promoter_pledge": 0.0,
            "crar": 19.3,
            "gross_npa": 1.33,
            "net_npa": 0.38,
            "nim": 3.65,
            "casa_ratio": 38.2
        }
    },
    {
        "symbol": "ICICIBANK",
        "name": "ICICI Bank Ltd",
        "category": "F&O / Banking",
        "lot_size": 700,
        "days_to_earnings": 55,
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=1248.50, setup="pullback_bounce", count=60),
        "fundamentals": {
            "is_banking": True,
            "eps": 64.0,
            "bvps": 380.0,
            "pe": 19.5,
            "pb": 3.28,
            "industry_pe": 21.0,
            "shares_cr": 702.0,
            "fcf_cr": 0.0,
            "cfo_cr": 45000.0,
            "net_profit_cr": 44000.0,
            "revenue_growth_3yr": 24.5,
            "profit_growth_3yr": 32.0,
            "roe": 18.5,
            "roce": 0.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 0.0,
            "current_ratio": 0.0,
            "quick_ratio": 0.0,
            "promoter_holding": 0.0,
            "promoter_pledge": 0.0,
            "crar": 16.8,
            "gross_npa": 2.15,
            "net_npa": 0.42,
            "nim": 4.35,
            "casa_ratio": 42.1
        }
    },
    {
        "symbol": "TCS",
        "name": "Tata Consultancy Services Ltd",
        "category": "F&O / Information Technology",
        "lot_size": 175,
        "days_to_earnings": 1,  # Event risk!
        "days_to_expiry": 7,
        "candles": generate_candle_series(target_cmp=3980.00, setup="breakout", count=60),
        "fundamentals": {
            "is_banking": False,
            "eps": 132.0,
            "bvps": 260.0,
            "pe": 30.1,
            "pb": 15.3,
            "industry_pe": 29.0,
            "shares_cr": 361.0,
            "fcf_cr": 44000.0,
            "cfo_cr": 48000.0,
            "net_profit_cr": 46500.0,
            "revenue_growth_3yr": 12.0,
            "profit_growth_3yr": 10.5,
            "roe": 51.5,
            "roce": 64.0,
            "debt_to_equity": 0.0,
            "interest_coverage": 55.0,
            "current_ratio": 2.45,
            "quick_ratio": 2.45,
            "promoter_holding": 71.8,
            "promoter_pledge": 0.0
        }
    }
]


class SampleUniverseProvider(MarketDataProvider):
    def __init__(self):
        self.stocks = INDIAN_STOCKS_DATA
        self.indices = INDIAN_INDICES

    def get_indices(self) -> List[Dict[str, Any]]:
        return self.indices

    def get_all_stocks(self) -> List[Dict[str, Any]]:
        return self.stocks

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        for s in self.stocks:
            if s["symbol"] == symbol:
                last_c = s["candles"][-1]
                return {
                    "symbol": symbol,
                    "ltp": last_c["close"],
                    "open": last_c["open"],
                    "high": last_c["high"],
                    "low": last_c["low"],
                    "volume": last_c["volume"]
                }
        return {}

    def get_candles(self, symbol: str, interval: str = "1d", limit: int = 100) -> List[Dict[str, float]]:
        for s in self.stocks:
            if s["symbol"] == symbol:
                return s["candles"][-limit:]
        return []

    def get_fundamentals(self, symbol: str) -> Dict[str, Any]:
        for s in self.stocks:
            if s["symbol"] == symbol:
                return s["fundamentals"]
        return {}

    def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[Dict[float, Dict[str, Any]]]:
        for s in self.stocks:
            if s["symbol"] == symbol:
                return s.get("option_chain")
        return None
