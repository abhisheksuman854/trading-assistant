"""
TradeLens AI - Unit Tests: Indian Market Session Clock.
Verifies IST timezone alignment (UTC+5:30), market trading sessions (09:15-15:30),
NSE holiday calendar, and session boundary reset detection.
"""
import unittest
from datetime import datetime, date, time
from app.data.session_clock import IndianMarketSessionClock, MarketSessionState, IST_ZONE


class TestSessionClock(unittest.TestCase):

    def setUp(self):
        self.clock = IndianMarketSessionClock()

    def test_ist_timezone_localization(self):
        """Verifies that datetime objects are accurately localized to Asia/Kolkata."""
        utc_dt = datetime(2026, 11, 9, 3, 45, tzinfo=IST_ZONE) # UTC+5:30
        ist_dt = self.clock.to_ist(utc_dt)
        self.assertEqual(str(ist_dt.tzinfo), "Asia/Kolkata")

    def test_weekend_market_closed(self):
        """Saturday and Sunday must always be MARKET_CLOSED."""
        saturday = datetime(2026, 11, 7, 10, 30, tzinfo=IST_ZONE)
        sunday = datetime(2026, 11, 8, 11, 0, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(saturday), MarketSessionState.MARKET_CLOSED)
        self.assertEqual(self.clock.get_session_state(sunday), MarketSessionState.MARKET_CLOSED)
        self.assertFalse(self.clock.is_market_open(saturday))

    def test_holiday_market_closed(self):
        """Official market holidays must be MARKET_CLOSED even during trading hours."""
        republic_day = datetime(2026, 1, 26, 10, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(republic_day), MarketSessionState.MARKET_CLOSED)
        self.assertFalse(self.clock.is_trading_day(republic_day))

    def test_session_state_progression_on_regular_day(self):
        """Verifies progression through trading day phases on Monday Nov 9, 2026."""
        # 08:30 IST -> MARKET_CLOSED
        t_pre = datetime(2026, 11, 9, 8, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_pre), MarketSessionState.MARKET_CLOSED)

        # 09:05 IST -> PRE_MARKET
        t_pm = datetime(2026, 11, 9, 9, 5, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_pm), MarketSessionState.PRE_MARKET)

        # 09:20 IST -> OPENING_RANGE (First 15 mins)
        t_or = datetime(2026, 11, 9, 9, 20, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_or), MarketSessionState.OPENING_RANGE)
        self.assertTrue(self.clock.is_market_open(t_or))

        # 11:30 IST -> REGULAR_SESSION
        t_reg = datetime(2026, 11, 9, 11, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_reg), MarketSessionState.REGULAR_SESSION)

        # 15:20 IST -> CLOSING_RANGE (MIS square-off window)
        t_close = datetime(2026, 11, 9, 15, 20, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_close), MarketSessionState.CLOSING_RANGE)

        # 15:45 IST -> POST_MARKET
        t_post = datetime(2026, 11, 9, 15, 45, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_post), MarketSessionState.POST_MARKET)

        # 16:30 IST -> MARKET_CLOSED
        t_night = datetime(2026, 11, 9, 16, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_night), MarketSessionState.MARKET_CLOSED)

    def test_session_boundary_reset_signal(self):
        """
        is_new_session must return True when crossing from previous day
        or crossing the 09:15:00 IST open boundary.
        """
        # Date change
        prev_day = datetime(2026, 11, 6, 15, 30, tzinfo=IST_ZONE)
        next_day_morning = datetime(2026, 11, 9, 9, 0, tzinfo=IST_ZONE)
        self.assertTrue(self.clock.is_new_session(prev_day, next_day_morning))

        # Crossing 09:15 open today
        before_open = datetime(2026, 11, 9, 9, 10, tzinfo=IST_ZONE)
        after_open = datetime(2026, 11, 9, 9, 16, tzinfo=IST_ZONE)
        self.assertTrue(self.clock.is_new_session(before_open, after_open))

        # Mid-session ticks do NOT trigger new session
        tick1 = datetime(2026, 11, 9, 10, 0, tzinfo=IST_ZONE)
        tick2 = datetime(2026, 11, 9, 10, 5, tzinfo=IST_ZONE)
        self.assertFalse(self.clock.is_new_session(tick1, tick2))

    def test_exceptional_session_diwali_muhurat_trading(self):
        """
        On Diwali Laxmi Pujan (2026-11-08, Sunday):
        - Before 14:05: MARKET_CLOSED
        - 14:05 to 14:15: MUHURAT_PRE_OPEN (is_market_open=False)
        - 14:15 to 14:20: MUHURAT_PRE_OPEN_BUFFER (is_market_open=False)
        - 14:20 to 15:40: MUHURAT_TRADING (is_market_open=True)
        - After 15:40: MARKET_CLOSED
        """
        # 1. Normal daytime on Sunday holiday before 14:05 -> closed
        t_day = datetime(2026, 11, 8, 14, 0, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_day), MarketSessionState.MARKET_CLOSED)
        self.assertFalse(self.clock.is_market_open(t_day))
        self.assertTrue(self.clock.is_trading_day(t_day))

        # 2. Muhurat Pre-Open (14:10 IST) -> Pre-open state, is_market_open is False
        t_pre = datetime(2026, 11, 8, 14, 10, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_pre), "MUHURAT_PRE_OPEN")
        self.assertFalse(self.clock.is_market_open(t_pre))

        # 3. Muhurat Pre-Open Buffer (14:18 IST) -> Pre-open buffer state, is_market_open is False
        t_buff = datetime(2026, 11, 8, 14, 18, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_buff), "MUHURAT_PRE_OPEN_BUFFER")
        self.assertFalse(self.clock.is_market_open(t_buff))

        # 4. Continuous trading (14:30 IST) -> MUHURAT_TRADING, is_market_open is True
        t_trading = datetime(2026, 11, 8, 14, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_trading), "MUHURAT_TRADING")
        self.assertTrue(self.clock.is_market_open(t_trading))

        # 5. After 15:40 (15:45 IST and 18:30 IST) -> MARKET_CLOSED
        t_closed_after = datetime(2026, 11, 8, 15, 45, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_closed_after), MarketSessionState.MARKET_CLOSED)
        self.assertFalse(self.clock.is_market_open(t_closed_after))

        t_evening = datetime(2026, 11, 8, 18, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_evening), MarketSessionState.MARKET_CLOSED)
        self.assertFalse(self.clock.is_market_open(t_evening))

    def test_dynamic_exceptional_session_registration(self):
        """Allows dynamic registration of exchange circular trading sessions at runtime."""
        self.clock.add_exceptional_session("2026-06-15", time(10, 0), time(11, 0), "MOCK_EXCEPTIONAL")
        t_mock = datetime(2026, 6, 15, 10, 30, tzinfo=IST_ZONE)
        self.assertEqual(self.clock.get_session_state(t_mock), "MOCK_EXCEPTIONAL")
        self.assertTrue(self.clock.is_market_open(t_mock))


if __name__ == "__main__":
    unittest.main()

