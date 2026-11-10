"""
TradeLens AI - Unit Tests: Futures Contract & Volume Proxy Manager.
Verifies contract selection, NSE monthly expiry calculation, rollover detection,
basis calculations, distinct labeling of NIFTY Futures VWAP, and broker contract-master validation.
"""
import unittest
from datetime import datetime, date
from app.data.futures_manager import FuturesContractManager
from app.data.session_clock import IST_ZONE


class TestFuturesManager(unittest.TestCase):

    def setUp(self):
        self.mgr = FuturesContractManager()

    def test_all_supported_indices_expire_on_last_tuesday(self):
        """
        Under current NSE contract specifications:
        - NIFTY 50: Last Tuesday
        - BANKNIFTY: Last Tuesday
        - FINNIFTY: Last Tuesday
        - MIDCPNIFTY: Last Tuesday
        In October 2026, Tuesdays are: Oct 6, 13, 20, 27.
        Last Tuesday is Oct 27, 2026 (a valid trading day).
        """
        supported = ["NIFTY 50", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
        expected_oct_exp = date(2026, 10, 27)
        for sym in supported:
            exp = self.mgr.get_monthly_expiry(2026, 10, symbol=sym)
            self.assertEqual(exp, expected_oct_exp, f"Failed for {sym}: expected {expected_oct_exp}, got {exp}")
            self.assertEqual(exp.weekday(), 1, f"Failed for {sym}: expected weekday 1 (Tuesday)")

    def test_november_2026_regression_case_holiday_adjustment(self):
        """
        Critical NSE Regression Case:
        In November 2026, normal last Tuesday is 24 November 2026.
        24 November 2026 is an NSE trading holiday (Guru Nanak Jayanti).
        Under NSE rules, expiry shifts to preceding trading day: Monday, 23 November 2026.
        """
        for sym in ["NIFTY 50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]:
            exp_nov_2026 = self.mgr.get_monthly_expiry(2026, 11, symbol=sym)
            self.assertEqual(exp_nov_2026, date(2026, 11, 23), f"Failed for {sym}")
            self.assertEqual(exp_nov_2026.weekday(), 0, f"Expected Monday for {sym}")

    def test_year_boundary_rollover_december_to_january(self):
        """
        Verifies year-boundary transition from December to January.
        In December 2026, last Tuesday is Dec 29, 2026.
        After Dec 29 15:30 IST, active near-month contract must roll to January 2027.
        In January 2027, Tuesdays are Jan 5, 12, 19, 26 -> Last Tuesday is Jan 26, 2027.
        Note: Jan 26 is Republic Day holiday -> shifts to Monday, Jan 25, 2027.
        """
        post_dec_exp_dt = datetime(2026, 12, 30, 10, 0, tzinfo=IST_ZONE)
        contracts = self.mgr.get_active_contracts(symbol="NIFTY 50", current_dt=post_dec_exp_dt)
        self.assertEqual(contracts["near_expiry"].year, 2027)
        self.assertEqual(contracts["near_expiry"].month, 1)
        # Jan 26, 2027 is Republic Day holiday in DEFAULT_HOLIDAYS_2025_2026 (or weekend) -> shifts to Jan 25
        self.assertEqual(contracts["near_expiry"], date(2027, 1, 25))

        sym = self.mgr.format_trading_symbol("NIFTY 50", contracts["near_expiry"])
        self.assertEqual(sym, "NIFTY27JANFUT")

    def test_active_contract_and_trading_symbol_format(self):
        """
        On 2026-11-09, active near-month is Nov 2026 expiring on Nov 23, 2026 (due to Nov 24 holiday).
        Trading symbol: NIFTY26NOVFUT.
        """
        test_dt = datetime(2026, 11, 9, 10, 30, tzinfo=IST_ZONE)
        contracts = self.mgr.get_active_contracts(symbol="NIFTY 50", current_dt=test_dt)
        self.assertEqual(contracts["near_expiry"], date(2026, 11, 23))
        self.assertEqual(contracts["rollover_status"], "NORMAL_ACTIVE_CYCLE")

        sym = self.mgr.format_trading_symbol("NIFTY 50", contracts["near_expiry"])
        self.assertEqual(sym, "NIFTY26NOVFUT")

    def test_rollover_window_detection_on_expiry_week(self):
        """
        Nov 20, 2026 is Friday, 3 days before Monday Nov 23 expiry -> ROLLOVER_IN_PROGRESS.
        Nov 23, 2026 is Monday (Expiry Day) -> EXPIRY_DAY_FINAL_SESSION.
        """
        test_dt_window = datetime(2026, 11, 20, 11, 0, tzinfo=IST_ZONE)
        contracts_window = self.mgr.get_active_contracts(symbol="NIFTY 50", current_dt=test_dt_window)
        self.assertTrue(contracts_window["is_rollover_window"])
        self.assertEqual(contracts_window["rollover_status"], "ROLLOVER_IN_PROGRESS")

        test_dt_expiry = datetime(2026, 11, 23, 11, 0, tzinfo=IST_ZONE)
        contracts_expiry = self.mgr.get_active_contracts(symbol="NIFTY 50", current_dt=test_dt_expiry)
        self.assertTrue(contracts_expiry["is_expiry_day"])
        self.assertEqual(contracts_expiry["rollover_status"], "EXPIRY_DAY_FINAL_SESSION")

    def test_contract_master_cross_validation_and_mismatch_rejection(self):
        """
        Cross-checks calculated contract and expiry against broker contract master.
        Rejects mismatches to prevent trading non-existent or misaligned contracts.
        """
        calc_expiry = date(2026, 11, 23)

        # 1. Matching master contract
        valid_master = {
            "NIFTY26NOVFUT": {
                "token": "26000",
                "symbol": "NIFTY26NOVFUT",
                "expiry": "2026-11-23",
                "lotsize": 65
            }
        }
        res_valid = self.mgr.validate_contract_against_master("NIFTY 50", calc_expiry, valid_master)
        self.assertTrue(res_valid["is_valid"])
        self.assertEqual(res_valid["status"], "VERIFIED_IN_CONTRACT_MASTER")
        self.assertEqual(res_valid["token"], "26000")
        self.assertEqual(res_valid["lot_size"], 65)

        # 2. Expiry mismatch (Master has 2026-11-26 but calculated is 2026-11-23)
        mismatched_master = {
            "NIFTY26NOVFUT": {
                "token": "26000",
                "symbol": "NIFTY26NOVFUT",
                "expiry": "2026-11-26",
                "lotsize": 65
            }
        }
        res_mismatch = self.mgr.validate_contract_against_master("NIFTY 50", calc_expiry, mismatched_master)
        self.assertFalse(res_mismatch["is_valid"])
        self.assertEqual(res_mismatch["status"], "CONTRACT_MASTER_MISMATCH")
        self.assertIn("does not match", res_mismatch["error"])

        # 3. Missing symbol in contract master
        res_missing = self.mgr.validate_contract_against_master("NIFTY 50", calc_expiry, {})
        self.assertFalse(res_missing["is_valid"])
        self.assertEqual(res_missing["status"], "SYMBOL_NOT_IN_CONTRACT_MASTER")
        self.assertIn("not found in broker contract master", res_missing["error"])

        # 4. Standalone validation without contract master
        res_standalone = self.mgr.validate_contract_against_master("NIFTY 50", calc_expiry, None)
        self.assertTrue(res_standalone["is_valid"])
        self.assertEqual(res_standalone["status"], "UNVALIDATED_STANDALONE")

    def test_basis_calculation_contango_and_backwardation(self):
        """Verifies Basis = Futures Price - Spot Price."""
        # Contango (Futures > Spot)
        res_contango = self.mgr.calculate_basis(spot_price=24850.0, futures_price=24892.5)
        self.assertEqual(res_contango["basis_pts"], 42.5)
        self.assertEqual(res_contango["market_structure"], "PREMIUM_CONTANGO")

        # Backwardation (Futures < Spot)
        res_backwardation = self.mgr.calculate_basis(spot_price=24900.0, futures_price=24870.0)
        self.assertEqual(res_backwardation["basis_pts"], -30.0)
        self.assertEqual(res_backwardation["market_structure"], "DISCOUNT_BACKWARDATION")

    def test_futures_vwap_distinct_labeling_and_contract_attribution(self):
        """
        Verifies that futures VWAP is explicitly labeled as 'NIFTY Futures VWAP',
        identifies underlying contract, and marks volume_source as FUTURES_TRADED_PROXY.
        """
        sample_futures_candles = [
            {"high": 24900.0, "low": 24850.0, "close": 24880.0, "volume": 10000},
            {"high": 24920.0, "low": 24870.0, "close": 24910.0, "volume": 15000}
        ]
        vwap_res = self.mgr.calculate_futures_vwap("NIFTY 50", sample_futures_candles, spot_price=24860.0)

        self.assertEqual(vwap_res["vwap_name"], "NIFTY Futures VWAP")
        self.assertIn("NIFTY", vwap_res["underlying_contract"])
        self.assertEqual(vwap_res["volume_source"], "FUTURES_TRADED_PROXY")
        self.assertTrue(vwap_res["is_spot_proxy"])
        self.assertEqual(vwap_res["vwap_status"], "VALID_FUTURES_VWAP")
        self.assertIsNotNone(vwap_res["futures_vwap"])
        self.assertIn("basis", vwap_res)

    def test_futures_vwap_when_feed_is_missing(self):
        """When futures candles are unavailable, returns UNAVAILABLE without inventing volume."""
        vwap_res = self.mgr.calculate_futures_vwap("NIFTY 50", [])
        self.assertIsNone(vwap_res["futures_vwap"])
        self.assertEqual(vwap_res["vwap_status"], "UNAVAILABLE_NO_FUTURES_FEED")


if __name__ == "__main__":
    unittest.main()
