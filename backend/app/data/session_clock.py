"""
TradeLens AI - Indian Market Session Clock (NSE / NIFTY 50)
Accurately models the National Stock Exchange of India (NSE) trading hours,
intraday session phases, IST timezone (UTC+5:30), and daily session reset signals.
"""
from datetime import datetime, time, date, timedelta
from typing import Optional, Set, Dict, List, Any
import zoneinfo

IST_ZONE = zoneinfo.ZoneInfo("Asia/Kolkata")


class MarketSessionState:
    PRE_MARKET = "PRE_MARKET"                  # 09:00:00 - 09:08:00 IST
    PRE_MARKET_BUFFER = "PRE_MARKET_BUFFER"    # 09:08:00 - 09:15:00 IST
    OPENING_RANGE = "OPENING_RANGE"            # 09:15:00 - 09:30:00 IST (First 15 mins for ORH/ORL)
    REGULAR_SESSION = "REGULAR_SESSION"        # 09:30:00 - 15:15:00 IST
    CLOSING_RANGE = "CLOSING_RANGE"            # 15:15:00 - 15:30:00 IST (MIS auto square-off)
    POST_MARKET = "POST_MARKET"                # 15:30:00 - 16:00:00 IST
    SPECIAL_SESSION = "SPECIAL_SESSION"        # Exceptional sessions (e.g. Diwali Muhurat Trading, DR testing)
    MARKET_CLOSED = "MARKET_CLOSED"            # Closed hours, regular weekends, or full-day exchange holidays


class IndianMarketSessionClock:
    """
    Session clock for Indian equity and F&O markets.
    Regular trading window: 09:15:00 to 15:30:00 IST Monday through Friday.
    Supports official NSE exchange holiday calendar and exceptional trading sessions
    (e.g., Diwali Muhurat Trading, Disaster Recovery live trading Saturdays).
    """
    PRE_MARKET_START = time(9, 0, 0)
    PRE_MARKET_END = time(9, 8, 0)
    MARKET_OPEN = time(9, 15, 0)
    OPENING_RANGE_END = time(9, 30, 0)
    MIS_SQUARE_OFF = time(15, 15, 0)
    MARKET_CLOSE = time(15, 30, 0)
    POST_MARKET_END = time(16, 0, 0)

    # Standard national holidays in YYYY-MM-DD format (expandable)
    DEFAULT_HOLIDAYS_2025_2026: Set[str] = {
        "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31",
        "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01",
        "2025-08-15", "2025-08-27", "2025-10-02", "2025-10-21",
        "2025-10-22", "2025-11-05", "2025-12-25",
        "2026-01-26", "2026-03-03", "2026-03-20", "2026-04-03",
        "2026-04-14", "2026-05-01", "2026-08-15", "2026-10-02",
        "2026-10-20", "2026-11-08", "2026-11-24", "2026-12-25",
        "2027-01-26"
    }

    # Exceptional special trading sessions published via NSE circulars
    DEFAULT_EXCEPTIONAL_SESSIONS: Dict[str, List[Dict[str, Any]]] = {
        # Diwali Laxmi Pujan (Official NSE Muhurat Trading Schedule for 8 Nov 2026)
        # Pre-open: 14:05-14:15 order collection, 14:15-14:20 order matching.
        # Continuous trading: 14:20-15:40 IST.
        "2026-11-08": [
            {"start": time(14, 5), "end": time(14, 15), "name": "MUHURAT_PRE_OPEN", "is_market_open": False},
            {"start": time(14, 15), "end": time(14, 20), "name": "MUHURAT_PRE_OPEN_BUFFER", "is_market_open": False},
            {"start": time(14, 20), "end": time(15, 40), "name": "MUHURAT_TRADING", "is_market_open": True}
        ],
        # Disaster Recovery (DR) testing on special Saturday
        "2026-03-07": [
            {"start": time(9, 15), "end": time(10, 0), "name": "DR_PRIMARY_SESSION", "is_market_open": True},
            {"start": time(11, 30), "end": time(12, 30), "name": "DR_FAILOVER_SESSION", "is_market_open": True}
        ]
    }

    def __init__(
        self,
        holidays: Optional[Set[str]] = None,
        exceptional_sessions: Optional[Dict[str, List[Dict[str, Any]]]] = None
    ):
        self.holidays = holidays if holidays is not None else set(self.DEFAULT_HOLIDAYS_2025_2026)
        self.exceptional_sessions = (
            exceptional_sessions if exceptional_sessions is not None
            else dict(self.DEFAULT_EXCEPTIONAL_SESSIONS)
        )

    def add_exceptional_session(
        self,
        date_str: str,
        start: time,
        end: time,
        name: str = "SPECIAL_SESSION",
        is_market_open: bool = True
    ) -> None:
        """Dynamically registers an exceptional exchange trading session (e.g. from circular)."""
        if date_str not in self.exceptional_sessions:
            self.exceptional_sessions[date_str] = []
        self.exceptional_sessions[date_str].append({
            "start": start,
            "end": end,
            "name": name,
            "is_market_open": is_market_open
        })

    @staticmethod
    def now_ist() -> datetime:
        """Returns the current datetime localized in Asia/Kolkata."""
        return datetime.now(IST_ZONE)

    @classmethod
    def to_ist(cls, dt: datetime) -> datetime:
        """Converts any datetime to Asia/Kolkata timezone."""
        if dt.tzinfo is None:
            # Assume naive datetime is in IST
            return dt.replace(tzinfo=IST_ZONE)
        return dt.astimezone(IST_ZONE)

    def is_holiday(self, d: date) -> bool:
        """Checks if a given date is an official market holiday."""
        return d.strftime("%Y-%m-%d") in self.holidays

    def is_trading_day(self, dt_or_d: Optional[datetime] = None) -> bool:
        """
        Returns True if the day has an active trading session.
        Accounts for:
        1. Exceptional trading sessions (e.g. Diwali Muhurat or DR Saturdays).
        2. Regular weekdays (Monday-Friday) that are not holidays.
        """
        if dt_or_d is None:
            target = self.now_ist().date()
        elif isinstance(dt_or_d, datetime):
            target = self.to_ist(dt_or_d).date()
        else:
            target = dt_or_d

        d_str = target.strftime("%Y-%m-%d")
        if d_str in self.exceptional_sessions:
            return True

        if target.weekday() >= 5:  # 5=Saturday, 6=Sunday
            return False
        return not self.is_holiday(target)

    def get_session_state(self, dt: Optional[datetime] = None) -> str:
        """
        Determines the current session phase in IST.
        Checks exceptional sessions first, then regular session progression.
        """
        curr = self.now_ist() if dt is None else self.to_ist(dt)
        curr_date = curr.date()
        curr_time = curr.time()
        d_str = curr_date.strftime("%Y-%m-%d")

        # 1. Check Exceptional Sessions (e.g. Muhurat Trading)
        if d_str in self.exceptional_sessions:
            for s in self.exceptional_sessions[d_str]:
                if s["start"] <= curr_time < s["end"]:
                    return s.get("name", MarketSessionState.SPECIAL_SESSION)
            return MarketSessionState.MARKET_CLOSED

        if not self.is_trading_day(curr_date):
            return MarketSessionState.MARKET_CLOSED

        if curr_time < self.PRE_MARKET_START:
            return MarketSessionState.MARKET_CLOSED
        elif curr_time < self.PRE_MARKET_END:
            return MarketSessionState.PRE_MARKET
        elif curr_time < self.MARKET_OPEN:
            return MarketSessionState.PRE_MARKET_BUFFER
        elif curr_time < self.OPENING_RANGE_END:
            return MarketSessionState.OPENING_RANGE
        elif curr_time < self.MIS_SQUARE_OFF:
            return MarketSessionState.REGULAR_SESSION
        elif curr_time < self.MARKET_CLOSE:
            return MarketSessionState.CLOSING_RANGE
        elif curr_time < self.POST_MARKET_END:
            return MarketSessionState.POST_MARKET
        else:
            return MarketSessionState.MARKET_CLOSED

    def is_market_open(self, dt: Optional[datetime] = None) -> bool:
        """
        Returns True if the market is in an active continuous trading window.
        """
        curr = self.now_ist() if dt is None else self.to_ist(dt)
        d_str = curr.date().strftime("%Y-%m-%d")

        if d_str in self.exceptional_sessions:
            curr_time = curr.time()
            for s in self.exceptional_sessions[d_str]:
                if s["start"] <= curr_time < s["end"]:
                    return bool(s.get("is_market_open", True))
            return False

        state = self.get_session_state(dt)
        return state in {
            MarketSessionState.OPENING_RANGE,
            MarketSessionState.REGULAR_SESSION,
            MarketSessionState.CLOSING_RANGE,
            MarketSessionState.SPECIAL_SESSION
        }

    def is_new_session(self, prev_dt: datetime, curr_dt: datetime) -> bool:
        """
        Detects if curr_dt belongs to a new trading session relative to prev_dt.
        Triggers a session reset when:
        1. Date has changed, OR
        2. Time crossed 09:15:00 IST from a prior period.
        """
        prev_ist = self.to_ist(prev_dt)
        curr_ist = self.to_ist(curr_dt)

        if curr_ist.date() > prev_ist.date():
            return True

        # Check if crossed the 09:15:00 boundary today
        if prev_ist.time() < self.MARKET_OPEN <= curr_ist.time():
            return True

        return False

    def get_session_open_time(self, d: Optional[date] = None) -> datetime:
        """Returns 09:15:00 IST datetime for the given date (default today)."""
        target_date = d or self.now_ist().date()
        return datetime.combine(target_date, self.MARKET_OPEN, tzinfo=IST_ZONE)

    def get_session_close_time(self, d: Optional[date] = None) -> datetime:
        """Returns 15:30:00 IST datetime for the given date (default today)."""
        target_date = d or self.now_ist().date()
        return datetime.combine(target_date, self.MARKET_CLOSE, tzinfo=IST_ZONE)

    def get_elapsed_session_seconds(self, dt: Optional[datetime] = None) -> int:
        """
        Returns seconds elapsed since 09:15:00 IST in the current session.
        If before 09:15, returns 0. If after 15:30, caps at 22,500 seconds (6h 15m).
        """
        curr = self.now_ist() if dt is None else self.to_ist(dt)
        if not self.is_trading_day(curr.date()):
            return 0

        curr_time = curr.time()
        if curr_time < self.MARKET_OPEN:
            return 0
        if curr_time >= self.MARKET_CLOSE:
            return int((datetime.combine(curr.date(), self.MARKET_CLOSE) -
                        datetime.combine(curr.date(), self.MARKET_OPEN)).total_seconds())

        delta = datetime.combine(curr.date(), curr_time) - datetime.combine(curr.date(), self.MARKET_OPEN)
        return int(delta.total_seconds())

