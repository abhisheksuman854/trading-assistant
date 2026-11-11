"""
TradeLens AI - macOS Power, Sleep, Lid-Closure & Heartbeat Recovery Monitor.

Non-invasive policy:
- Never mutates global macOS pmset settings without explicit owner approval.
- Allows display sleep at all times (never uses `caffeinate -d`).
- Prevents unintended idle system sleep only when connected to AC power during
  configured trading hours (`caffeinate -i -s -w <pid>`).
- Detects unexpected system sleep (wall-vs-monotonic clock drift) and unexpected
  shutdown / power loss (`clean_shutdown == False` across persisted heartbeats).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.app.data.session_clock import IST_ZONE, IndianMarketSessionClock
from .security import _atomic_write_json


MACBOOK_POWER_AND_LID_LIMITATIONS: Tuple[str, ...] = (
    "MacBook lid closure forces macOS clamshell sleep unless connected to AC power + external display + HID/clamshell mode; keep lid open or cracked on AC power during trading hours.",
    "Operating solely on Battery Power triggers aggressive macOS sleep/standby (pmset sleep=1, displaysleep=2 on inspected MacBookAir10,1) and risks sudden shutdown during market hours.",
    "Home Mac does not modify global pmset defaults automatically; runtime keep-awake uses a scoped child `caffeinate -i -s` assertion only while on AC power during trading hours.",
)


@dataclass
class TradingHoursPolicy:
    timezone_name: str = "Asia/Kolkata"
    start_hhmm: str = "09:00"
    end_hhmm: str = "15:45"
    require_nse_trading_day: bool = True
    allow_display_sleep: bool = True
    prevent_idle_sleep_on_ac: bool = True
    release_assertion_on_battery: bool = True
    sleep_gap_threshold_seconds: float = 20.0
    never_mutate_global_pmset: bool = True

    def is_within_trading_window(
        self,
        now_utc: Optional[datetime] = None,
        session_clock: Optional[IndianMarketSessionClock] = None,
    ) -> Tuple[bool, str]:
        ref = now_utc or datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        local_dt = ref.astimezone(IST_ZONE)

        if self.require_nse_trading_day:
            clock = session_clock or IndianMarketSessionClock()
            if not clock.is_trading_day(local_dt.date()):
                return False, "NON_TRADING_DAY_OR_HOLIDAY"

        sh, sm = (int(x) for x in self.start_hhmm.split(":"))
        eh, em = (int(x) for x in self.end_hhmm.split(":"))
        start_t = dt_time(sh, sm)
        end_t = dt_time(eh, em)
        cur_t = local_dt.time().replace(tzinfo=None)
        if start_t <= cur_t <= end_t:
            return True, "WITHIN_CONFIGURED_TRADING_HOURS"
        return False, "OUTSIDE_CONFIGURED_TRADING_HOURS"


@dataclass
class HostPowerSnapshot:
    hardware_model: str
    macos_version: str
    power_source: str  # "AC_POWER" | "BATTERY_POWER" | "UNKNOWN"
    battery_pct: Optional[int]
    pmset_custom: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    has_battery_and_lid: bool = True
    global_pmset_modified: bool = False
    warnings: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=lambda: list(MACBOOK_POWER_AND_LID_LIMITATIONS))


@dataclass
class PowerPolicyEvaluation:
    should_hold_awake_assertion: bool
    display_sleep_allowed: bool
    caffeinate_args: List[str]
    policy_status: str
    window_reason: str
    snapshot: HostPowerSnapshot
    warnings: List[str] = field(default_factory=list)


def _default_cmd_runner(cmd: List[str], timeout: float = 3.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def parse_pmset_custom(raw_output: str) -> Dict[str, Dict[str, Any]]:
    sections: Dict[str, Dict[str, Any]] = {}
    current: Optional[str] = None
    for line in (raw_output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":"):
            current = stripped[:-1]
            sections.setdefault(current, {})
            continue
        if current and not stripped.startswith("Now drawing"):
            parts = stripped.split()
            if len(parts) >= 2:
                key = parts[0]
                val_raw = " ".join(parts[1:])
                try:
                    val: Any = int(val_raw)
                except ValueError:
                    val = val_raw
                sections[current][key] = val
    return sections


def parse_pmset_batt(raw_output: str) -> Tuple[str, Optional[int]]:
    text = raw_output or ""
    source = "UNKNOWN"
    if "AC Power" in text:
        source = "AC_POWER"
    elif "Battery Power" in text:
        source = "BATTERY_POWER"
    match = re.search(r"(\d{1,3})%", text)
    pct = int(match.group(1)) if match else None
    return source, pct


class PowerAndSleepMonitor:
    """
    Observes macOS power/sleep configuration, manages scoped `caffeinate -i -s`
    during AC trading hours, and detects unexpected sleep or dirty shutdown/power loss.
    """

    def __init__(
        self,
        heartbeat_path: Path | str,
        policy: Optional[TradingHoursPolicy] = None,
        cmd_runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        mono_clock: Optional[Callable[[], float]] = None,
        on_unexpected_sleep: Optional[Callable[[float, str], None]] = None,
        session_clock: Optional[IndianMarketSessionClock] = None,
    ) -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.policy = policy or TradingHoursPolicy()
        self._cmd_runner = cmd_runner or _default_cmd_runner
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._mono_clock = mono_clock or time.monotonic
        self._on_unexpected_sleep = on_unexpected_sleep
        self._session_clock = session_clock

        self._prev_wall: Optional[datetime] = None
        self._prev_mono: Optional[float] = None
        self.unexpected_sleep_detected: bool = False
        self.sleep_events_count: int = 0
        self.last_sleep_gap_seconds: float = 0.0
        self.last_sleep_detected_at: Optional[str] = None
        self.unexpected_shutdown_detected: bool = False
        self.previous_unclean_heartbeat_at: Optional[str] = None
        self._caffeinate_proc: Optional[subprocess.Popen[Any]] = None

    def _now_utc(self) -> datetime:
        dt = self._wall_clock()
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    def inspect_host_power(self) -> HostPowerSnapshot:
        model = "Unknown-Mac"
        rc_m, out_m, _ = self._cmd_runner(["sysctl", "-n", "hw.model"])
        if rc_m == 0 and out_m.strip():
            model = out_m.strip()

        macos_ver = platform.mac_ver()[0] or platform.release()
        rc_v, out_v, _ = self._cmd_runner(["sw_vers", "-productVersion"])
        if rc_v == 0 and out_v.strip():
            macos_ver = out_v.strip()

        rc_c, out_c, _ = self._cmd_runner(["pmset", "-g", "custom"])
        custom_map = parse_pmset_custom(out_c) if rc_c == 0 else {}

        rc_b, out_b, _ = self._cmd_runner(["pmset", "-g", "batt"])
        source, pct = parse_pmset_batt(out_b) if rc_b == 0 else ("UNKNOWN", None)
        if source == "UNKNOWN" and "Battery Power" in out_c:
            source = "BATTERY_POWER"
        elif source == "UNKNOWN" and "AC Power" in out_c:
            source = "AC_POWER"

        is_laptop = "Book" in model or pct is not None or "Battery Power" in custom_map
        warnings: List[str] = []
        ac_sleep = custom_map.get("AC Power", {}).get("sleep")
        if isinstance(ac_sleep, int) and ac_sleep > 0:
            warnings.append(
                f"macOS AC Power pmset sleep={ac_sleep}m; scoped caffeinate (-i -s) is needed during market hours."
            )
        if source == "BATTERY_POWER":
            warnings.append(
                f"Home Mac running on Battery Power ({pct if pct is not None else '?'}%): remote continuity at risk if battery drains or lid closes."
            )

        return HostPowerSnapshot(
            hardware_model=model,
            macos_version=macos_ver,
            power_source=source,
            battery_pct=pct,
            pmset_custom=custom_map,
            has_battery_and_lid=is_laptop,
            global_pmset_modified=False,
            warnings=warnings,
            limitations=list(MACBOOK_POWER_AND_LID_LIMITATIONS) if is_laptop else [],
        )

    def evaluate_policy(self, now_utc: Optional[datetime] = None) -> PowerPolicyEvaluation:
        snapshot = self.inspect_host_power()
        in_window, window_reason = self.policy.is_within_trading_window(
            now_utc=now_utc or self._now_utc(),
            session_clock=self._session_clock,
        )
        warnings = list(snapshot.warnings)
        should_hold = False
        policy_status = "OUTSIDE_TRADING_HOURS"

        if in_window:
            if snapshot.power_source == "BATTERY_POWER" and self.policy.release_assertion_on_battery:
                should_hold = False
                policy_status = "DEGRADED_ON_BATTERY_NO_AWAKE_ASSERTION"
                warnings.append("Running on battery during trading hours: keep-awake assertion withheld to avoid unsafe battery exhaustion.")
            elif snapshot.power_source == "AC_POWER" and self.policy.prevent_idle_sleep_on_ac:
                should_hold = True
                policy_status = "AWAKE_TRADING_HOURS_AC"
            else:
                should_hold = False
                policy_status = "DEGRADED_POWER_SOURCE_UNKNOWN"
                warnings.append("Unable to confirm AC power; power continuity not guaranteed.")

        # Crucially: NEVER include '-d' so display sleep remains permitted!
        caffeinate_args = ["-i", "-s"] if (should_hold and self.policy.allow_display_sleep) else []

        return PowerPolicyEvaluation(
            should_hold_awake_assertion=should_hold,
            display_sleep_allowed=self.policy.allow_display_sleep,
            caffeinate_args=caffeinate_args,
            policy_status=policy_status,
            window_reason=window_reason,
            snapshot=snapshot,
            warnings=warnings,
        )

    def inspect_previous_shutdown(self) -> Dict[str, Any]:
        """Inspect persisted heartbeat file to detect prior power loss or crash."""
        if not self.heartbeat_path.exists():
            return {
                "prior_state_present": False,
                "unexpected_shutdown_detected": False,
                "last_heartbeat_utc": None,
            }
        try:
            with open(self.heartbeat_path, "r", encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:
            self.unexpected_shutdown_detected = True
            return {
                "prior_state_present": True,
                "unexpected_shutdown_detected": True,
                "last_heartbeat_utc": None,
                "reason": "CORRUPTED_HEARTBEAT_FILE",
            }

        clean = bool(prev.get("clean_shutdown", True))
        if not clean:
            self.unexpected_shutdown_detected = True
            self.previous_unclean_heartbeat_at = prev.get("last_heartbeat_utc")
        return {
            "prior_state_present": True,
            "unexpected_shutdown_detected": not clean,
            "last_heartbeat_utc": prev.get("last_heartbeat_utc"),
            "previous_pid": prev.get("pid"),
            "shutdown_reason": prev.get("shutdown_reason"),
        }

    def record_tick(
        self,
        now_utc: Optional[datetime] = None,
        now_mono: Optional[float] = None,
        persist: bool = True,
        pid: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Record a runtime tick and detect unexpected system sleep via clock jump."""
        wall_now = now_utc or self._now_utc()
        if wall_now.tzinfo is None:
            wall_now = wall_now.replace(tzinfo=timezone.utc)
        mono_now = self._mono_clock() if now_mono is None else float(now_mono)

        sleep_detected_now = False
        drift_seconds = 0.0
        if self._prev_wall is not None and self._prev_mono is not None:
            wall_delta = (wall_now - self._prev_wall).total_seconds()
            mono_delta = mono_now - self._prev_mono
            drift_seconds = wall_delta - mono_delta
            if drift_seconds > self.policy.sleep_gap_threshold_seconds:
                sleep_detected_now = True
                self.unexpected_sleep_detected = True
                self.sleep_events_count += 1
                self.last_sleep_gap_seconds = round(drift_seconds, 2)
                self.last_sleep_detected_at = wall_now.isoformat()
                if self._on_unexpected_sleep:
                    self._on_unexpected_sleep(drift_seconds, self.last_sleep_detected_at)

        self._prev_wall = wall_now
        self._prev_mono = mono_now

        if persist:
            _atomic_write_json(
                self.heartbeat_path,
                {
                    "pid": pid or os.getpid(),
                    "last_heartbeat_utc": wall_now.isoformat(),
                    "clean_shutdown": False,
                    "sleep_events_count": self.sleep_events_count,
                    "last_sleep_detected_at": self.last_sleep_detected_at,
                },
            )

        return {
            "sleep_detected_this_tick": sleep_detected_now,
            "drift_seconds": round(drift_seconds, 2),
            "unexpected_sleep_detected": self.unexpected_sleep_detected,
            "sleep_events_count": self.sleep_events_count,
            "last_sleep_detected_at": self.last_sleep_detected_at,
        }

    def mark_clean_shutdown(self, reason: str = "CLEAN_SHUTDOWN") -> None:
        self.stop_caffeinate()
        now_iso = self._now_utc().isoformat()
        _atomic_write_json(
            self.heartbeat_path,
            {
                "pid": os.getpid(),
                "last_heartbeat_utc": now_iso,
                "clean_shutdown": True,
                "shutdown_reason": reason,
                "sleep_events_count": self.sleep_events_count,
                "last_sleep_detected_at": self.last_sleep_detected_at,
            },
        )

    def sync_caffeinate(self, watched_pid: Optional[int] = None, enable_process: bool = False) -> Dict[str, Any]:
        """
        Start or stop child `caffeinate -i -s -w <pid>` according to current policy.
        `enable_process=False` lets tests or dry-runs evaluate policy without spawning OS binary.
        """
        evaluation = self.evaluate_policy()
        if not enable_process:
            return {
                "caffeinate_active": False,
                "dry_run": True,
                "evaluation": asdict(evaluation),
            }

        caffeinate_bin = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        if evaluation.should_hold_awake_assertion and os.path.exists(caffeinate_bin):
            if self._caffeinate_proc is None or self._caffeinate_proc.poll() is not None:
                args = [caffeinate_bin] + list(evaluation.caffeinate_args)
                if watched_pid:
                    args.extend(["-w", str(watched_pid)])
                self._caffeinate_proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            self.stop_caffeinate()

        active = self._caffeinate_proc is not None and self._caffeinate_proc.poll() is None
        return {
            "caffeinate_active": active,
            "caffeinate_pid": self._caffeinate_proc.pid if active and self._caffeinate_proc else None,
            "dry_run": False,
            "evaluation": asdict(evaluation),
        }

    def stop_caffeinate(self) -> None:
        if self._caffeinate_proc is not None:
            if self._caffeinate_proc.poll() is None:
                self._caffeinate_proc.terminate()
                try:
                    self._caffeinate_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._caffeinate_proc.kill()
            self._caffeinate_proc = None

    def get_status(self, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        eval_res = self.evaluate_policy(now_utc=now_utc)
        caffeinate_running = self._caffeinate_proc is not None and self._caffeinate_proc.poll() is None
        return {
            "hardware_model": eval_res.snapshot.hardware_model,
            "macos_version": eval_res.snapshot.macos_version,
            "power_source": eval_res.snapshot.power_source,
            "battery_pct": eval_res.snapshot.battery_pct,
            "policy_status": eval_res.policy_status,
            "window_reason": eval_res.window_reason,
            "should_hold_awake_assertion": eval_res.should_hold_awake_assertion,
            "display_sleep_allowed": eval_res.display_sleep_allowed,
            "caffeinate_active": caffeinate_running,
            "global_pmset_modified": False,
            "unexpected_sleep_detected": self.unexpected_sleep_detected,
            "sleep_events_count": self.sleep_events_count,
            "last_sleep_gap_seconds": self.last_sleep_gap_seconds,
            "last_sleep_detected_at": self.last_sleep_detected_at,
            "unexpected_shutdown_detected": self.unexpected_shutdown_detected,
            "previous_unclean_heartbeat_at": self.previous_unclean_heartbeat_at,
            "warnings": eval_res.warnings,
            "limitations": eval_res.snapshot.limitations,
        }

