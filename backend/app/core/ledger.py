"""
TradeLens AI - Daily Capital & P&L Ledger Engine
Maintains transparent, unbiased record of trading performance, starting from initial capital.
Enforces a hard Daily Circuit Breaker to prevent emotional over-trading / revenge trading.
"""
import os
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, date


LEDGER_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "ledger_store.json"
)


# Full verified trade history reconstructed from broker & co-partner chat logs
CHAT_TRADE_HISTORY = [
    {
        "id": "trade-oct-14-1",
        "date": "2026-10-14",
        "time": "09:47 AM",
        "symbol": "KPITTECH 530 CE",
        "type": "CE",
        "lots": 2,
        "entry_price": 13.80,
        "exit_price": 16.00,
        "lot_size": 775,
        "pnl": 3410.0,
        "status": "WIN",
        "notes": "Target 1 hit cleanly within 10 mins."
    },
    {
        "id": "trade-oct-14-2",
        "date": "2026-10-14",
        "time": "10:25 AM",
        "symbol": "SHRIRAMFIN 940 PE",
        "type": "PE",
        "lots": 5,
        "entry_price": 14.50,
        "exit_price": 20.00,
        "lot_size": 825,
        "pnl": 22687.5,
        "status": "WIN",
        "notes": "All targets hit."
    },
    {
        "id": "trade-oct-14-3",
        "date": "2026-10-14",
        "time": "01:27 PM",
        "symbol": "NIFTY 22800 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 58.00,
        "exit_price": 78.00,
        "lot_size": 65,
        "pnl": 1300.0,
        "status": "WIN",
        "notes": "Booked partial profit at 78."
    },
    {
        "id": "trade-oct-14-4",
        "date": "2026-10-14",
        "time": "11:38 AM",
        "symbol": "TCS 2340 CE",
        "type": "CE",
        "lots": 2,
        "entry_price": 34.15,
        "exit_price": 28.50,
        "lot_size": 175,
        "pnl": -1977.5,
        "status": "LOSS",
        "notes": "Averaged down at 28. Cut on breakdown."
    },
    {
        "id": "trade-oct-14-5",
        "date": "2026-10-14",
        "time": "09:37 AM",
        "symbol": "TATAELXSI 3700 CE",
        "type": "CE",
        "lots": 2,
        "entry_price": 77.00,
        "exit_price": 62.00,
        "lot_size": 100,
        "pnl": -3000.0,
        "status": "LOSS",
        "notes": "Chased entry at 91, averaged at 63.5, stopped out."
    },
    {
        "id": "trade-oct-15-1",
        "date": "2026-10-15",
        "time": "09:34 AM",
        "symbol": "BHARTIATL 1840 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 14.50,
        "exit_price": 22.50,
        "lot_size": 475,
        "pnl": 3800.0,
        "status": "WIN",
        "notes": "15-min morning breakout. All targets hit."
    },
    {
        "id": "trade-oct-15-2",
        "date": "2026-10-15",
        "time": "09:39 AM",
        "symbol": "AXISBANK 1290 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 21.00,
        "exit_price": 25.00,
        "lot_size": 625,
        "pnl": 2500.0,
        "status": "WIN",
        "notes": "Quick morning scalp."
    },
    {
        "id": "trade-oct-15-3",
        "date": "2026-10-15",
        "time": "10:18 AM",
        "symbol": "RELIANCE 1200 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 22.20,
        "exit_price": 24.80,
        "lot_size": 250,
        "pnl": 650.0,
        "status": "WIN",
        "notes": "Target 1 scalp. Exited at 24.80."
    },
    {
        "id": "trade-oct-15-4",
        "date": "2026-10-15",
        "time": "10:31 AM",
        "symbol": "INDUSINDBK 900 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 16.90,
        "exit_price": 15.00,
        "lot_size": 700,
        "pnl": -1330.0,
        "status": "LOSS",
        "notes": "SL hit at 15.00. Disciplined exit."
    },
    {
        "id": "trade-oct-16-1",
        "date": "2026-10-16",
        "time": "09:39 AM",
        "symbol": "LTF 245 PE",
        "type": "PE",
        "lots": 1,
        "entry_price": 3.80,
        "exit_price": 5.00,
        "lot_size": 2000,
        "pnl": 2400.0,
        "status": "WIN",
        "notes": "Target 1 hit."
    },
    {
        "id": "trade-oct-16-2",
        "date": "2026-10-16",
        "time": "10:30 AM",
        "symbol": "PERSISTENT 5600 PE",
        "type": "PE",
        "lots": 1,
        "entry_price": 98.00,
        "exit_price": 133.00,
        "lot_size": 150,
        "pnl": 5250.0,
        "status": "WIN",
        "notes": "Tech breakdown. Ran from 98 to 148."
    },
    {
        "id": "trade-oct-16-3",
        "date": "2026-10-16",
        "time": "09:39 AM",
        "symbol": "RELIANCE 1200 PE",
        "type": "PE",
        "lots": 2,
        "entry_price": 17.62,
        "exit_price": 12.80,
        "lot_size": 250,
        "pnl": -2410.0,
        "status": "LOSS",
        "notes": "Averaged down, held over weekend. Sunk by theta."
    },
    {
        "id": "trade-oct-23-1",
        "date": "2026-10-23",
        "time": "09:41 AM",
        "symbol": "BANDHANBNK 155 PE",
        "type": "PE",
        "lots": 1,
        "entry_price": 2.25,
        "exit_price": 4.25,
        "lot_size": 3600,
        "pnl": 7200.0,
        "status": "WIN",
        "notes": "Rapid banking breakdown."
    },
    {
        "id": "trade-oct-23-2",
        "date": "2026-10-23",
        "time": "09:55 AM",
        "symbol": "MANAPURAM 290 PE",
        "type": "PE",
        "lots": 1,
        "entry_price": 3.30,
        "exit_price": 5.80,
        "lot_size": 3000,
        "pnl": 7500.0,
        "status": "WIN",
        "notes": "Target 2 achieved."
    },
    {
        "id": "trade-oct-23-3",
        "date": "2026-10-23",
        "time": "10:08 AM",
        "symbol": "AUBANK 920 PE",
        "type": "PE",
        "lots": 1,
        "entry_price": 15.40,
        "exit_price": 14.20,
        "lot_size": 1000,
        "pnl": -1200.0,
        "status": "LOSS",
        "notes": "SL hit on quick reversal."
    },
    {
        "id": "trade-oct-23-4",
        "date": "2026-10-23",
        "time": "02:35 PM",
        "symbol": "DLF 690 CE",
        "type": "CE",
        "lots": 4,
        "entry_price": 3.90,
        "exit_price": 1.50,
        "lot_size": 825,
        "pnl": -7920.0,
        "status": "LOSS",
        "notes": "Averaged down 4 lots during expiry week. Severe theta burn."
    },
    {
        "id": "trade-oct-23-5",
        "date": "2026-10-23",
        "time": "02:35 PM",
        "symbol": "FEDERALBNK 350 CE",
        "type": "CE",
        "lots": 2,
        "entry_price": 3.75,
        "exit_price": 1.25,
        "lot_size": 2500,
        "pnl": -6250.0,
        "status": "LOSS",
        "notes": "Held carry-forward past holding period. Theta collapse."
    },
    {
        "id": "trade-nov-04-1",
        "date": "2026-11-04",
        "time": "09:43 AM",
        "symbol": "NESTLEIND 1260 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 28.00,
        "exit_price": 22.00,
        "lot_size": 50,
        "pnl": -300.0,
        "status": "LOSS",
        "notes": "Underlying below 20 EMA. SL hit at 22."
    },
    {
        "id": "trade-nov-04-2",
        "date": "2026-11-04",
        "time": "12:21 PM",
        "symbol": "NIFTY 23800 CE",
        "type": "CE",
        "lots": 1,
        "entry_price": 142.00,
        "exit_price": 182.00,
        "lot_size": 65,
        "pnl": 2600.0,
        "status": "WIN",
        "notes": "Re-entry on afternoon trend continuation."
    }
]


class CapitalLedgerManager:
    def __init__(self, storage_path: str = LEDGER_STORE_PATH):
        self.storage_path = storage_path
        self.data = self._load()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "initial_capital": 50000.0,
            "current_balance": 50000.0,
            "max_daily_loss_pct": 5.0,  # 5% max drawdown allowed per day
            "last_reset_date": str(date.today()),
            "trades": CHAT_TRADE_HISTORY
        }

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r") as f:
                    stored = json.load(f)
                    # If existing file only has <= 3 sample trades, load full verified chat history
                    if len(stored.get("trades", [])) <= 3:
                        stored["trades"] = CHAT_TRADE_HISTORY
                        self._save(stored)
                    return stored
            except Exception as e:
                print(f"[!] Error loading ledger store: {e}. Reverting to default.")
        
        default_data = self._default_state()
        self._save(default_data)
        return default_data

    def _save(self, data: Optional[Dict[str, Any]] = None):
        if data is not None:
            self.data = data
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"[!] Failed to persist ledger: {e}")

    def update_settings(self, initial_capital: float, max_daily_loss_pct: float) -> Dict[str, Any]:
        """Updates base capital settings."""
        self.data["initial_capital"] = float(initial_capital)
        self.data["max_daily_loss_pct"] = float(max_daily_loss_pct)
        self._recalc_balance()
        self._save()
        return self.get_summary()

    def record_trade(
        self,
        symbol: str,
        instrument_type: str,
        lots: int,
        entry_price: float,
        exit_price: float,
        lot_size: int,
        notes: str = ""
    ) -> Dict[str, Any]:
        """Adds a closed trade to the journal and recalculates P&L."""
        qty = lots * lot_size
        pnl = round((exit_price - entry_price) * qty, 2)
        status = "WIN" if pnl > 0 else ("BREAKEVEN" if pnl == 0 else "LOSS")
        
        trade_entry = {
            "id": f"trade-{int(time.time())}",
            "date": str(date.today()),
            "time": datetime.now().strftime("%I:%M %p"),
            "symbol": symbol.strip().upper(),
            "type": instrument_type.strip().upper(),
            "lots": lots,
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "lot_size": lot_size,
            "pnl": pnl,
            "status": status,
            "notes": notes.strip()
        }
        
        self.data.setdefault("trades", []).insert(0, trade_entry)
        self._recalc_balance()
        self._save()
        return self.get_summary()

    def delete_trade(self, trade_id: str) -> Dict[str, Any]:
        """Deletes a specific trade by ID and recalculates balance."""
        self.data["trades"] = [t for t in self.data.get("trades", []) if t.get("id") != trade_id]
        self._recalc_balance()
        self._save()
        return self.get_summary()

    def clear_all_trades(self) -> Dict[str, Any]:
        """Clears all trades for a fresh start."""
        self.data["trades"] = []
        self._recalc_balance()
        self._save()
        return self.get_summary()

    def load_chat_history(self) -> Dict[str, Any]:
        """Reloads the full historical trade history from the chat."""
        self.data["trades"] = CHAT_TRADE_HISTORY
        self._recalc_balance()
        self._save()
        return self.get_summary()

    def _recalc_balance(self):
        """Recalculates net balance based on initial capital plus all historical trades."""
        total_pnl = sum(t.get("pnl", 0.0) for t in self.data.get("trades", []))
        self.data["current_balance"] = round(self.data.get("initial_capital", 50000.0) + total_pnl, 2)

    def get_summary(self) -> Dict[str, Any]:
        """Computes comprehensive daily and overall capital metrics."""
        self._recalc_balance()
        today_str = str(date.today())
        all_trades = self.data.get("trades", [])
        
        today_trades = [t for t in all_trades if t.get("date") == today_str]
        today_pnl = round(sum(t.get("pnl", 0.0) for t in today_trades), 2)
        
        initial_cap = self.data.get("initial_capital", 50000.0)
        current_bal = self.data.get("current_balance", initial_cap)
        max_daily_loss_pct = self.data.get("max_daily_loss_pct", 5.0)
        
        # Max loss permitted today in ₹
        max_daily_loss_rupees = round(initial_cap * (max_daily_loss_pct / 100.0), 2)
        
        # Remaining loss buffer before circuit breaker triggers
        remaining_loss_buffer = round(max(0.0, max_daily_loss_rupees + today_pnl), 2)
        
        # Circuit Breaker Triggered if today's loss >= max daily loss
        circuit_breaker_triggered = today_pnl <= -max_daily_loss_rupees
        
        wins = [t for t in today_trades if t.get("status") == "WIN"]
        losses = [t for t in today_trades if t.get("status") == "LOSS"]
        total_today = len(today_trades)
        win_rate_pct = round((len(wins) / total_today * 100), 1) if total_today > 0 else 0.0
        
        overall_pnl = round(current_bal - initial_cap, 2)
        overall_pnl_pct = round((overall_pnl / max(1.0, initial_cap)) * 100, 2)
        
        return {
            "initial_capital": initial_cap,
            "current_balance": current_bal,
            "overall_pnl": overall_pnl,
            "overall_pnl_pct": overall_pnl_pct,
            "today_pnl": today_pnl,
            "today_trades_count": total_today,
            "today_wins": len(wins),
            "today_losses": len(losses),
            "today_win_rate_pct": win_rate_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_daily_loss_rupees": max_daily_loss_rupees,
            "remaining_loss_buffer": remaining_loss_buffer,
            "circuit_breaker_triggered": circuit_breaker_triggered,
            "trades": all_trades
        }


# Singleton instance
ledger_manager = CapitalLedgerManager()
