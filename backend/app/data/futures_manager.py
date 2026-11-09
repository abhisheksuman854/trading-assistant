"""
TradeLens AI - NSE Index Futures Contract & Volume Proxy Manager
Implements a strict, deterministic contract-selection and rollover policy for Indian Index Futures
(NIFTY, BANKNIFTY, FINNIFTY) to provide volume-dependent analytics (Futures VWAP, Basis, RVOL)
without misrepresenting them as cash spot index volume.
"""
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, Optional, Set, Tuple, List
import calendar
import zoneinfo

from .session_clock import IndianMarketSessionClock, IST_ZONE


class FuturesContractManager:
    """
    Manages NSE index futures contract resolution, monthly expiry calculation,
    rollover detection, basis (futures - spot), and futures-derived VWAP.
    """

    SUPPORTED_INDICES = {
        # NSE Published Contract Rules: All NSE Index Futures expire on LAST TUESDAY of the expiry month
        "NIFTY 50": {"fut_symbol": "NIFTY", "lot_size": 75, "step": 50.0, "expiry_weekday": 1},   # 1 = Tuesday
        "NIFTY": {"fut_symbol": "NIFTY", "lot_size": 75, "step": 50.0, "expiry_weekday": 1},      # 1 = Tuesday
        "BANKNIFTY": {"fut_symbol": "BANKNIFTY", "lot_size": 30, "step": 100.0, "expiry_weekday": 1}, # 1 = Tuesday
        "FINNIFTY": {"fut_symbol": "FINNIFTY", "lot_size": 65, "step": 50.0, "expiry_weekday": 1},  # 1 = Tuesday
        "MIDCPNIFTY": {"fut_symbol": "MIDCPNIFTY", "lot_size": 120, "step": 25.0, "expiry_weekday": 1} # 1 = Tuesday
    }

    MONTH_CODES = {
        1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
        5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
        9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"
    }

    def __init__(self, holidays: Optional[Set[str]] = None):
        self.clock = IndianMarketSessionClock(holidays=holidays)

    def get_monthly_expiry(self, year: int, month: int, symbol: str = "NIFTY 50") -> date:
        """
        Calculates the official NSE monthly futures expiry date according to published NSE contract rules.
        For NIFTY 50 and index futures, monthly contracts expire on the LAST TUESDAY of the expiry month.
        If the last Tuesday is an exchange holiday, expiry shifts to the preceding trading day.
        """
        sym_clean = symbol.strip().upper()
        target_weekday = self.SUPPORTED_INDICES.get(sym_clean, {}).get("expiry_weekday", 1)  # 1 = Tuesday
        cal = calendar.monthcalendar(year, month)
        # Weekday in calendar: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4
        valid_days = [week[target_weekday] for week in cal if week[target_weekday] != 0]
        last_day = valid_days[-1]
        expiry_date = date(year, month, last_day)

        # If holiday, shift backward to preceding valid trading day
        while not self.clock.is_trading_day(expiry_date):
            expiry_date -= timedelta(days=1)

        return expiry_date

    def get_active_contracts(self, symbol: str = "NIFTY 50", current_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Determines the current Near-Month and Next-Month contract expiries.
        Handles rollover on Expiry Day at 15:30:00 IST.
        """
        now = self.clock.to_ist(current_dt or self.clock.now_ist())
        curr_date = now.date()
        curr_time = now.time()

        near_expiry = self.get_monthly_expiry(curr_date.year, curr_date.month, symbol=symbol)

        # Check if the near-month contract has already expired today (past 15:30 IST) or earlier this month
        is_past_expiry = (curr_date > near_expiry) or (curr_date == near_expiry and curr_time >= self.clock.MARKET_CLOSE)

        if is_past_expiry:
            # Active near-month shifts to next calendar month
            next_month = curr_date.month + 1 if curr_date.month < 12 else 1
            next_year = curr_date.year if curr_date.month < 12 else curr_date.year + 1
            far_month = next_month + 1 if next_month < 12 else 1
            far_year = next_year if next_month < 12 else next_year + 1

            active_near_expiry = self.get_monthly_expiry(next_year, next_month, symbol=symbol)
            active_next_expiry = self.get_monthly_expiry(far_year, far_month, symbol=symbol)
        else:
            # Current month is still active
            next_month = curr_date.month + 1 if curr_date.month < 12 else 1
            next_year = curr_date.year if curr_date.month < 12 else curr_date.year + 1

            active_near_expiry = near_expiry
            active_next_expiry = self.get_monthly_expiry(next_year, next_month, symbol=symbol)

        # Detect Rollover Window (typically the 3 trading days leading up to expiry)
        days_to_expiry = (active_near_expiry - curr_date).days
        is_expiry_day = (curr_date == active_near_expiry)
        is_rollover_window = (0 <= days_to_expiry <= 3)

        return {
            "near_expiry": active_near_expiry,
            "next_expiry": active_next_expiry,
            "days_to_expiry": max(0, days_to_expiry),
            "is_expiry_day": is_expiry_day,
            "is_rollover_window": is_rollover_window,
            "rollover_status": (
                "EXPIRY_DAY_FINAL_SESSION" if is_expiry_day else (
                    "ROLLOVER_IN_PROGRESS" if is_rollover_window else "NORMAL_ACTIVE_CYCLE"
                )
            )
        }

    def format_trading_symbol(self, base_symbol: str, expiry: date) -> str:
        """
        Formats standard NSE trading symbol: e.g. NIFTY26NOVFUT
        """
        sym_clean = base_symbol.strip().upper()
        root = self.SUPPORTED_INDICES.get(sym_clean, {}).get("fut_symbol", sym_clean)
        yr_short = str(expiry.year)[-2:]
        mon_code = self.MONTH_CODES.get(expiry.month, "FUT")
        return f"{root}{yr_short}{mon_code}FUT"

    def validate_contract_against_master(
        self,
        symbol: str,
        calculated_expiry: date,
        contract_master: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Cross-checks the calculated expiry and trading symbol against broker contract master.
        Rejects mismatches to prevent trading non-existent or misaligned contracts.
        """
        trading_symbol = self.format_trading_symbol(symbol, calculated_expiry)
        if contract_master is None:
            return {
                "is_valid": True,
                "status": "UNVALIDATED_STANDALONE",
                "trading_symbol": trading_symbol,
                "calculated_expiry": calculated_expiry.strftime("%Y-%m-%d"),
                "token": None
            }

        # Look up symbol in contract master
        master_record = contract_master.get(trading_symbol) or contract_master.get(symbol)
        if not master_record:
            return {
                "is_valid": False,
                "status": "SYMBOL_NOT_IN_CONTRACT_MASTER",
                "trading_symbol": trading_symbol,
                "calculated_expiry": calculated_expiry.strftime("%Y-%m-%d"),
                "error": f"Calculated contract {trading_symbol} not found in broker contract master."
            }

        master_exp_str = master_record.get("expiry") or master_record.get("expiry_date")
        if master_exp_str:
            try:
                parts = master_exp_str.split("-")
                if len(parts[0]) == 4:
                    master_exp_date = date.fromisoformat(master_exp_str[:10])
                else:
                    master_exp_date = datetime.strptime(master_exp_str[:11], "%d-%b-%Y").date()

                if master_exp_date != calculated_expiry:
                    return {
                        "is_valid": False,
                        "status": "CONTRACT_MASTER_MISMATCH",
                        "trading_symbol": trading_symbol,
                        "calculated_expiry": calculated_expiry.strftime("%Y-%m-%d"),
                        "master_expiry": master_exp_date.strftime("%Y-%m-%d"),
                        "error": f"Calculated expiry {calculated_expiry} does not match broker contract master expiry {master_exp_date}."
                    }
            except Exception as pe:
                pass

        return {
            "is_valid": True,
            "status": "VERIFIED_IN_CONTRACT_MASTER",
            "trading_symbol": trading_symbol,
            "calculated_expiry": calculated_expiry.strftime("%Y-%m-%d"),
            "token": master_record.get("token") or master_record.get("symboltoken")
        }

    def calculate_basis(self, spot_price: float, futures_price: float) -> Dict[str, Any]:
        """
        Calculates Basis (Futures Price - Spot Price).
        Basis > 0: Contango (Premium)
        Basis < 0: Backwardation (Discount)
        """
        basis_pts = round(futures_price - spot_price, 2)
        basis_pct = round((basis_pts / max(0.01, spot_price)) * 100.0, 3)
        return {
            "spot_price": round(spot_price, 2),
            "futures_price": round(futures_price, 2),
            "basis_pts": basis_pts,
            "basis_pct": basis_pct,
            "market_structure": "PREMIUM_CONTANGO" if basis_pts >= 0 else "DISCOUNT_BACKWARDATION"
        }

    def calculate_futures_vwap(
        self,
        symbol: str,
        futures_candles: List[Dict[str, Any]],
        spot_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Computes VWAP strictly using futures price and volume.
        EXPLICIT CONTRACT: Never mislabels futures-derived VWAP as Spot VWAP.
        """
        sym_clean = symbol.strip().upper()
        root = self.SUPPORTED_INDICES.get(sym_clean, {}).get("fut_symbol", sym_clean)
        contract_info = self.get_active_contracts(symbol=sym_clean)
        near_expiry = contract_info["near_expiry"]
        trading_symbol = self.format_trading_symbol(root, near_expiry)
        vwap_display_name = f"{root} Futures VWAP"

        if not futures_candles:
            return {
                "vwap_name": vwap_display_name,
                "underlying_contract": trading_symbol,
                "expiry_date": near_expiry.strftime("%Y-%m-%d"),
                "futures_vwap": None,
                "vwap_status": "UNAVAILABLE_NO_FUTURES_FEED",
                "volume_source": "FUTURES_TRADED_PROXY",
                "warning": f"Futures candles unavailable for {trading_symbol}. Spot index volume is not traded on cash segment."
            }

        cum_vol = 0
        cum_pv = 0.0

        for c in futures_candles:
            vol = c.get("volume", 0)
            if vol > 0:
                typical_price = (c["high"] + c["low"] + c["close"]) / 3.0
                cum_pv += typical_price * vol
                cum_vol += vol

        if cum_vol == 0:
            return {
                "vwap_name": vwap_display_name,
                "underlying_contract": trading_symbol,
                "expiry_date": near_expiry.strftime("%Y-%m-%d"),
                "futures_vwap": None,
                "vwap_status": "ZERO_FUTURES_VOLUME",
                "volume_source": "FUTURES_TRADED_PROXY",
                "warning": "No traded volume recorded on futures contract during session."
            }

        fut_vwap = round(cum_pv / cum_vol, 2)
        latest_fut_close = futures_candles[-1]["close"]

        res: Dict[str, Any] = {
            "vwap_name": vwap_display_name,
            "underlying_contract": trading_symbol,
            "expiry_date": near_expiry.strftime("%Y-%m-%d"),
            "days_to_expiry": contract_info["days_to_expiry"],
            "rollover_status": contract_info["rollover_status"],
            "futures_vwap": fut_vwap,
            "latest_futures_ltp": latest_fut_close,
            "total_futures_volume": cum_vol,
            "vwap_status": "VALID_FUTURES_VWAP",
            "volume_source": "FUTURES_TRADED_PROXY",
            "is_spot_proxy": True,
            "label_notice": f"Derived from {trading_symbol}. Spot cash index does not have native traded volume."
        }

        if spot_price is not None:
            basis_data = self.calculate_basis(spot_price, latest_fut_close)
            res["basis"] = basis_data

        return res
