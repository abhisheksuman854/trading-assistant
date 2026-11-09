"""
TradeLens AI - Unit Tests: Option Tradability & Liquidity Verification Engine.
Proves that a mathematically valid strike is NOT assumed tradable unless verified for:
1. Active quote and counterparty presence.
2. Bid-Ask spread execution friction.
3. Minimum institutional OI and volume.
4. Quote freshness (detects stale prices).
"""
import unittest
from datetime import datetime, timedelta
from app.core.options_analytics import verify_option_contract_tradability


class TestOptionTradability(unittest.TestCase):

    def test_liquid_contract_accepted_as_tradable(self):
        """Liquid contract with tight spread and high volume passes all checks."""
        now = datetime.now()
        contract_quote = {
            "ltp": 120.0,
            "bid": 119.5,
            "ask": 120.5,
            "oi": 1500000,
            "volume": 250000,
            "last_trade_time": (now - timedelta(seconds=15)).isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, current_dt=now)

        self.assertTrue(res["is_tradable"])
        self.assertEqual(res["tradability_status"], "TRADABLE_LIQUID")
        self.assertEqual(res["bid_ask_spread"], 1.0)
        self.assertEqual(res["spread_pct"], 0.83)
        self.assertEqual(res["bid_ask_status"], "TIGHT_SPREAD_OPTIMAL")
        self.assertEqual(res["quote_freshness"], "FRESH_LIVE_QUOTE")

    def test_excessive_spread_vetoes_tradability(self):
        """When bid-ask spread is wide (e.g. 8.3%), contract must be rejected as unviable."""
        now = datetime.now()
        contract_quote = {
            "ltp": 120.0,
            "bid": 115.0,
            "ask": 125.0,
            "oi": 500000,
            "volume": 25000,
            "last_trade_time": now.isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, max_spread_pct=4.0, current_dt=now)

        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["tradability_status"], "UNTRADABLE_ILLIQUID")
        self.assertEqual(res["bid_ask_spread"], 10.0)
        self.assertEqual(res["spread_pct"], 8.33)
        self.assertEqual(res["bid_ask_status"], "WIDE_SPREAD_EXCESSIVE_FRICTION")
        self.assertTrue(any("Excessive bid-ask spread" in r for r in res["rejection_reasons"]))

    def test_zero_counterparty_bid_ask_rejected(self):
        """Zero bid or ask means order cannot be filled immediately."""
        now = datetime.now()
        contract_quote = {
            "ltp": 85.0,
            "bid": 0.0,
            "ask": 95.0,
            "oi": 10000,
            "volume": 500,
            "last_trade_time": now.isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, current_dt=now)

        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["tradability_status"], "UNTRADABLE_ILLIQUID")
        self.assertEqual(res["bid_ask_status"], "NO_COUNTERPARTY_LIQUIDITY")

    def test_ghost_strike_zero_oi_zero_volume_rejected(self):
        """Strikes with zero OI and zero volume are ghost strikes."""
        now = datetime.now()
        contract_quote = {
            "ltp": 10.0,
            "bid": 8.0,
            "ask": 12.0,
            "oi": 0,
            "volume": 0,
            "last_trade_time": now.isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, current_dt=now)

        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["liquidity_status"], "ILLIQUID_GHOST_STRIKE")

    def test_stale_quote_flagged(self):
        """Quote older than max_quote_age_seconds (180s) is rejected for stale data risk."""
        now = datetime.now()
        contract_quote = {
            "ltp": 150.0,
            "bid": 149.0,
            "ask": 151.0,
            "oi": 800000,
            "volume": 50000,
            "last_trade_time": (now - timedelta(seconds=350)).isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, max_quote_age_seconds=180, current_dt=now)

        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["quote_freshness"], "STALE_QUOTE")
        self.assertTrue(any("Stale quote" in r for r in res["rejection_reasons"]))

    def test_crossed_orderbook_anomaly_rejected(self):
        """Crossed bid/ask (bid > ask) is an inverted anomaly and must be rejected immediately."""
        now = datetime.now()
        contract_quote = {
            "ltp": 100.0,
            "bid": 105.0,  # Corrupted: bid > ask
            "ask": 95.0,
            "oi": 100000,
            "volume": 20000,
            "last_trade_time": now.isoformat()
        }
        res = verify_option_contract_tradability(contract_quote, current_dt=now)
        self.assertFalse(res["is_tradable"])
        self.assertEqual(res["bid_ask_status"], "CROSSED_ORDERBOOK_ANOMALY")
        self.assertTrue(any("Crossed bid/ask" in r for r in res["rejection_reasons"]))

    def test_missing_or_zero_ltp_returns_unavailable(self):
        """Missing or zero LTP returns UNAVAILABLE without fabricating prices."""
        res_zero = verify_option_contract_tradability({"ltp": 0.0})
        self.assertFalse(res_zero["is_tradable"])
        self.assertEqual(res_zero["tradability_status"], "UNAVAILABLE")

        res_none = verify_option_contract_tradability({})
        self.assertFalse(res_none["is_tradable"])
        self.assertEqual(res_none["tradability_status"], "UNAVAILABLE")

    def test_configurable_thresholds(self):
        """Proves liquidity and spread thresholds are fully configurable parameters."""
        now = datetime.now()
        contract_quote = {
            "ltp": 100.0,
            "bid": 97.5,
            "ask": 101.5,  # Spread: 4.0%
            "oi": 30000,
            "volume": 3000,
            "last_trade_time": now.isoformat()
        }
        # Under strict filter (spread <= 3%, oi >= 50k): rejected
        res_strict = verify_option_contract_tradability(
            contract_quote,
            min_oi=50000,
            min_volume=5000,
            max_spread_pct=3.0,
            current_dt=now
        )
        self.assertFalse(res_strict["is_tradable"])

        # Under relaxed filter (spread <= 5%, oi >= 20k, vol >= 2k): accepted with caution
        res_relaxed = verify_option_contract_tradability(
            contract_quote,
            min_oi=20000,
            min_volume=2000,
            max_spread_pct=5.0,
            current_dt=now
        )
        self.assertTrue(res_relaxed["is_tradable"])


if __name__ == "__main__":
    unittest.main()

