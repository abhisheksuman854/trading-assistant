"""
TradeLens AI - Startup Verification & 7-Signal Trading Data Safety State Machine.

Guarantees:
1. Post-reboot startup verifies supervisor -> backend HTTP -> DB/storage -> provider config,
   never assumes Angel One authentication is valid after reboot, and never auto-places orders.
2. Tracks all 7 signals independently:
   - broker_auth_status
   - network_connectivity
   - feed_connectivity
   - last_market_data_timestamp
   - market_data_freshness
   - calculation_health
   - remote_client_connectivity
3. Connected remote office sessions NEVER serve as proof that market data is live.
4. Stale data NEVER displays as LIVE; degrades cleanly to UNAVAILABLE or DEGRADED with reasons.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.app.data.session_clock import IST_ZONE


class EngineState:
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class BrokerAuthState:
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    EXPLICIT_AUTH_REQUIRED = "EXPLICIT_AUTH_REQUIRED"
    AUTHENTICATED = "AUTHENTICATED"
    EXPIRED = "EXPIRED"
    AUTH_FAILED = "AUTH_FAILED"


class NetworkState:
    ONLINE = "ONLINE"
    DISCONNECTED = "DISCONNECTED"
    UNKNOWN = "UNKNOWN"


class FeedState:
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    API_OUTAGE = "API_OUTAGE"
    RATE_LIMITED = "RATE_LIMITED"
    UNINITIALIZED = "UNINITIALIZED"


class FreshnessState:
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class CalculationState:
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass
class StartupCheckStep:
    step_number: int
    name: str
    passed: bool
    status: str
    detail: str


class RuntimeHealthCoordinator:
    """
    Authoritative truth coordinator for Home Mac live readiness and remote availability.
    """

    def __init__(
        self,
        freshness_ttl_seconds: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.freshness_ttl_seconds = max(1.0, float(freshness_ttl_seconds))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self.order_execution_enabled: bool = False  # Strictly disabled across all phases
        self.startup_verified: bool = False
        self.startup_steps: List[StartupCheckStep] = []

        # Signal 1: Broker authentication status
        self.broker_auth_status: str = BrokerAuthState.EXPLICIT_AUTH_REQUIRED
        self.broker_auth_detail: str = "Explicit broker authentication required after startup."

        # Signal 2: Network / internet connectivity
        self.network_connectivity: str = NetworkState.UNKNOWN
        self.network_detail: str = "Internet reachability not yet verified."

        # Signal 3: Feed connectivity
        self.feed_connectivity: str = FeedState.UNINITIALIZED
        self.feed_detail: str = "Market data feed has not streamed yet."

        # Signal 4 & 5: Last received market-data timestamp & freshness
        self.last_market_data_timestamp: Optional[str] = None
        self._last_market_data_dt: Optional[datetime] = None
        self._sleep_invalidated_feed: bool = False

        # Signal 6: Calculation & local DB/storage health
        self.calculation_health: str = CalculationState.HEALTHY
        self.calculation_detail: str = "Engines unverified until startup check completes."
        self.database_accessible: bool = False
        self.historical_store_accessible: bool = False
        self.provider_config_ready: bool = False

        # Signal 7: Remote client & VPN connectivity (strictly informational; never implies LIVE market data)
        self.vpn_connected: bool = False
        self.vpn_detail: str = "Private VPN interface not verified."
        self.remote_access_manager: Optional[Any] = None

        # Supervised service states
        self.backend_healthy: bool = True
        self.frontend_healthy: bool = True
        self.frontend_detail: str = "Frontend dashboard status nominal."
        self.supervisor_healthy: bool = True
        self.owner_shutdown_requested: bool = False
        self.recovery_notes: List[str] = []

    def _now(self) -> datetime:
        dt = self._clock()
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    @staticmethod
    def check_provider_config_readiness(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Inspect credential presence safely without returning secret values."""
        env = environ if environ is not None else os.environ
        required = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET")
        placeholders = {"", "your_api_key_here", "your_client_code", "your_password", "your_totp_secret", "changeme"}
        status_map: Dict[str, str] = {}
        all_ready = True
        for key in required:
            val = (env.get(key) or "").strip()
            if not val:
                status_map[key] = "MISSING"
                all_ready = False
            elif val.lower() in placeholders or val.lower().startswith("your_"):
                status_map[key] = "PLACEHOLDER"
                all_ready = False
            else:
                status_map[key] = "PRESENT_VALID_FORMAT"
        return {"all_ready": all_ready, "fields": status_map}

    def verify_startup_dependencies(
        self,
        ledger_path: Path | str,
        historical_dir: Path | str,
        environ: Optional[Dict[str, str]] = None,
        supervisor_ok: bool = True,
        backend_http_ok: bool = True,
        internet_ok: bool = False,
        prior_unclean_shutdown: bool = False,
    ) -> List[StartupCheckStep]:
        """
        Execute deterministic post-reboot verification steps 1..9 cleanly.
        Never assumes Angel One auth is valid on boot; never places orders.
        """
        steps: List[StartupCheckStep] = []
        self.supervisor_healthy = supervisor_ok
        self.backend_healthy = backend_http_ok

        # 1. Local service supervisor
        steps.append(
            StartupCheckStep(
                1,
                "supervisor_running",
                supervisor_ok,
                "PASS" if supervisor_ok else "FAIL",
                "Local service supervisor lock active." if supervisor_ok else "Supervisor unavailable.",
            )
        )

        # 2-3. Backend HTTP started & health verified
        steps.append(
            StartupCheckStep(
                2,
                "backend_health",
                backend_http_ok,
                "PASS" if backend_http_ok else "FAIL",
                "Backend HTTP health endpoint verified." if backend_http_ok else "Backend health check failed.",
            )
        )

        # 4. Database / ledger & historical storage access verification
        db_ok = False
        db_detail = ""
        lpath = Path(ledger_path)
        try:
            if lpath.exists():
                with open(lpath, "r", encoding="utf-8") as fh:
                    parsed = json.load(fh)
                db_ok = isinstance(parsed, dict)
                db_detail = "Ledger store JSON readable and valid."
            else:
                db_ok = lpath.parent.exists()
                db_detail = "Ledger store file absent; parent directory writable."
        except Exception as exc:
            db_ok = False
            db_detail = f"Ledger database corruption/unreadable: {exc}"

        hist_dir = Path(historical_dir)
        hist_ok = hist_dir.exists() and hist_dir.is_dir()
        self.database_accessible = db_ok
        self.historical_store_accessible = hist_ok
        if db_ok and hist_ok:
            self.calculation_health = CalculationState.HEALTHY
            self.calculation_detail = "Deterministic engines & storage verified."
        elif db_ok or hist_ok:
            self.calculation_health = CalculationState.DEGRADED
            self.calculation_detail = f"Storage partially accessible ({db_detail}; historical_dir={hist_ok})."
        else:
            self.calculation_health = CalculationState.FAILED
            self.calculation_detail = f"Database and historical storage inaccessible ({db_detail})."

        steps.append(
            StartupCheckStep(
                3,
                "database_and_storage_access",
                db_ok and hist_ok,
                "PASS" if (db_ok and hist_ok) else "DEGRADED" if db_ok else "FAIL",
                f"{db_detail} Historical store present={hist_ok}.",
            )
        )

        # 5. Market-data provider configuration check
        cfg = self.check_provider_config_readiness(environ=environ)
        self.provider_config_ready = bool(cfg["all_ready"])
        steps.append(
            StartupCheckStep(
                4,
                "provider_config_verification",
                self.provider_config_ready,
                "PASS" if self.provider_config_ready else "DEGRADED",
                "Angel One credential environment formatted cleanly on Home Mac."
                if self.provider_config_ready
                else "Angel One credentials missing/placeholder on Home Mac.",
            )
        )

        # 6. Order execution safeguard
        self.order_execution_enabled = False
        steps.append(
            StartupCheckStep(
                5,
                "no_auto_order_execution",
                True,
                "PASS",
                "Order execution strictly disabled (read-only research & assistance mode).",
            )
        )

        # 7-8. Do NOT assume Angel One authentication survived reboot!
        self.broker_auth_status = BrokerAuthState.EXPLICIT_AUTH_REQUIRED
        self.broker_auth_detail = "Post-reboot explicit Angel One authentication or approved session required."
        self.feed_connectivity = FeedState.UNINITIALIZED
        self.last_market_data_timestamp = None
        self._last_market_data_dt = None
        self.network_connectivity = NetworkState.ONLINE if internet_ok else NetworkState.UNKNOWN
        self.network_detail = "Internet connectivity reachable." if internet_ok else "Network probe pending."
        steps.append(
            StartupCheckStep(
                6,
                "explicit_broker_auth_required",
                False,  # Pending explicit auth; must not falsely pass as LIVE on boot!
                "PENDING_EXPLICIT_AUTH",
                "Startup refuses to assume Angel One JWT validity across reboot; explicit owner/session auth required.",
            )
        )

        # 9. Freshness gate initially UNAVAILABLE until genuine quote received
        steps.append(
            StartupCheckStep(
                7,
                "data_freshness_gate",
                False,
                "UNAVAILABLE",
                "No verified live market tick received yet after startup.",
            )
        )

        if prior_unclean_shutdown:
            self.recovery_notes.append(
                "Detected prior unexpected shutdown or power loss; broker session and cached quotes invalidated."
            )

        self.startup_verified = True
        self.startup_steps = steps
        return steps

    def mark_internet_status(self, is_online: bool, detail: str = "") -> None:
        if is_online:
            was_down = self.network_connectivity == NetworkState.DISCONNECTED
            self.network_connectivity = NetworkState.ONLINE
            self.network_detail = detail or "Internet connectivity restored."
            if was_down:
                self.recovery_notes.append("Internet reconnected; awaiting fresh broker tick verification.")
                if self.feed_connectivity == FeedState.DISCONNECTED:
                    self.feed_connectivity = FeedState.UNINITIALIZED
                    self.feed_detail = "Re-verifying live feed after internet reconnection."
        else:
            self.network_connectivity = NetworkState.DISCONNECTED
            self.network_detail = detail or "Internet connectivity lost on Home Mac."
            self.feed_connectivity = FeedState.DISCONNECTED
            self.feed_detail = "Feed unreachable while internet is disconnected."

    def mark_broker_auth_result(
        self,
        success: bool,
        expired: bool = False,
        reason: str = "",
    ) -> None:
        if expired:
            self.broker_auth_status = BrokerAuthState.EXPIRED
            self.broker_auth_detail = reason or "Angel One authentication session expired."
            self.feed_connectivity = FeedState.DISCONNECTED
            self.feed_detail = "Market feed stopped: broker auth expired."
        elif success:
            self.broker_auth_status = BrokerAuthState.AUTHENTICATED
            self.broker_auth_detail = reason or "Angel One SmartAPI session authenticated."
            self._sleep_invalidated_feed = False
        else:
            self.broker_auth_status = BrokerAuthState.AUTH_FAILED
            self.broker_auth_detail = reason or "Angel One authentication failed."
            self.feed_connectivity = FeedState.DISCONNECTED

    def mark_broker_api_outage(self, rate_limited: bool = False, reason: str = "") -> None:
        self.feed_connectivity = FeedState.RATE_LIMITED if rate_limited else FeedState.API_OUTAGE
        self.feed_detail = reason or (
            "Angel One API rate-limit or upstream error." if rate_limited else "Angel One broker API unreachable or returning errors."
        )

    def mark_sleep_interruption(self, sleep_duration_sec: float, detected_at_iso: str) -> None:
        self._sleep_invalidated_feed = True
        self.feed_connectivity = FeedState.DISCONNECTED
        self.feed_detail = (
            f"Home Mac woke from unexpected sleep ({sleep_duration_sec:.1f}s gap at {detected_at_iso}); "
            "cached quotes invalidated pending fresh broker poll."
        )
        self.recovery_notes.append(self.feed_detail)

    def mark_frontend_status(self, healthy: bool, detail: str = "") -> None:
        self.frontend_healthy = healthy
        self.frontend_detail = detail or ("Frontend dashboard server healthy." if healthy else "Frontend server crashed or unavailable.")

    def mark_vpn_status(self, connected: bool, detail: str = "") -> None:
        self.vpn_connected = connected
        self.vpn_detail = detail or ("Private VPN mesh active." if connected else "Private VPN interface disconnected.")

    def mark_calculation_health(self, healthy: bool, detail: str = "") -> None:
        if healthy:
            self.calculation_health = CalculationState.HEALTHY
            self.calculation_detail = detail or "Analytics & database healthy."
        else:
            self.calculation_health = CalculationState.FAILED
            self.calculation_detail = detail or "Calculation engine or database failure."

    def record_market_data_tick(
        self,
        tick_timestamp: Optional[datetime] = None,
        now_utc: Optional[datetime] = None,
    ) -> None:
        if self.broker_auth_status != BrokerAuthState.AUTHENTICATED:
            return
        if self.network_connectivity != NetworkState.ONLINE:
            return
        dt = tick_timestamp or now_utc or self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        self._last_market_data_dt = dt
        self.last_market_data_timestamp = dt.astimezone(IST_ZONE).isoformat()
        self.feed_connectivity = FeedState.CONNECTED
        self.feed_detail = "Streaming verified quotes from Angel One."
        self._sleep_invalidated_feed = False

    def compute_freshness(self, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        if self._last_market_data_dt is None:
            return {
                "market_data_freshness": FreshnessState.UNAVAILABLE,
                "age_seconds": None,
                "ttl_seconds": self.freshness_ttl_seconds,
            }
        now = now_utc or self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - self._last_market_data_dt).total_seconds())
        if self._sleep_invalidated_feed or age > self.freshness_ttl_seconds:
            return {
                "market_data_freshness": FreshnessState.STALE,
                "age_seconds": round(age, 2),
                "ttl_seconds": self.freshness_ttl_seconds,
            }
        return {
            "market_data_freshness": FreshnessState.FRESH,
            "age_seconds": round(age, 2),
            "ttl_seconds": self.freshness_ttl_seconds,
        }

    def evaluate(
        self,
        now_utc: Optional[datetime] = None,
        power_status: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = now_utc or self._now()
        freshness = self.compute_freshness(now_utc=now)
        freshness_state = freshness["market_data_freshness"]

        remote_summary = (
            self.remote_access_manager.summarize_connectivity()
            if self.remote_access_manager is not None
            else {"active_remote_sessions": 0, "proves_live_market_data": False}
        )
        remote_summary["vpn_connected"] = self.vpn_connected
        remote_summary["vpn_detail"] = self.vpn_detail
        remote_summary["proves_live_market_data"] = False  # Enforced invariant

        failure_reasons: List[str] = []
        critical_fail = False
        degraded = False

        if self.owner_shutdown_requested:
            critical_fail = True
            failure_reasons.append("OWNER_SHUTDOWN: Home Mac trading engine stopped intentionally by owner.")
        if not self.supervisor_healthy or not self.backend_healthy:
            critical_fail = True
            failure_reasons.append("BACKEND_UNHEALTHY: Core backend service or supervisor is down.")
        if not self.frontend_healthy:
            degraded = True
            failure_reasons.append(f"FRONTEND_CRASHED: {self.frontend_detail}")
        if not self.database_accessible or self.calculation_health == CalculationState.FAILED:
            critical_fail = True
            failure_reasons.append(f"CALCULATION_OR_DB_FAILURE: {self.calculation_detail}")
        elif self.calculation_health == CalculationState.DEGRADED:
            degraded = True
            failure_reasons.append(f"STORAGE_DEGRADED: {self.calculation_detail}")
        if self.network_connectivity == NetworkState.DISCONNECTED:
            critical_fail = True
            failure_reasons.append(f"INTERNET_DISCONNECTED: {self.network_detail}")
        if self.broker_auth_status in {
            BrokerAuthState.EXPLICIT_AUTH_REQUIRED,
            BrokerAuthState.NOT_ATTEMPTED,
            BrokerAuthState.EXPIRED,
            BrokerAuthState.AUTH_FAILED,
        }:
            critical_fail = True
            failure_reasons.append(f"BROKER_AUTH_{self.broker_auth_status}: {self.broker_auth_detail}")
        if self.feed_connectivity in {FeedState.API_OUTAGE, FeedState.RATE_LIMITED, FeedState.DISCONNECTED}:
            degraded = True
            failure_reasons.append(f"FEED_{self.feed_connectivity}: {self.feed_detail}")
        if freshness_state == FreshnessState.STALE:
            degraded = True
            failure_reasons.append(
                f"STALE_MARKET_DATA: Last tick is {freshness['age_seconds']}s old (TTL {freshness['ttl_seconds']}s); refused LIVE status."
            )
        elif freshness_state == FreshnessState.UNAVAILABLE and self.broker_auth_status == BrokerAuthState.AUTHENTICATED:
            degraded = True
            failure_reasons.append("NO_MARKET_TICKS_YET: Authenticated with broker but no live tick timestamp received yet.")

        if power_status:
            if power_status.get("unexpected_sleep_detected") and self._sleep_invalidated_feed:
                degraded = True
                failure_reasons.append("HOME_SLEEP_DETECTED: Unexpected Home Mac sleep invalidated cached feed.")
            if power_status.get("unexpected_shutdown_detected") and self.broker_auth_status != BrokerAuthState.AUTHENTICATED:
                failure_reasons.append("RECOVERED_FROM_UNEXPECTED_SHUTDOWN: Explicit verification required.")
            if power_status.get("power_source") == "BATTERY_POWER":
                degraded = True
                failure_reasons.append("BATTERY_POWER_WARNING: Home Mac unplugged; sleep/power loss risk.")

        if not self.vpn_connected:
            failure_reasons.append(f"VPN_DISCONNECTED: {self.vpn_detail}")

        # Engine state classification
        all_live_ok = (
            not critical_fail
            and not degraded
            and self.broker_auth_status == BrokerAuthState.AUTHENTICATED
            and self.network_connectivity == NetworkState.ONLINE
            and self.feed_connectivity == FeedState.CONNECTED
            and freshness_state == FreshnessState.FRESH
            and self.calculation_health == CalculationState.HEALTHY
        )

        if all_live_ok:
            engine_status = EngineState.LIVE
            data_status = "LIVE"
        elif critical_fail and self.feed_connectivity == FeedState.UNINITIALIZED and freshness_state == FreshnessState.UNAVAILABLE:
            engine_status = EngineState.UNAVAILABLE
            data_status = "UNAVAILABLE"
        elif critical_fail:
            engine_status = EngineState.UNAVAILABLE
            data_status = "STALE" if freshness_state == FreshnessState.STALE else "UNAVAILABLE"
        else:
            engine_status = EngineState.DEGRADED
            data_status = "STALE" if freshness_state == FreshnessState.STALE else "UNAVAILABLE"

        remote_available = (
            self.backend_healthy
            and not self.owner_shutdown_requested
            and self.vpn_connected
        )

        return {
            "engine_status": engine_status,
            "data_status": data_status,
            "remote_available": remote_available,
            "order_execution_enabled": False,
            "evaluated_at_utc": now.isoformat(),
            "signals": {
                "broker_auth_status": self.broker_auth_status,
                "broker_auth_detail": self.broker_auth_detail,
                "network_connectivity": self.network_connectivity,
                "network_detail": self.network_detail,
                "feed_connectivity": self.feed_connectivity,
                "feed_detail": self.feed_detail,
                "last_market_data_timestamp": self.last_market_data_timestamp,
                "market_data_freshness": freshness_state,
                "market_data_age_seconds": freshness["age_seconds"],
                "freshness_ttl_seconds": freshness["ttl_seconds"],
                "calculation_health": self.calculation_health,
                "calculation_detail": self.calculation_detail,
                "remote_client_connectivity": remote_summary,
            },
            "services": {
                "supervisor_healthy": self.supervisor_healthy,
                "backend_healthy": self.backend_healthy,
                "frontend_healthy": self.frontend_healthy,
                "frontend_detail": self.frontend_detail,
            },
            "startup_steps": [asdict(s) for s in self.startup_steps],
            "failure_reasons": failure_reasons,
            "recovery_notes": list(self.recovery_notes[-10:]),
            "power": power_status,
        }
