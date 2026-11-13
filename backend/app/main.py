"""
TradeLens AI - Hardened Home Mac Backend REST API Service
Serves private-network-gated endpoints for live shortlisting, deep-dive stock analysis,
position monitoring, explicit broker session unlock, and 7-signal runtime safety telemetry.
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import stat
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]


def read_env_dict(
    base_env: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
    env_path: Optional[Path | str] = None,
    require_strict_perms: bool = False,
) -> Dict[str, str]:
    """
    Read runtime environment variables strictly from the repository root `.env`
    (or an explicit `env_path` / `TRADELENS_ENV_FILE`). Never walks parent directories
    above `REPO_ROOT`.
    """
    merged = dict(base_env if base_env is not None else os.environ)
    if env_path is not None:
        target_env: Optional[Path] = Path(env_path)
    elif "TRADELENS_ENV_FILE" in merged:
        raw_env_file = str(merged.get("TRADELENS_ENV_FILE", "")).strip()
        target_env = Path(raw_env_file) if raw_env_file else None
    else:
        target_env = REPO_ROOT / ".env"

    if target_env is not None and (target_env.exists() or target_env.is_symlink()):
        if require_strict_perms:
            if target_env.is_symlink():
                raise PermissionError(
                    f"Refusing to load symlinked '{target_env}' in hardened remote mode."
                )
            mode = stat.S_IMODE(target_env.stat().st_mode)
            if (mode & 0o077) != 0:
                raise PermissionError(
                    f"Refusing to load '{target_env}' with permissive mode {oct(mode)}; "
                    "restrict permissions with chmod 600 .env first."
                )
            for key, val in parse_dotenv_content(
                target_env.read_text(encoding="utf-8"),
                strict_syntax=True,
            ).items():
                if overwrite or key not in merged:
                    merged[key] = val
    return merged


def load_env(
    overwrite: bool = False,
    env_path: Optional[Path | str] = None,
    require_strict_perms: bool = False,
) -> None:
    for key, val in read_env_dict(
        overwrite=overwrite,
        env_path=env_path,
        require_strict_perms=require_strict_perms,
    ).items():
        if overwrite or key not in os.environ:
            os.environ[key] = val


# Ensure core packages can be imported regardless of working directory invocation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.ledger import ledger_manager
from app.core.options_analytics import get_strike_step
from app.core.shortlist_engine import (
    evaluate_single_stock_full_profile,
    monitor_open_positions,
    scan_and_shortlist_universe,
)
from app.core.tip_verifier import verify_tip_setup
from app.data.angel_one import AngelOneSmartAPIProvider
from app.data.sample_universe import SampleUniverseProvider
from backend.app.runtime.health_and_safety import (
    BrokerAuthState,
    FeedState,
    NetworkState,
    RuntimeHealthCoordinator,
)
from backend.app.runtime.power_policy import PowerAndSleepMonitor
from backend.app.runtime.security import (
    RedactingLogFilter,
    RemoteAccessManager,
    _extract_hostname_without_port,
    _parse_ip_clean,
    build_security_headers,
    collect_known_secrets,
    is_allowed_remote_client_ip,
    normalize_allowed_peer_ips,
    parse_dotenv_content,
    redact_payload,
    redact_runtime_text,
    render_hardened_html,
    validate_bind_host,
    wrap_server_tls,
)
from backend.app.runtime.stage_b_setup import StageBSetupCoordinator
from backend.app.runtime.supervisor import TradeLensSupervisor


class ThreadingTradeLensServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class TradeLensAppRuntime:
    """Encapsulates broker provider, 7-signal safety state, power monitor, and remote auth."""

    def __init__(
        self,
        state_dir: Optional[Path | str] = None,
        provider: Optional[Any] = None,
        fallback_provider: Optional[Any] = None,
        remote_access: Optional[RemoteAccessManager] = None,
        health_coordinator: Optional[RuntimeHealthCoordinator] = None,
        power_monitor: Optional[PowerAndSleepMonitor] = None,
        ledger: Optional[Any] = None,
        ledger_path: Optional[Path | str] = None,
        historical_dir: Optional[Path | str] = None,
        allowed_vpn_cidrs: Optional[List[str]] = None,
        allow_sample_fallback: Optional[bool] = None,
        freshness_ttl_seconds: float = 15.0,
        environ: Optional[Dict[str, str]] = None,
        allowed_peer_ips: Optional[List[str]] = None,
        stage_b_progress_path: Optional[Path | str] = None,
    ) -> None:
        self.environ = dict(environ) if environ is not None else read_env_dict()
        self.state_dir = Path(
            state_dir
            or self.environ.get("TRADELENS_RUNTIME_STATE_DIR")
            or (REPO_ROOT / "runtime_state")
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.provider = provider or AngelOneSmartAPIProvider(
            api_key=self.environ.get("ANGEL_API_KEY"),
            client_code=self.environ.get("ANGEL_CLIENT_CODE"),
            password=self.environ.get("ANGEL_PASSWORD"),
            totp_secret=self.environ.get("ANGEL_TOTP_SECRET"),
        )
        self.fallback_provider = fallback_provider or SampleUniverseProvider()
        self.ledger = ledger or ledger_manager
        self.ledger_path = Path(ledger_path or (REPO_ROOT / "backend" / "app" / "data" / "ledger_store.json"))
        self.historical_dir = Path(historical_dir or (REPO_ROOT / "data" / "historical"))

        env_cidrs = [
            c.strip()
            for c in self.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").split(",")
            if c.strip()
        ]
        env_peers = [
            p.strip()
            for p in self.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").split(",")
            if p.strip()
        ]
        env_hosts = [
            h.strip()
            for h in self.environ.get("TRADELENS_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]
        env_origins = [
            o.strip()
            for o in self.environ.get("TRADELENS_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        ]
        effective_cidrs = allowed_vpn_cidrs if allowed_vpn_cidrs is not None else env_cidrs
        effective_peers = normalize_allowed_peer_ips(
            allowed_peer_ips if allowed_peer_ips is not None else env_peers
        )
        bind_host_env = self.environ.get("TRADELENS_BIND_HOST", "").strip()
        remote_mode_env = self.environ.get("TRADELENS_REMOTE_ACCESS_ENABLED", "0").strip() == "1"
        remote_mode = remote_mode_env or bool(effective_cidrs) or bool(effective_peers) or (
            bool(bind_host_env) and bind_host_env.lower() not in {"127.0.0.1", "::1", "localhost"}
        )
        self.remote_access = remote_access or RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=self.environ.get("TRADELENS_OWNER_SECRET"),
            allowed_vpn_cidrs=effective_cidrs,
            allowed_peer_ips=effective_peers,
            allowed_hosts=env_hosts,
            allowed_origins=env_origins,
            require_auth_on_loopback=self.environ.get("TRADELENS_REQUIRE_LOCAL_AUTH", "0") == "1",
            state_hmac_secret=self.environ.get("TRADELENS_STATE_HMAC_SECRET"),
            remote_mode_enabled=remote_mode,
        )

        self.health = health_coordinator or RuntimeHealthCoordinator(
            freshness_ttl_seconds=freshness_ttl_seconds
        )
        self.health.order_execution_enabled = False
        self.health.remote_access_manager = self.remote_access

        self.power_monitor = power_monitor or PowerAndSleepMonitor(
            heartbeat_path=self.state_dir / "heartbeat.json",
            on_unexpected_sleep=self._handle_unexpected_sleep,
        )

        if allow_sample_fallback is None:
            self.allow_sample_fallback = self.environ.get("TRADELENS_ALLOW_SIMULATED_FALLBACK", "0") == "1"
        else:
            self.allow_sample_fallback = allow_sample_fallback

        self._lock = threading.RLock()
        self.stage_b_setup = StageBSetupCoordinator(
            repo_root=REPO_ROOT,
            state_dir=self.state_dir,
            progress_path=stage_b_progress_path,
            remote_access=self.remote_access,
            environ=self.environ,
        )
        self.httpd: Optional[ThreadingTradeLensServer] = None
        self.logger = self._setup_logger()
        if self.remote_access.remote_mode_enabled and not self.remote_access.allowed_peer_ips:
            self.logger.warning(
                "SECURITY_ADVISORY: remote mode enabled without TRADELENS_ALLOWED_PEER_IPS; "
                "pin the exact approved Office Mac Tailscale IP before live provisioning."
            )
        self._startup_init()

    @property
    def order_execution_enabled(self) -> bool:
        """Order execution is permanently disabled in TradeLens AI."""
        return False

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"tradelens.api.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(
            RedactingLogFilter(
                extra_secrets_provider=lambda: collect_known_secrets(
                    environ=self.environ,
                    extra_secrets=self.remote_access.active_ephemeral_secrets(),
                )
            )
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        return logger

    def _handle_unexpected_sleep(self, drift_seconds: float, detected_at_iso: str) -> None:
        self.health.mark_sleep_interruption(drift_seconds, detected_at_iso)
        # Invalidate cached quotes after unexpected Home Mac sleep
        if hasattr(self.provider, "cached_quotes"):
            self.provider.cached_quotes = {}
        self.logger.warning(
            "Home Mac sleep interruption detected (%.1fs); broker feed invalidated until refreshed.",
            drift_seconds,
        )

    def _startup_init(self) -> None:
        prior = self.power_monitor.inspect_previous_shutdown()
        self.power_monitor.record_tick(persist=True)
        # Never assume Angel One JWT survived reboot; wait for explicit auth/session check.
        self.health.verify_startup_dependencies(
            ledger_path=self.ledger_path,
            historical_dir=self.historical_dir,
            environ=self.environ,
            supervisor_ok=True,
            backend_http_ok=True,
            internet_ok=True if self.environ.get("TRADELENS_ASSUME_ONLINE_TEST") == "1" else False,
            prior_unclean_shutdown=bool(prior.get("unexpected_shutdown_detected", False)),
        )

    def check_clock_and_power(self) -> Dict[str, Any]:
        with self._lock:
            self.power_monitor.record_tick(persist=True)
            return self.power_monitor.get_status()

    def authenticate_broker(self, verify_fresh_tick: bool = True) -> Dict[str, Any]:
        with self._lock:
            self.health.mark_internet_status(True, "Broker authentication initiated on Home Mac.")
            try:
                ok = bool(self.provider.connect())
            except Exception as exc:
                self.health.mark_broker_auth_result(
                    False, reason=f"Broker connect exception: {redact_runtime_text(str(exc), environ=self.environ)}"
                )
                return self.health.evaluate(power_status=self.power_monitor.get_status())

            if not ok or not getattr(self.provider, "is_authenticated", False):
                self.health.mark_broker_auth_result(
                    False,
                    reason="Angel One SmartAPI authentication failed or credentials unconfigured.",
                )
                return self.health.evaluate(power_status=self.power_monitor.get_status())

            self.health.mark_broker_auth_result(True, reason="Angel One session authenticated explicitly by owner.")
            if verify_fresh_tick:
                self._refresh_live_quotes_locked()
            return self.health.evaluate(power_status=self.power_monitor.get_status())

    @staticmethod
    def _call_provider_no_reconnect(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            sig = inspect.signature(fn)
            if "_allow_reconnect" in sig.parameters:
                kwargs = {**kwargs, "_allow_reconnect": False}
        except (TypeError, ValueError):
            pass
        return fn(*args, **kwargs)

    def _refresh_live_quotes_locked(self) -> bool:
        if not getattr(self.provider, "is_authenticated", False):
            return False
        prev_fetch_time = getattr(self.provider, "last_fetch_time", None)
        call_started = time.time()
        try:
            quotes = self._call_provider_no_reconnect(self.provider.fetch_live_quotes)
            # Detect if provider internally downgraded due to 401/403 expiry
            if not getattr(self.provider, "is_authenticated", False):
                self.health.mark_broker_auth_result(
                    False, expired=True, reason="Angel One HTTP 401/403 token expired."
                )
                return False
            after_fetch_time = getattr(self.provider, "last_fetch_time", None)
            # AngelOneSmartAPIProvider catches network/HTTP errors and returns stale cached_quotes without advancing last_fetch_time
            if (
                prev_fetch_time is not None
                and prev_fetch_time > 0
                and (call_started - prev_fetch_time) >= 2.5
                and after_fetch_time == prev_fetch_time
            ):
                self.health.mark_broker_api_outage(
                    reason="Broker quote poll returned unchanged cache without advancing fetch timestamp (outage/network error)."
                )
                return False
            if quotes:
                self.health.record_market_data_tick()
                return True
            self.health.mark_broker_api_outage(reason="Angel One returned empty quote payload.")
            return False
        except urllib.error.HTTPError as he:
            if he.code in (401, 403):
                self.health.mark_broker_auth_result(False, expired=True, reason=f"Angel One HTTP {he.code} expired.")
            elif he.code == 429:
                self.health.mark_broker_api_outage(rate_limited=True, reason="Angel One rate limit HTTP 429.")
            else:
                self.health.mark_broker_api_outage(reason=f"Angel One HTTP {he.code} error.")
            return False
        except Exception as exc:
            self.health.mark_broker_api_outage(
                reason=f"Broker API quote fetch failed: {redact_runtime_text(str(exc), environ=self.environ)}"
            )
            return False

    def _active_stock_universe(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            power_status = self.check_clock_and_power()
            if (
                getattr(self.provider, "is_authenticated", False)
                and self.health.broker_auth_status == BrokerAuthState.AUTHENTICATED
                and self.health.network_connectivity == NetworkState.ONLINE
            ):
                # Refresh only if stale or sleep invalidated
                fresh = self.health.compute_freshness()
                if fresh["market_data_freshness"] == "FRESH" and self.health.feed_connectivity == FeedState.CONNECTED:
                    stocks = self._call_provider_no_reconnect(self.provider.get_all_stocks)
                    snap = self.health.evaluate(power_status=power_status)
                    return stocks, snap
                # Attempt live refresh without triggering implicit broker login/reconnect
                self._refresh_live_quotes_locked()
                snap = self.health.evaluate(power_status=power_status)
                if snap["data_status"] == "LIVE":
                    return self._call_provider_no_reconnect(self.provider.get_all_stocks), snap
                return [], snap

            snap = self.health.evaluate(power_status=power_status)
            if self.allow_sample_fallback:
                stocks = self.fallback_provider.get_all_stocks()
                snap["data_status"] = "SIMULATED"
                return stocks, snap
            return [], snap

    def get_health_payload(self) -> Dict[str, Any]:
        with self._lock:
            power_status = self.check_clock_and_power()
            eval_snap = self.health.evaluate(power_status=power_status)
            eval_snap.update(
                {
                    "status": "ok" if eval_snap["engine_status"] == "LIVE" else eval_snap["engine_status"].lower(),
                    "service": "TradeLens AI Home Market Engine",
                    "data_source": (
                        "LIVE Angel One SmartAPI"
                        if eval_snap["data_status"] == "LIVE"
                        else "SIMULATED Benchmark Universe"
                        if eval_snap["data_status"] == "SIMULATED"
                        else "UNAVAILABLE (Fail-Closed Safety Mode)"
                    ),
                    "order_execution_enabled": False,
                    "proves_live_market_data": False,
                    "server_time": datetime.now(timezone.utc).isoformat(),
                    "version": "1.1.0",
                }
            )
            return redact_payload(
                eval_snap,
                extra_secrets=self.remote_access.active_ephemeral_secrets(),
                environ=self.environ,
            )

    def get_scan_payload(self, sentiment: str = "BULLISH", top_n: int = 5) -> Dict[str, Any]:
        stocks, snap = self._active_stock_universe()
        if stocks:
            result = scan_and_shortlist_universe(stocks, top_n=top_n, market_sentiment=sentiment)
            active_prov = self.provider if snap["data_status"] == "LIVE" else self.fallback_provider
            result["indices"] = self._call_provider_no_reconnect(active_prov.get_indices)
            pos_fn = getattr(active_prov, "get_live_positions", None)
            result["live_portfolio"] = self._call_provider_no_reconnect(pos_fn) if pos_fn else {}
            all_watch = []
            for s in stocks:
                c = s["candles"][-1]["close"] if s.get("candles") else s.get("cmp", 0.0)
                chg = s.get("live_change_pct", 0.0)
                step = get_strike_step(c, symbol=s["symbol"])
                atm_s = round(c / step) * step
                atm_label = f"{int(atm_s) if atm_s.is_integer() else atm_s} CE"
                all_watch.append(
                    {
                        "symbol": s["symbol"],
                        "name": s.get("name", s["symbol"]),
                        "category": s.get("category", "F&O"),
                        "cmp": c,
                        "change_pct": chg,
                        "score": s.get("composite_score", 76.5),
                        "swing": "Active Setup",
                        "opt": atm_label,
                        "data_status": snap["data_status"],
                    }
                )
            result["all_watchlist_stocks"] = all_watch
        else:
            result = {
                "market_sentiment": "UNAVAILABLE",
                "top_movers": [],
                "top_swing_setups": [],
                "top_options_setups": [],
                "avoid_or_vetoed": [],
                "indices": [],
                "live_portfolio": {},
                "all_watchlist_stocks": [],
            }

        result["data_source"] = (
            "LIVE Angel One SmartAPI"
            if snap["data_status"] == "LIVE"
            else "SIMULATED Benchmark Universe"
            if snap["data_status"] == "SIMULATED"
            else "UNAVAILABLE"
        )
        result["data_status"] = snap["data_status"]
        result["engine_status"] = snap["engine_status"]
        result["failure_reasons"] = snap["failure_reasons"]
        result["signals"] = snap["signals"]
        result["remote_available"] = snap["remote_available"]
        result["server_time"] = datetime.now(timezone.utc).isoformat()
        return redact_payload(
            result,
            extra_secrets=self.remote_access.active_ephemeral_secrets(),
            environ=self.environ,
        )

    def request_owner_shutdown(self, reason: str = "OWNER_API_SHUTDOWN") -> Dict[str, Any]:
        with self._lock:
            self.health.owner_shutdown_requested = True
            self.power_monitor.mark_clean_shutdown(reason=reason)
            TradeLensSupervisor.request_owner_shutdown(self.state_dir, reason=reason)
            httpd = self.httpd
        if httpd is not None:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        return {
            "status": "SHUTTING_DOWN",
            "owner_shutdown": True,
            "reason": reason,
        }


def make_api_handler(runtime: TradeLensAppRuntime) -> type[BaseHTTPRequestHandler]:
    class TradeLensAPIHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            runtime.logger.info(
                "%s - %s",
                self.client_address[0],
                format % args if args else format,
            )

        def _send_headers_list(self, hdrs: List[Tuple[str, str]]) -> None:
            for k, v in hdrs:
                self.send_header(k, v)

        def _send_json(
            self,
            status: int,
            payload: Dict[str, Any],
            allowed_origin: Optional[str] = None,
        ) -> None:
            safe_payload = redact_payload(
                payload,
                extra_secrets=runtime.remote_access.active_ephemeral_secrets(),
                environ=runtime.environ,
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self._send_headers_list(
                build_security_headers(allowed_origin=allowed_origin, is_html=False)
            )
            self.end_headers()
            self.wfile.write(json.dumps(safe_payload, indent=2).encode("utf-8"))

        def _authorize(self, path: str, method: str, query: str = "") -> Optional[Any]:
            client_ip = self.client_address[0]
            # Allow internal local supervisor probe on /api/health when supervisor & backend share ephemeral secret
            probe_secret = runtime.environ.get("TRADELENS_SUPERVISOR_PROBE_SECRET")
            supplied_probe = self.headers.get("X-TradeLens-Supervisor-Probe")
            if (
                probe_secret
                and supplied_probe
                and path == "/api/health"
                and method == "GET"
                and not query
                and len(probe_secret) >= 12
                and is_allowed_remote_client_ip(client_ip, runtime.remote_access.allowed_vpn_cidrs)[0]
                and hmac.compare_digest(supplied_probe, probe_secret)
            ):
                from backend.app.runtime.security import AuthorizationDecision
                return AuthorizationDecision(
                    allowed=True,
                    reason_code="SUPERVISOR_HEALTH_PROBE",
                    message="Internal supervisor probe.",
                    status_code=200,
                    client_ip=client_ip,
                    is_loopback=True,
                )

            headers = {k: v for k, v in self.headers.items()}
            decision = runtime.remote_access.authorize_request(
                path=path,
                method=method,
                client_ip=client_ip,
                headers=headers,
                query=query,
                require_host_header=True,
            )
            if not decision.allowed:
                self._send_json(
                    decision.status_code,
                    {
                        "error": decision.message,
                        "reason_code": decision.reason_code,
                        "remote_available": False,
                        "storage_policy": "MEMORY_ONLY",
                    },
                    allowed_origin=None,
                )
                return None
            return decision

        def do_OPTIONS(self) -> None:
            parsed = urlparse(self.path)
            dec = self._authorize(parsed.path, "OPTIONS", query=parsed.query)
            if dec is None:
                return
            self._send_json(200, {"status": "ok"}, allowed_origin=dec.allowed_origin)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            dec = self._authorize(path, "GET", query=parsed.query)
            if dec is None:
                return

            if path in {"/", "/index.html"}:
                html_path = REPO_ROOT / "frontend" / "index.html"
                if html_path.exists():
                    _, hostname = _extract_hostname_without_port(self.headers.get("Host", "127.0.0.1"))
                    fmt_host = f"[{hostname}]" if ":" in hostname else (hostname or "127.0.0.1")
                    rendered, html_headers = render_hardened_html(
                        html_path.read_bytes(),
                        connect_origins=(
                            f"http://{fmt_host}:{self.server.server_port}",
                            f"https://{fmt_host}:{self.server.server_port}",
                        ),
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self._send_headers_list(html_headers)
                    self.end_headers()
                    self.wfile.write(rendered)
                    return
                self._send_json(404, {"error": "Dashboard HTML not found"}, allowed_origin=dec.allowed_origin)
                return

            if path in {"/api/health", "/api/runtime/status"}:
                self._send_json(200, runtime.get_health_payload(), allowed_origin=dec.allowed_origin)
                return

            if path == "/api/scan":
                sentiment = query.get("sentiment", ["BULLISH"])[0]
                try:
                    top_n = max(1, min(25, int(query.get("top_n", ["5"])[0])))
                except ValueError:
                    top_n = 5
                self._send_json(200, runtime.get_scan_payload(sentiment=sentiment, top_n=top_n), allowed_origin=dec.allowed_origin)
                return

            if path == "/api/positions":
                snap = runtime.health.evaluate(power_status=runtime.check_clock_and_power())
                if snap["data_status"] == "LIVE":
                    pos_fn = getattr(runtime.provider, "get_live_positions", None)
                    live_pos = runtime._call_provider_no_reconnect(pos_fn) if pos_fn else {}
                else:
                    live_pos = {
                        "positions": [],
                        "total_pnl": 0.0,
                        "realised_pnl": 0.0,
                        "unrealised_pnl": 0.0,
                        "has_live_positions": False,
                        "data_status": snap["data_status"],
                        "failure_reasons": snap["failure_reasons"],
                    }
                self._send_json(200, live_pos, allowed_origin=dec.allowed_origin)
                return

            if path == "/api/indices":
                _, snap = runtime._active_stock_universe()
                if snap["data_status"] == "LIVE":
                    indices = runtime._call_provider_no_reconnect(runtime.provider.get_indices)
                elif runtime.allow_sample_fallback:
                    indices = runtime.fallback_provider.get_indices()
                else:
                    indices = []
                self._send_json(
                    200,
                    {
                        "indices": indices,
                        "data_status": snap["data_status"],
                        "engine_status": snap["engine_status"],
                        "failure_reasons": snap["failure_reasons"],
                    },
                    allowed_origin=dec.allowed_origin,
                )
                return

            if path.startswith("/api/stock/"):
                symbol = path.split("/api/stock/", 1)[1].strip().upper()
                stocks, snap = runtime._active_stock_universe()
                target = next((s for s in stocks if s["symbol"] == symbol), None)
                if target:
                    profile = evaluate_single_stock_full_profile(target)
                    profile["data_status"] = snap["data_status"]
                    profile["engine_status"] = snap["engine_status"]
                    self._send_json(200, profile, allowed_origin=dec.allowed_origin)
                else:
                    self._send_json(
                        404 if snap["data_status"] in {"LIVE", "SIMULATED"} else 503,
                        {
                            "error": f"Symbol '{symbol}' unavailable.",
                            "data_status": snap["data_status"],
                            "failure_reasons": snap["failure_reasons"],
                        },
                        allowed_origin=dec.allowed_origin,
                    )
                return

            if path == "/api/ledger":
                self._send_json(200, runtime.ledger.get_summary(), allowed_origin=dec.allowed_origin)
                return

            if path == "/api/remote/devices":
                status_f = query.get("status", [None])[0]
                self._send_json(
                    200,
                    {
                        "devices": runtime.remote_access.list_devices(status_filter=status_f),
                        "connectivity": runtime.remote_access.summarize_connectivity(),
                    },
                    allowed_origin=dec.allowed_origin,
                )
                return

            if path == "/api/setup/state":
                setup_state = runtime.stage_b_setup.get_setup_state(
                    persist=False,
                    is_remote_view=not dec.is_loopback,
                    remote_device_id=dec.device_id,
                )
                self._send_json(200, setup_state, allowed_origin=dec.allowed_origin)
                return

            if path == "/api/universe":
                stocks, snap = runtime._active_stock_universe()
                summary = [
                    {
                        "symbol": s["symbol"],
                        "name": s["name"],
                        "category": s.get("category", "LargeCap"),
                        "cmp": s["candles"][-1]["close"] if s.get("candles") else s.get("cmp"),
                        "lot_size": s.get("lot_size", 250),
                    }
                    for s in stocks
                ]
                self._send_json(
                    200,
                    {
                        "total": len(summary),
                        "stocks": summary,
                        "data_status": snap["data_status"],
                        "failure_reasons": snap["failure_reasons"],
                    },
                    allowed_origin=dec.allowed_origin,
                )
                return

            self._send_json(404, {"error": "Endpoint not found"}, allowed_origin=dec.allowed_origin)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            decision = self._authorize(path, "POST", query=parsed.query)
            if decision is None:
                return

            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_length = 0
            if content_length < 0 or content_length > 1_048_576:
                self._send_json(413, {"error": "Request payload too large"}, allowed_origin=decision.allowed_origin)
                return
            body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                self._send_json(400, {"error": "Invalid JSON body"}, allowed_origin=decision.allowed_origin)
                return

            if (
                payload.get("execute_order")
                or payload.get("place_order")
                or payload.get("order_execution_enabled")
            ):
                self._send_json(
                    403,
                    {
                        "error": "Order execution is strictly disabled in TradeLens AI.",
                        "reason_code": "ORDER_EXECUTION_DISABLED",
                        "order_execution_enabled": False,
                    },
                    allowed_origin=decision.allowed_origin,
                )
                return

            if path == "/api/remote/challenge":
                chg_data, chg_decision = runtime.remote_access.issue_auth_challenge(
                    device_id=str(payload.get("device_id", "")),
                    client_ip=decision.client_ip or self.client_address[0],
                    ttl_seconds=payload.get("ttl_seconds"),
                )
                if not chg_decision.allowed or not chg_data:
                    self._send_json(
                        chg_decision.status_code,
                        {
                            "error": chg_decision.message,
                            "reason_code": chg_decision.reason_code,
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                    return
                self._send_json(200, chg_data, allowed_origin=decision.allowed_origin)
                return

            if path == "/api/remote/auth":
                if payload.get("device_secret") and not payload.get("signature"):
                    self._send_json(
                        401,
                        {
                            "error": "Shared device_secret authentication has been retired. Use cryptographic challenge signature.",
                            "reason_code": "LEGACY_SHARED_SECRET_NOT_SUPPORTED",
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                    return
                if payload.get("owner_secret"):
                    self._send_json(
                        400,
                        {
                            "error": (
                                "TRADELENS_OWNER_SECRET must stay exclusively on Home Mac and cannot be used or transmitted "
                                "for remote workstation authentication; authenticate using your approved device key challenge signature."
                            ),
                            "reason_code": "OWNER_SECRET_NOT_PERMITTED_IN_REMOTE_AUTH",
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                    return
                session_data, auth_decision = runtime.remote_access.authenticate_remote_session(
                    device_id=str(payload.get("device_id", "")),
                    challenge_id=str(payload.get("challenge_id", "")),
                    signature_b64=str(payload.get("signature", payload.get("signature_b64", ""))),
                    client_ip=decision.client_ip or self.client_address[0],
                    ttl_seconds=payload.get("ttl_seconds"),
                )
                if not auth_decision.allowed or not session_data:
                    self._send_json(
                        auth_decision.status_code,
                        {
                            "error": auth_decision.message,
                            "reason_code": auth_decision.reason_code,
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                    return
                session_data["engine"] = runtime.get_health_payload()
                # Emit newly issued Bearer token once over no-store channel without redact scrubbing it
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._send_headers_list(
                    build_security_headers(allowed_origin=decision.allowed_origin, is_html=False)
                )
                self.end_headers()
                self.wfile.write(json.dumps(session_data, indent=2).encode("utf-8"))
                return

            if path == "/api/remote/heartbeat":
                health_snap = runtime.get_health_payload()
                self._send_json(
                    200,
                    {
                        "heartbeat": "recorded",
                        "device_id": decision.device_id,
                        "proves_live_market_data": False,
                        "health": health_snap,
                    },
                    allowed_origin=decision.allowed_origin,
                )
                return

            if path == "/api/remote/logout":
                auth_hdr = self.headers.get("Authorization", "")
                token = auth_hdr[7:].strip() if auth_hdr.lower().startswith("bearer ") else ""
                revoked = runtime.remote_access.revoke_session_by_token(token, reason="EXPLICIT_LOGOUT")
                self._send_json(200, {"logged_out": revoked}, allowed_origin=decision.allowed_origin)
                return

            if path == "/api/broker/authenticate":
                snap = runtime.authenticate_broker(verify_fresh_tick=bool(payload.get("verify_fresh_tick", True)))
                status = 200 if snap["signals"]["broker_auth_status"] == BrokerAuthState.AUTHENTICATED else 503
                self._send_json(status, snap, allowed_origin=decision.allowed_origin)
                return

            if path == "/api/runtime/shutdown":
                reason = str(payload.get("reason") or "OWNER_API_SHUTDOWN")
                self._send_json(200, runtime.request_owner_shutdown(reason=reason), allowed_origin=decision.allowed_origin)
                return

            if path == "/api/remote/devices/register":
                try:
                    owner_sec = self.headers.get("X-TradeLens-Owner-Secret") or str(payload.get("owner_secret", ""))
                    raw_ips = payload.get("allowed_ips")
                    allowed_ips_arg = list(raw_ips) if isinstance(raw_ips, list) else None
                    rec = runtime.remote_access.register_device(
                        device_id=str(payload.get("device_id", "")),
                        device_name=str(payload.get("device_name", "")),
                        public_key_pem=str(payload.get("public_key_pem", "")),
                        owner_secret=owner_sec,
                        approve=bool(payload.get("approve", True)),
                        allow_re_enroll=bool(payload.get("allow_re_enroll", False)),
                        allowed_ips=allowed_ips_arg,
                    )
                    self._send_json(
                        200,
                        {
                            "status": "REGISTERED",
                            "device_id": rec.device_id,
                            "device_status": rec.status,
                            "public_key_fingerprint": rec.public_key_fingerprint,
                            "allowed_ips": list(rec.allowed_ips),
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(400, {"error": redact_runtime_text(str(exc), environ=runtime.environ)}, allowed_origin=decision.allowed_origin)
                return

            if path == "/api/remote/devices/approve":
                try:
                    owner_sec = self.headers.get("X-TradeLens-Owner-Secret") or str(payload.get("owner_secret", ""))
                    raw_ips = payload.get("allowed_ips")
                    allowed_ips_arg = list(raw_ips) if isinstance(raw_ips, list) else None
                    rec = runtime.remote_access.approve_device(
                        device_id=str(payload.get("device_id", "")),
                        owner_secret=owner_sec,
                        activate=bool(payload.get("activate", True)),
                        allowed_ips=allowed_ips_arg,
                    )
                    self._send_json(
                        200,
                        {
                            "status": "APPROVED",
                            "device_id": rec.device_id,
                            "device_status": rec.status,
                            "public_key_fingerprint": rec.public_key_fingerprint,
                            "allowed_ips": list(rec.allowed_ips),
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(400, {"error": redact_runtime_text(str(exc), environ=runtime.environ)}, allowed_origin=decision.allowed_origin)
                return

            if path == "/api/remote/devices/revoke":
                try:
                    owner_sec = self.headers.get("X-TradeLens-Owner-Secret") or str(payload.get("owner_secret", ""))
                    cnt = runtime.remote_access.revoke_device(
                        device_id=str(payload.get("device_id", "")),
                        owner_secret=owner_sec,
                        reason=str(payload.get("reason", "OWNER_REVOKED")),
                    )
                    self._send_json(
                        200,
                        {"status": "REVOKED", "revoked_sessions_count": cnt},
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/remote/devices/remove":
                try:
                    owner_sec = self.headers.get("X-TradeLens-Owner-Secret") or str(payload.get("owner_secret", ""))
                    res = runtime.remote_access.remove_device(
                        device_id=str(payload.get("device_id", "")),
                        owner_secret=owner_sec,
                        require_revoked=bool(payload.get("require_revoked", True)),
                        reason=str(payload.get("reason", "REMOVED_BY_OWNER")),
                    )
                    self._send_json(200, res, allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/setup/run-task":
                try:
                    if not decision.is_loopback or not decision.is_owner_verified:
                        raise PermissionError("HOME_OWNER_ONLY_OPERATION")
                    raw_extra = payload.get("extra_args")
                    if raw_extra not in (None, {}):
                        raise ValueError("HTTP_EXTRA_ARGS_NOT_ALLOWED")
                    res = runtime.stage_b_setup.run_allowlisted_task(
                        task_id=str(payload.get("task_id", "")),
                        confirmed=bool(payload.get("confirmed", False)),
                        audit_note=str(payload.get("audit_note", "")),
                        is_loopback=bool(decision.is_loopback),
                        extra_args=None,
                        called_from_http=True,
                    )
                    self._send_json(200, res, allowed_origin=decision.allowed_origin)
                except PermissionError as exc:
                    self._send_json(
                        403,
                        {
                            "error": redact_runtime_text(str(exc), environ=runtime.environ),
                            "reason_code": "SETUP_ACTION_FORBIDDEN",
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/setup/step/confirm":
                try:
                    if not decision.is_loopback or not decision.is_owner_verified:
                        raise PermissionError("HOME_OWNER_ONLY_OPERATION")
                    res = runtime.stage_b_setup.confirm_manual_step(
                        step_id=str(payload.get("step_id", "")),
                        confirmed=bool(payload.get("confirmed", False)),
                        audit_note=str(payload.get("audit_note", "")),
                        is_loopback=bool(decision.is_loopback),
                    )
                    self._send_json(200, res, allowed_origin=decision.allowed_origin)
                except PermissionError as exc:
                    self._send_json(
                        403,
                        {
                            "error": redact_runtime_text(str(exc), environ=runtime.environ),
                            "reason_code": "SETUP_CONFIRMATION_FORBIDDEN",
                        },
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/positions/monitor":
                try:
                    positions = payload.get("positions", [])
                    stocks, snap = runtime._active_stock_universe()
                    profiles = {s["symbol"]: evaluate_single_stock_full_profile(s) for s in stocks}
                    alerts = monitor_open_positions(positions, profiles)
                    self._send_json(
                        200,
                        {"alerts": alerts, "data_status": snap["data_status"]},
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/verify-tip":
                try:
                    symbol = str(payload.get("symbol", "")).strip().upper()
                    instrument_type = str(payload.get("instrument_type", "CE")).strip().upper()
                    tip_entry = float(payload.get("tip_entry", 0.0))
                    tip_sl = float(payload.get("tip_sl", 0.0))
                    tip_target_1 = float(payload.get("tip_target_1", 0.0))
                    tip_target_2 = float(payload.get("tip_target_2", 0.0)) if payload.get("tip_target_2") else None
                    strike_price = float(payload.get("strike_price", 0.0)) if payload.get("strike_price") else None
                    lots = int(payload.get("lots", 1))
                    user_capital = float(payload.get("user_capital", runtime.ledger.data.get("initial_capital", 50000.0)))
                    max_risk_pct = float(payload.get("max_risk_pct", 3.0))

                    stocks, snap = runtime._active_stock_universe()
                    target_stock = next((s for s in stocks if s["symbol"] == symbol), None)
                    live_quote = None
                    if target_stock:
                        cmp_price = target_stock["candles"][-1]["close"] if target_stock.get("candles") else target_stock.get("cmp", tip_entry)
                        lot_size = target_stock.get("lot_size", 250)
                        live_quote = {"ltp": cmp_price, "lot_size": lot_size, "option_ltp": tip_entry}

                    result = verify_tip_setup(
                        symbol=symbol,
                        instrument_type=instrument_type,
                        tip_entry=tip_entry,
                        tip_sl=tip_sl,
                        tip_target_1=tip_target_1,
                        tip_target_2=tip_target_2,
                        strike_price=strike_price,
                        lots=lots,
                        user_capital=user_capital,
                        max_risk_pct=max_risk_pct,
                        stock_record=target_stock,
                        live_quote=live_quote,
                    )
                    result["data_status"] = snap["data_status"]
                    self._send_json(200, result, allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/ledger/trade":
                try:
                    summary = runtime.ledger.record_trade(
                        symbol=payload.get("symbol", ""),
                        instrument_type=payload.get("type", "CE"),
                        lots=int(payload.get("lots", 1)),
                        entry_price=float(payload.get("entry_price", 0.0)),
                        exit_price=float(payload.get("exit_price", 0.0)),
                        lot_size=int(payload.get("lot_size", 250)),
                        notes=payload.get("notes", ""),
                    )
                    self._send_json(200, summary, allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/ledger/trade/delete":
                try:
                    summary = runtime.ledger.delete_trade(payload.get("id", ""))
                    self._send_json(200, summary, allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/ledger/clear":
                try:
                    self._send_json(200, runtime.ledger.clear_all_trades(), allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/ledger/import-chat":
                try:
                    self._send_json(200, runtime.ledger.load_chat_history(), allowed_origin=decision.allowed_origin)
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            if path == "/api/ledger/settings":
                try:
                    initial_capital = float(payload.get("initial_capital", 50000.0))
                    max_daily_loss_pct = float(payload.get("max_daily_loss_pct", 5.0))
                    self._send_json(
                        200,
                        runtime.ledger.update_settings(initial_capital, max_daily_loss_pct),
                        allowed_origin=decision.allowed_origin,
                    )
                except Exception as exc:
                    self._send_json(
                        400,
                        {"error": redact_runtime_text(str(exc), environ=runtime.environ)},
                        allowed_origin=decision.allowed_origin,
                    )
                return

            self._send_json(404, {"error": "Endpoint not found"}, allowed_origin=decision.allowed_origin)

    return TradeLensAPIHandler


def _is_loopback_bind(host: str) -> bool:
    clean = (host or "").strip().lower()
    if clean == "localhost":
        return True
    try:
        return _parse_ip_clean(clean).is_loopback
    except ValueError:
        return False


def build_api_server(
    port: int = 8000,
    bind_host: str = "127.0.0.1",
    runtime: Optional[TradeLensAppRuntime] = None,
    tls_certfile: Optional[str | Path] = None,
    tls_keyfile: Optional[str | Path] = None,
    allow_insecure_remote_http: bool = False,
) -> Tuple[ThreadingTradeLensServer, TradeLensAppRuntime]:
    rt = runtime or TradeLensAppRuntime()
    safe_host = validate_bind_host(bind_host, allowed_vpn_cidrs=rt.remote_access.allowed_vpn_cidrs)
    if not _is_loopback_bind(safe_host) and not allow_insecure_remote_http:
        if not tls_certfile or not tls_keyfile:
            raise ValueError(
                "TLS certificate and key (--tls-cert/--tls-key or TRADELENS_TLS_CERT_FILE/TRADELENS_TLS_KEY_FILE) "
                "are required when binding to a non-loopback remote VPN interface."
            )
    httpd = ThreadingTradeLensServer((safe_host, int(port)), make_api_handler(rt))
    tls_on = wrap_server_tls(httpd, tls_certfile=tls_certfile, tls_keyfile=tls_keyfile)
    rt.remote_access.tls_enabled = tls_on
    rt.httpd = httpd
    return httpd, rt


def run_server(port: int = 8000, bind_host: Optional[str] = None) -> None:
    prelim_env = read_env_dict(require_strict_perms=False)
    host = bind_host or prelim_env.get("TRADELENS_BIND_HOST", "127.0.0.1")
    remote_mode = (
        prelim_env.get("TRADELENS_REMOTE_ACCESS_ENABLED", "0").strip() == "1"
        or bool(prelim_env.get("TRADELENS_ALLOWED_VPN_CIDRS", "").strip())
        or bool(prelim_env.get("TRADELENS_ALLOWED_PEER_IPS", "").strip())
        or not _is_loopback_bind(host)
    )
    load_env(require_strict_perms=remote_mode)
    host = bind_host or os.environ.get("TRADELENS_BIND_HOST", "127.0.0.1")
    cert = os.environ.get("TRADELENS_TLS_CERT_FILE")
    key = os.environ.get("TRADELENS_TLS_KEY_FILE")
    httpd, rt = build_api_server(port=port, bind_host=host, tls_certfile=cert, tls_keyfile=key)
    scheme = "https" if rt.remote_access.tls_enabled else "http"
    rt.logger.info("TradeLens AI Home Backend listening strictly on %s://%s:%s (no public listener)", scheme, host, port)

    admin_httpd: Optional[ThreadingTradeLensServer] = None
    if not _is_loopback_bind(host):
        raw_admin_port = os.environ.get("TRADELENS_LOCAL_ADMIN_PORT", "").strip()
        if raw_admin_port.lower() not in {"0", "off", "false", "disabled"}:
            if raw_admin_port:
                local_admin_port = int(raw_admin_port)
            else:
                local_admin_port = port + 1 if (port > 0 and port + 1 != 8080) else (port + 2 if port > 0 else 0)
            if local_admin_port > 0:
                admin_httpd = ThreadingTradeLensServer(("127.0.0.1", local_admin_port), make_api_handler(rt))
                admin_thread = threading.Thread(
                    target=admin_httpd.serve_forever,
                    name="tradelens-loopback-admin",
                    daemon=True,
                )
                admin_thread.start()
                rt.logger.info(
                    "TradeLens AI Owner Loopback Admin listening strictly on http://127.0.0.1:%s",
                    local_admin_port,
                )

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        rt.logger.info("Received KeyboardInterrupt; shutting down cleanly.")
    finally:
        rt.power_monitor.mark_clean_shutdown(reason="SERVER_EXIT")
        if admin_httpd is not None:
            admin_httpd.shutdown()
            admin_httpd.server_close()
        httpd.server_close()


if __name__ == "__main__":
    server_port = int(os.environ.get("PORT", 8000))
    server_host = os.environ.get("TRADELENS_BIND_HOST", "127.0.0.1")
    run_server(port=server_port, bind_host=server_host)
