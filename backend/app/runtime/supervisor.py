"""
TradeLens AI - macOS Process Supervisor, Single-Instance Lock & Bounded Crash Recovery.

Responsibilities:
- Prevent duplicate runtime instances via advisory `fcntl.flock` + PID metadata JSON.
- Automatically detect and recover from stale PID files left by unexpected termination/power loss.
- Supervise backend and frontend child processes with periodic health checks.
- Restart crashed children with exponential backoff bounded by a sliding-window circuit breaker
  (never enters an infinite restart loop).
- Support graceful shutdown (SIGTERM cleanup) and explicit owner-controlled shutdown
  (`owner_shutdown.flag` -> exit cleanly with code 0 so launchd SuccessfulExit=false stays stopped).
- Emit secret-redacted lifecycle logs to rotating file/stderr.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .power_policy import PowerAndSleepMonitor
from .security import (
    RedactingLogFilter,
    _atomic_write_json,
    collect_known_secrets,
    redact_payload,
    redact_runtime_text,
)


class DuplicateRuntimeError(RuntimeError):
    """Raised when another active TradeLens supervisor instance already owns the lock."""


@dataclass
class RestartPolicy:
    max_restarts: int = 4
    window_seconds: float = 300.0
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 30.0


@dataclass
class SupervisedServiceSpec:
    name: str
    command: List[str]
    health_url: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    startup_grace_seconds: float = 5.0
    max_consecutive_health_failures: int = 3
    enabled: bool = True


@dataclass
class SupervisedServiceState:
    name: str
    status: str = "STOPPED"  # RUNNING | CRASHED | RESTARTING | STOPPED | CIRCUIT_OPEN
    pid: Optional[int] = None
    restart_count_total: int = 0
    recent_restarts: Deque[float] = field(default_factory=deque)
    consecutive_health_failures: int = 0
    next_restart_mono: Optional[float] = None
    last_exit_code: Optional[int] = None
    last_failure_reason: Optional[str] = None
    last_started_at: Optional[str] = None
    last_stopped_at: Optional[str] = None
    started_at_mono: float = 0.0
    circuit_open: bool = False


class SingleInstanceLock:
    """
    Combines OS `fcntl.flock` with a JSON PID metadata file to prevent duplicate processes
    and safely recover from stale PID files after power loss or SIGKILL.
    """

    def __init__(
        self,
        pid_path: Path | str,
        lock_path: Optional[Path | str] = None,
        process_alive_checker: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self.pid_path = Path(pid_path)
        self.lock_path = Path(lock_path) if lock_path else self.pid_path.with_suffix(".lock")
        self._process_alive = process_alive_checker or self._default_process_alive
        self._lock_fh: Optional[Any] = None
        self.stale_pid_recovered: bool = False
        self.recovered_stale_pid: Optional[int] = None

    @staticmethod
    def _default_process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return exc.errno != errno.ESRCH

    def acquire(self, role: str = "supervisor") -> Dict[str, Any]:
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            existing = self.read_pid_metadata()
            raise DuplicateRuntimeError(
                f"Another active TradeLens {role} instance already holds {self.lock_path} "
                f"(pid={existing.get('pid') if existing else 'unknown'})."
            ) from exc

        # Lock is now exclusively ours; inspect whether a leftover PID file is stale.
        existing = self.read_pid_metadata()
        if existing:
            old_pid = int(existing.get("pid") or 0)
            if old_pid and old_pid != os.getpid():
                if not self._process_alive(old_pid):
                    self.stale_pid_recovered = True
                    self.recovered_stale_pid = old_pid
                else:
                    # Flock succeeded so the old PID cannot hold this lock, yet if the OS reused
                    # an unrelated PID we still treat our old PID file as stale after lock win.
                    self.stale_pid_recovered = True
                    self.recovered_stale_pid = old_pid

        self._lock_fh = fh
        meta = {
            "pid": os.getpid(),
            "role": role,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "stale_pid_recovered": self.stale_pid_recovered,
            "recovered_stale_pid": self.recovered_stale_pid,
        }
        _atomic_write_json(self.pid_path, meta)
        return meta

    def read_pid_metadata(self) -> Optional[Dict[str, Any]]:
        if not self.pid_path.exists():
            return None
        try:
            with open(self.pid_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except Exception:
            return {"pid": None, "corrupted": True}

    def release(self) -> None:
        if self._lock_fh is not None:
            try:
                if self.pid_path.exists():
                    meta = self.read_pid_metadata()
                    if not meta or meta.get("pid") in (None, os.getpid()):
                        self.pid_path.unlink(missing_ok=True)
            finally:
                try:
                    fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                self._lock_fh.close()
                self._lock_fh = None


def build_redacted_logger(
    name: str,
    log_path: Path | str,
    extra_secrets_provider: Optional[Callable[[], List[str]]] = None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)

    lpath = Path(log_path)
    lpath.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    filt = RedactingLogFilter(extra_secrets_provider=extra_secrets_provider)

    file_handler = RotatingFileHandler(str(lpath), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(filt)
    logger.addHandler(file_handler)

    return logger


def default_http_health_probe(
    url: str,
    timeout: float = 2.0,
    probe_secret: Optional[str] = None,
    tls_ca_file: Optional[str | Path] = None,
) -> Tuple[bool, str]:
    try:
        headers = {"X-TradeLens-Supervisor-Probe": probe_secret} if probe_secret else {}
        req = urllib.request.Request(url, method="GET", headers=headers)
        context: Optional[ssl.SSLContext] = None
        if url.lower().startswith("https://"):
            ca_path = str(tls_ca_file) if tls_ca_file else None
            # Enforce full OpenSSL X509 strict validation + hostname/IP SAN verification.
            context = ssl.create_default_context(cafile=ca_path)
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            ok = 200 <= resp.status < 300
            return ok, f"HTTP_{resp.status}"
    except Exception as exc:
        extra = [probe_secret] if probe_secret else None
        return False, redact_runtime_text(str(exc), extra_secrets=extra)


def stop_running_supervisor(
    state_dir: Path | str,
    timeout_seconds: float = 8.0,
) -> Dict[str, Any]:
    """
    Gracefully stop an active supervisor process via SIGTERM using supervisor.pid
    without creating owner_shutdown.flag (suitable for `stop` / `restart` maintenance).
    """
    sdir = Path(state_dir)
    pid_path = sdir / "supervisor.pid"
    if not pid_path.exists():
        return {"stopped": False, "status": "NOT_RUNNING", "pid": None}
    try:
        meta = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(meta.get("pid") or 0)
    except Exception:
        pid = 0
    if pid <= 0 or pid == os.getpid():
        return {"stopped": False, "status": "NO_VALID_PID", "pid": None}
    try:
        os.kill(pid, 0)
    except OSError:
        return {"stopped": False, "status": "STALE_PID_NOT_ALIVE", "pid": pid}

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return {"stopped": True, "status": "STOPPED_GRACEFULLY", "pid": pid}
        time.sleep(0.1)
    return {"stopped": False, "status": "STOP_TIMEOUT_STILL_RUNNING", "pid": pid}


class TradeLensSupervisor:
    """
    Single-instance supervisor orchestrating backend & frontend services with
    bounded restarts, stale PID cleanup, power assertions, and owner shutdown.
    """

    def __init__(
        self,
        state_dir: Path | str,
        services: List[SupervisedServiceSpec],
        restart_policy: Optional[RestartPolicy] = None,
        power_monitor: Optional[PowerAndSleepMonitor] = None,
        enable_caffeinate_process: bool = False,
        health_probe: Optional[Callable[[str], Tuple[bool, str]]] = None,
        process_spawner: Optional[Callable[[SupervisedServiceSpec], Any]] = None,
        mono_clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        extra_secrets_provider: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.specs: Dict[str, SupervisedServiceSpec] = {s.name: s for s in services if s.enabled}
        self.states: Dict[str, SupervisedServiceState] = {
            name: SupervisedServiceState(name=name) for name in self.specs
        }
        self.procs: Dict[str, Any] = {}
        self.restart_policy = restart_policy or RestartPolicy()
        self.power_monitor = power_monitor or PowerAndSleepMonitor(self.state_dir / "heartbeat.json")
        self.enable_caffeinate_process = enable_caffeinate_process
        self._health_probe = health_probe or default_http_health_probe
        self._spawner = process_spawner or self._default_spawn
        self._mono_clock = mono_clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._extra_secrets_provider = extra_secrets_provider

        def _all_supervisor_secrets() -> List[str]:
            items: List[str] = []
            if extra_secrets_provider:
                items.extend(extra_secrets_provider() or [])
            for sp in self.specs.values():
                for k, v in (sp.env or {}).items():
                    if any(tok in k.upper() for tok in ("SECRET", "PASSWORD", "TOKEN", "KEY")) and isinstance(v, str) and len(v.strip()) >= 6:
                        items.append(v.strip())
            return items

        self._collect_extra_secrets = _all_supervisor_secrets

        self.lock = SingleInstanceLock(
            pid_path=self.state_dir / "supervisor.pid",
            lock_path=self.state_dir / "supervisor.lock",
        )
        self.owner_shutdown_flag_path = self.state_dir / "owner_shutdown.flag"
        self.status_path = self.state_dir / "supervisor_status.json"
        self.logger = build_redacted_logger(
            f"tradelens.supervisor.{id(self)}",
            self.state_dir / "supervisor.log",
            extra_secrets_provider=_all_supervisor_secrets,
        )

        self.running: bool = False
        self.owner_shutdown_requested: bool = False
        self.stop_requested: bool = False
        self.exit_code: int = 0
        self.prior_recovery_info: Dict[str, Any] = {}

    @staticmethod
    def _default_spawn(spec: SupervisedServiceSpec) -> subprocess.Popen[Any]:
        merged_env = os.environ.copy()
        if spec.env:
            merged_env.update(spec.env)
        log_dir = Path(spec.cwd or os.getcwd()) / "runtime_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_f = open(log_dir / f"{spec.name}.stdout.log", "a", encoding="utf-8")
        try:
            return subprocess.Popen(
                spec.command,
                cwd=spec.cwd,
                env=merged_env,
                stdout=stdout_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            stdout_f.close()

    def _now_iso(self) -> str:
        dt = self._wall_clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    def is_owner_shutdown_flagged(self) -> bool:
        return self.owner_shutdown_requested or self.owner_shutdown_flag_path.exists()

    @classmethod
    def request_owner_shutdown(cls, state_dir: Path | str, reason: str = "OWNER_CLI_SHUTDOWN") -> Path:
        sdir = Path(state_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        flag_path = sdir / "owner_shutdown.flag"
        _atomic_write_json(
            flag_path,
            {
                "requested_at_utc": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
            },
        )
        pid_file = sdir / "supervisor.pid"
        if pid_file.exists():
            try:
                with open(pid_file, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                pid = int(meta.get("pid") or 0)
                if pid > 0 and pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        return flag_path

    @classmethod
    def clear_owner_shutdown_flag(cls, state_dir: Path | str) -> None:
        (Path(state_dir) / "owner_shutdown.flag").unlink(missing_ok=True)

    def startup(self) -> Dict[str, Any]:
        if self.owner_shutdown_flag_path.exists():
            self.owner_shutdown_requested = True
            self.logger.warning("Owner shutdown flag present; refusing automatic launch until cleared via resume.")
            self.write_status()
            return {"started": False, "reason": "OWNER_SHUTDOWN_FLAG_ACTIVE"}

        lock_meta = self.lock.acquire(role="supervisor")
        self.prior_recovery_info = self.power_monitor.inspect_previous_shutdown()
        if self.lock.stale_pid_recovered:
            self.logger.warning(
                "Recovered from stale supervisor PID %s.", self.lock.recovered_stale_pid
            )
        if self.prior_recovery_info.get("unexpected_shutdown_detected"):
            self.logger.warning(
                "Prior unexpected shutdown / power loss detected (last heartbeat=%s).",
                self.prior_recovery_info.get("last_heartbeat_utc"),
            )

        self.running = True
        self.stop_requested = False
        self.power_monitor.record_tick(persist=True)
        self.logger.info("TradeLens supervisor started (pid=%s).", os.getpid())

        for name in self.specs:
            self._start_service(name)

        self.power_monitor.sync_caffeinate(
            watched_pid=os.getpid(),
            enable_process=self.enable_caffeinate_process,
        )
        self.write_status()
        return {
            "started": True,
            "lock": lock_meta,
            "prior_recovery": self.prior_recovery_info,
        }

    def _start_service(self, name: str) -> None:
        spec = self.specs[name]
        state = self.states[name]
        proc = self._spawner(spec)
        self.procs[name] = proc
        state.pid = getattr(proc, "pid", None)
        state.status = "RUNNING"
        state.consecutive_health_failures = 0
        state.started_at_mono = self._mono_clock()
        state.last_started_at = self._now_iso()
        state.next_restart_mono = None
        self.logger.info("Started service '%s' (pid=%s).", name, state.pid)

    def _compute_backoff_seconds(self, attempt_index: int) -> float:
        delay = self.restart_policy.initial_backoff_seconds * (
            self.restart_policy.backoff_multiplier ** max(0, attempt_index - 1)
        )
        return min(delay, self.restart_policy.max_backoff_seconds)

    def _handle_child_failure(self, name: str, reason: str, exit_code: Optional[int] = None) -> None:
        state = self.states[name]
        now_mono = self._mono_clock()
        state.last_exit_code = exit_code
        state.last_failure_reason = reason
        state.last_stopped_at = self._now_iso()

        if self.is_owner_shutdown_flagged() or self.stop_requested:
            state.status = "STOPPED"
            self.logger.info("Service '%s' exited cleanly under shutdown (%s).", name, reason)
            return

        # Prune restarts outside sliding window
        cutoff = now_mono - self.restart_policy.window_seconds
        while state.recent_restarts and state.recent_restarts[0] < cutoff:
            state.recent_restarts.popleft()

        if len(state.recent_restarts) >= self.restart_policy.max_restarts:
            state.status = "CIRCUIT_OPEN"
            state.circuit_open = True
            state.next_restart_mono = None
            self.logger.error(
                "RESTART_CIRCUIT_BREAKER_OPEN for service '%s': %s restarts within %.0fs (reason=%s). Refusing infinite loop.",
                name,
                len(state.recent_restarts),
                self.restart_policy.window_seconds,
                reason,
            )
            return

        state.recent_restarts.append(now_mono)
        state.restart_count_total += 1
        backoff = self._compute_backoff_seconds(len(state.recent_restarts))
        state.status = "RESTARTING"
        state.next_restart_mono = now_mono + backoff
        self.logger.warning(
            "Service '%s' crashed/unhealthy (%s). Scheduling bounded restart #%s in %.1fs.",
            name,
            reason,
            len(state.recent_restarts),
            backoff,
        )

    def _terminate_child(self, name: str, timeout: float = 3.0) -> None:
        proc = self.procs.get(name)
        state = self.states[name]
        if proc is None:
            state.status = "STOPPED"
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
        state.last_exit_code = proc.poll()
        state.last_stopped_at = self._now_iso()
        state.status = "STOPPED"
        state.pid = None

    def step_once(self) -> Dict[str, Any]:
        """Execute one deterministic supervision, health, and power-monitor tick."""
        now_mono = self._mono_clock()
        if self.owner_shutdown_flag_path.exists() and not self.owner_shutdown_requested:
            self.owner_shutdown_requested = True
            self.logger.info("Detected owner shutdown sentinel flag; halting cleanly.")
            self.shutdown(owner_initiated=True, reason="OWNER_SENTINEL_SHUTDOWN")
            return self.get_status()

        self.power_monitor.record_tick(persist=True)
        self.power_monitor.sync_caffeinate(
            watched_pid=os.getpid(),
            enable_process=self.enable_caffeinate_process,
        )

        for name, spec in self.specs.items():
            state = self.states[name]
            if state.circuit_open:
                continue

            if state.status == "RESTARTING":
                if state.next_restart_mono is not None and now_mono >= state.next_restart_mono:
                    self._start_service(name)
                continue

            proc = self.procs.get(name)
            if proc is None:
                continue

            rc = proc.poll()
            if rc is not None:
                self._handle_child_failure(name, f"PROCESS_EXITED_{rc}", exit_code=rc)
                continue

            # Health probe only after initial startup grace window
            if spec.health_url and (now_mono - state.started_at_mono) >= spec.startup_grace_seconds:
                ok, detail = self._health_probe(spec.health_url)
                if ok:
                    state.consecutive_health_failures = 0
                else:
                    state.consecutive_health_failures += 1
                    if state.consecutive_health_failures >= spec.max_consecutive_health_failures:
                        self.logger.error(
                            "Service '%s' failed %s consecutive health probes (%s); recycling.",
                            name,
                            state.consecutive_health_failures,
                            detail,
                        )
                        self._terminate_child(name)
                        self._handle_child_failure(name, f"HEALTH_TIMEOUT:{detail}", exit_code=state.last_exit_code)

        # If every supervised service hit CIRCUIT_OPEN, exit cleanly with non-zero stop or stay halted safely
        self.write_status()
        return self.get_status()

    def shutdown(self, owner_initiated: bool = False, reason: str = "GRACEFUL_STOP") -> None:
        self.stop_requested = True
        self.owner_shutdown_requested = self.owner_shutdown_requested or owner_initiated
        if owner_initiated:
            _atomic_write_json(
                self.owner_shutdown_flag_path,
                {"requested_at_utc": self._now_iso(), "reason": reason},
            )
        self.logger.info("Shutting down TradeLens supervisor (owner_initiated=%s, reason=%s).", owner_initiated, reason)

        for name in list(self.specs.keys())[::-1]:
            self._terminate_child(name)

        self.power_monitor.mark_clean_shutdown(reason=reason)
        self.running = False
        self.exit_code = 0
        self.write_status()
        self.lock.release()

    def get_status(self) -> Dict[str, Any]:
        return {
            "supervisor_pid": os.getpid() if self.running else None,
            "running": self.running,
            "owner_shutdown_requested": self.is_owner_shutdown_flagged(),
            "stale_pid_recovered": self.lock.stale_pid_recovered,
            "recovered_stale_pid": self.lock.recovered_stale_pid,
            "prior_recovery": self.prior_recovery_info,
            "services": {
                name: {
                    "status": st.status,
                    "pid": st.pid,
                    "restart_count_total": st.restart_count_total,
                    "recent_restarts_in_window": len(st.recent_restarts),
                    "circuit_open": st.circuit_open,
                    "last_exit_code": st.last_exit_code,
                    "last_failure_reason": st.last_failure_reason,
                    "last_started_at": st.last_started_at,
                    "last_stopped_at": st.last_stopped_at,
                }
                for name, st in self.states.items()
            },
            "power": self.power_monitor.get_status(),
            "order_execution_enabled": False,
            "proves_live_market_data": False,
            "updated_at_utc": self._now_iso(),
        }

    def write_status(self) -> None:
        safe_status = redact_payload(
            self.get_status(),
            extra_secrets=self._collect_extra_secrets(),
            environ=os.environ,
        )
        _atomic_write_json(self.status_path, safe_status)
