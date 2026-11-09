"""
TradeLens AI - Unit Tests: Daily Pivots Session-Level Validation.
Proves that Daily Classic Pivots are derived strictly from full completed trading sessions,
and that intraday bars (1m, 5m) from the current session are NEVER confused with prior session OHLC.
"""
import unittest
from app.core.technicals import resolve_daily_pivots, evaluate_technicals


class TestPivotsSessionValidation(unittest.TestCase):

    def test_single_day_intraday_candles_return_unavailable(self):
        """
        If only intraday bars from today's session are provided without prior session data,
        the system must return UNAVAILABLE rather than using an arbitrary 1m/5m candle!
        """
        today_intraday_candles = [
            {"timestamp": f"2026-11-09T09:{15+i}:00+05:30", "open": 24800.0, "high": 24820.0, "low": 24790.0, "close": 24810.0, "volume": 500}
            for i in range(15)
        ]

        pivots, source, status = resolve_daily_pivots(today_intraday_candles)
        self.assertIsNone(pivots)
        self.assertEqual(source, "UNAVAILABLE")
        self.assertEqual(status, "UNAVAILABLE_MISSING_PRIOR_SESSION_DATA")

        # Also verify in evaluate_technicals output
        eval_res = evaluate_technicals(today_intraday_candles)
        self.assertIsNone(eval_res["pivots"])
        self.assertEqual(eval_res["pivot_status"], "UNAVAILABLE_MISSING_PRIOR_SESSION_DATA")

    def test_multi_day_intraday_candles_aggregates_prior_session(self):
        """
        When intraday candles span yesterday and today, the system must aggregate
        the full prior calendar date to calculate accurate daily pivots.
        """
        # Yesterday: 2026-11-06 (High: 25100, Low: 24800, Close: 24950)
        yesterday_bars = [
            {"timestamp": "2026-11-06T09:15:00+05:30", "open": 24850.0, "high": 25000.0, "low": 24820.0, "close": 24900.0, "volume": 1000},
            {"timestamp": "2026-11-06T12:00:00+05:30", "open": 24900.0, "high": 25100.0, "low": 24800.0, "close": 24920.0, "volume": 2000},
            {"timestamp": "2026-11-06T15:25:00+05:30", "open": 24920.0, "high": 24960.0, "low": 24910.0, "close": 24950.0, "volume": 1500}
        ]
        # Today: 2026-11-09
        today_bars = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 24960.0, "high": 24980.0, "low": 24940.0, "close": 24970.0, "volume": 800}
        ]

        all_bars = yesterday_bars + today_bars
        pivots, source, status = resolve_daily_pivots(all_bars)

        self.assertIsNotNone(pivots)
        self.assertEqual(source, "AGGREGATED_PRIOR_INTRADAY_SESSION")
        self.assertEqual(status, "VALID_DAILY_PIVOTS")

        # Expected pivot: (High=25100 + Low=24800 + Close=24950) / 3 = 24950.0
        expected_p = round((25100.0 + 24800.0 + 24950.0) / 3.0, 2)
        self.assertEqual(pivots["pivot"], expected_p)

    def test_explicit_prior_session_ohlc_overrides_intraday_candles(self):
        """
        When explicit prior_session_ohlc is provided, it is always used as the authoritative source.
        """
        prior_session = {"high": 25300.0, "low": 25000.0, "close": 25150.0, "session_date": "2026-11-06"}
        today_bars = [
            {"timestamp": "2026-11-09T09:15:00+05:30", "open": 25200.0, "high": 25250.0, "low": 25180.0, "close": 25220.0, "volume": 500}
        ]

        pivots, source, status = resolve_daily_pivots(today_bars, prior_session_ohlc=prior_session)
        self.assertIsNotNone(pivots)
        self.assertEqual(source, "PRIOR_SESSION_OHLC")
        self.assertEqual(status, "VALID_DAILY_PIVOTS")

        expected_p = round((25300.0 + 25000.0 + 25150.0) / 3.0, 2)
        self.assertEqual(pivots["pivot"], expected_p)


if __name__ == "__main__":
    unittest.main()

