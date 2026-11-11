"""
Phase 3E.5.2.5.1 (Stage A.1) — Final Pre-Provisioning Security Gate Verification Tests (Offline).

Covers all 12 mandatory offline Stage A.1 verification scenarios:
 1. Exact approved Tailscale peer IP enforced on backend & frontend; unapproved Tailscale peer rejected 403.
 2. Office Mac Tailscale IP change immediately invalidates existing session & rejects challenge/auth from new IP.
 3. Missing repo `.env` never walks parent directories; missing/short TRADELENS_STATE_HMAC_SECRET fails closed in remote mode.
 4. LaunchAgent wrapper script (`deploy/scripts/run_launchd_service.sh`) and CLI `.env` loader fail closed on 0644/symlink `.env`.
 5. `cmd_restart` preserves security/TLS/peer-IP config, refuses silent downgrade without `--allow-downgrade`, and crash restarts keep spec.
 6. Browser RAM key loss on reload requires explicit owner re-enrollment approval; revoked device cannot silently overwrite key.
 7. Cross-process active session revocation & removal (`revoke-device` / `remove-device`) takes effect immediately without restart.
 8. Strict TLS X509 checks fail closed on SAN mismatch, wrong CA/cert, expired cert, and HTTP 401/403/404/500 health probes.
 9. Remote bearer session strictly read-only: ledger/runtime/device/broker mutations rejected 403; indirect broker `connect()` never triggered.
10. Order execution permanently disabled across property, health signal, env flags, POST payloads, order path guards, and source/AST scan.
11. `owner_shutdown.flag` blocks `restart`/`supervise`, whereas crash recovery restarts without bypass and `resume` keeps security config intact.
12. Secret redaction across supervisor logs, status files, runtime_config, plists, HTTP errors, and `--owner-secret-stdin`.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from backend.app import main as main_mod
from backend.app.main import (
    TradeLensAppRuntime,
    load_env,
    make_api_handler,
    read_env_dict,
)
from backend.app.data.angel_one import AngelOneSmartAPIProvider
from backend.app.runtime import cli as runtime_cli
from backend.app.runtime.frontend_server import FrontendStaticHandler
from backend.app.runtime.health_and_safety import RuntimeHealthCoordinator
from backend.app.runtime.launchd_config import render_launchd_plists
from backend.app.runtime.power_policy import PowerAndSleepMonitor
from backend.app.runtime.security import (
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_PENDING,
    DEVICE_STATUS_REVOKED,
    DeviceKeyCrypto,
    RemoteAccessManager,
    is_allowed_remote_client_ip,
    is_forbidden_order_execution_path,
    normalize_allowed_peer_ips,
    parse_dotenv_content,
)
from backend.app.runtime.supervisor import (
    RestartPolicy,
    SupervisedServiceSpec,
    TradeLensSupervisor,
    default_http_health_probe,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
WRAPPER_SH = REPO_ROOT / "deploy" / "scripts" / "run_launchd_service.sh"
STRONG_HMAC = "stage_a1_hmac_secret_min_32_bytes_2026!"
STRONG_OWNER = "HomeMacOwner!A1#2026"


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 11, 9, 20, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class _FakeSocket:
    def makefile(self, *_args: Any, **_kwargs: Any) -> io.BytesIO:
        return io.BytesIO()


class _FakeServer:
    server_port = 8000


def _dispatch_http(
    handler_cls: Any,
    method: str,
    path: str,
    *,
    client_ip: str = "127.0.0.1",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any, Dict[str, str]]:
    req_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
    hdr_map = dict(headers or {})
    hdr_map.setdefault("Host", "127.0.0.1:8000")
    if body is not None:
        hdr_map.setdefault("Content-Type", "application/json")
        hdr_map["Content-Length"] = str(len(req_bytes))

    raw_out = io.BytesIO()

    class _MockHandler(handler_cls):
        def __init__(self) -> None:
            self.rfile = io.BytesIO(req_bytes)
            self.wfile = raw_out
            self.client_address = (client_ip, 52345)
            self.request_version = "HTTP/1.1"
            self.command = method
            self.path = path
            self.close_connection = True
            self.server = _FakeServer()
            self.request = _FakeSocket()
            msg = HTTPMessage()
            for k, v in hdr_map.items():
                msg[k] = v
            self.headers = msg
            self._headers_buffer: List[bytes] = []

        def send_response(self, code: int, message: Optional[str] = None) -> None:
            self._status_code = code
            self._resp_headers: Dict[str, str] = {}

        def send_header(self, keyword: str, value: str) -> None:
            self._resp_headers[keyword.lower()] = value

        def end_headers(self) -> None:
            return None

    h = _MockHandler()
    getattr(h, f"do_{method}")()
    text = raw_out.getvalue().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return getattr(h, "_status_code", 500), parsed, getattr(h, "_resp_headers", {})


class _DummyProvider:
    api_key = "CANARY_BROKER_API_KEY_9911"
    client_code = "CANARY_CLIENT_CODE_9922"
    password = "CANARY_BROKER_PASSWORD_9933"
    totp_secret = "JBSWY3DPEHPK3PXP"

    def __init__(self) -> None:
        self.jwt_token = "CANARY_BROKER_JWT_9944"
        self.feed_token = "CANARY_BROKER_FEED_9955"
        self.is_authenticated = True
        self.connect_calls = 0

    def connect(self) -> bool:
        self.connect_calls += 1
        self.is_authenticated = True
        self.jwt_token = "CANARY_BROKER_JWT_REFRESHED"
        return True

    def get_all_stocks(self, **kwargs: Any) -> List[Dict[str, Any]]:
        _ = kwargs
        return [
            {
                "symbol": "INFY",
                "name": "Infosys Ltd",
                "price": 1825.0,
                "change": 12.0,
                "change_pct": 0.66,
                "sector": "IT",
                "high": 1835.0,
                "low": 1805.0,
                "volume": 2_000_000,
                "rsi": 58.0,
                "adx": 27.0,
                "data_source": "angel_one_live",
                "data_timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]

    def get_indices(self, **kwargs: Any) -> List[Dict[str, Any]]:
        _ = kwargs
        return [{"symbol": "NIFTY 50", "ltp": 24500.0, "change": 45.0, "change_pct": 0.18}]


class TestPreProvisioningSecurityGateStageA1(unittest.TestCase):
    def _make_runtime_and_handlers(
        self,
        tmp: Path,
        *,
        allowed_peer_ips: Optional[List[str]] = None,
        provider: Optional[Any] = None,
        clock: Optional[_FakeClock] = None,
    ) -> Tuple[TradeLensAppRuntime, Any, Any, str, str]:
        clk = clock or _FakeClock()
        priv_pem, pub_pem = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=tmp / "state" / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=allowed_peer_ips or ["100.64.0.25"],
            allowed_hosts=["127.0.0.1", "100.64.0.10", "home-mac.tailnet.ts.net"],
            allowed_origins=["https://100.64.0.10:8080", "https://home-mac.tailnet.ts.net:8080"],
            state_hmac_secret=STRONG_HMAC,
            remote_mode_enabled=True,
            clock=clk.now_utc,
        )
        mgr.register_device(
            device_id="office-mac-1",
            device_name="Office MacBook",
            public_key_pem=pub_pem,
            owner_secret=STRONG_OWNER,
            approve=True,
            allowed_ips=allowed_peer_ips or ["100.64.0.25"],
        )
        prov = provider or _DummyProvider()
        env = {
            "ANGEL_API_KEY": _DummyProvider.api_key,
            "ANGEL_CLIENT_CODE": _DummyProvider.client_code,
            "ANGEL_PASSWORD": _DummyProvider.password,
            "ANGEL_TOTP_SECRET": _DummyProvider.totp_secret,
            "TRADELENS_OWNER_SECRET": STRONG_OWNER,
            "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC,
            "TRADELENS_ALLOWED_VPN_CIDRS": "100.64.0.0/10",
            "TRADELENS_ALLOWED_PEER_IPS": ",".join(allowed_peer_ips or ["100.64.0.25"]),
            "TRADELENS_REMOTE_ACCESS_ENABLED": "1",
        }
        rt = TradeLensAppRuntime(
            state_dir=tmp / "state",
            provider=prov,
            remote_access=mgr,
            health_coordinator=RuntimeHealthCoordinator(clock=clk.now_utc),
            power_monitor=PowerAndSleepMonitor(
                heartbeat_path=tmp / "state" / "heartbeat.json",
                wall_clock=clk.now_utc,
                mono_clock=lambda: clk.now_utc().timestamp(),
            ),
            ledger_path=tmp / "state" / "ledger.json",
            environ=env,
        )
        api_handler = make_api_handler(rt)

        fe_peers = tuple(normalize_allowed_peer_ips(allowed_peer_ips or ["100.64.0.25"]))

        class _ConfiguredFrontend(FrontendStaticHandler):
            index_path = INDEX_HTML
            allowed_vpn_cidrs = ("100.64.0.0/10",)
            allowed_peer_ips = fe_peers
            allowed_hosts = ("127.0.0.1", "100.64.0.10", "home-mac.tailnet.ts.net")
            allowed_origins = ("https://100.64.0.10:8080", "https://home-mac.tailnet.ts.net:8080")
            backend_port = 8000

        return rt, api_handler, _ConfiguredFrontend, priv_pem, pub_pem

    def _authenticate_http(
        self,
        api_handler: Any,
        *,
        device_id: str,
        priv_pem: str,
        client_ip: str = "100.64.0.25",
    ) -> str:
        st_c, b_c, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/challenge",
            client_ip=client_ip,
            headers={"Host": "100.64.0.10:8000", "Origin": "https://100.64.0.10:8080"},
            body={"device_id": device_id},
        )
        self.assertEqual(st_c, 200, f"Challenge failed: {b_c}")
        sig = DeviceKeyCrypto.sign_challenge(priv_pem, b_c["challenge_string"])
        st_a, b_a, _ = _dispatch_http(
            api_handler,
            "POST",
            "/api/remote/auth",
            client_ip=client_ip,
            headers={"Host": "100.64.0.10:8000", "Origin": "https://100.64.0.10:8080"},
            body={"device_id": device_id, "challenge_id": b_c["challenge_id"], "signature": sig},
        )
        self.assertEqual(st_a, 200, f"Auth failed: {b_a}")
        return str(b_a["access_token"])

    # 1. Tailscale peer-IP allowlist vs broader CGNAT 100.64.0.0/10
    def test_01_exact_approved_peer_ip_backend_and_frontend_enforced(self) -> None:
        self.assertEqual(normalize_allowed_peer_ips(["100.64.0.25/32"]), ["100.64.0.25"])
        with self.assertRaises(ValueError):
            normalize_allowed_peer_ips(["100.64.0.0/10"])
        with self.assertRaises(ValueError):
            normalize_allowed_peer_ips(["0.0.0.0/0"])

        self.assertEqual(
            is_allowed_remote_client_ip("100.64.0.25", ["100.64.0.0/10"], ["100.64.0.25"]),
            (True, "TAILSCALE_MESH_IP"),
        )
        self.assertEqual(
            is_allowed_remote_client_ip("100.64.0.88", ["100.64.0.0/10"], ["100.64.0.25"]),
            (False, "UNAPPROVED_TAILSCALE_PEER"),
        )

        with tempfile.TemporaryDirectory() as td:
            rt, api_handler, fe_handler, priv_pem, _ = self._make_runtime_and_handlers(
                Path(td), allowed_peer_ips=["100.64.0.25"]
            )
            token = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv_pem, client_ip="100.64.0.25")

            # Approved Office Mac IP succeeds on backend & frontend
            st_b_ok, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
            )
            self.assertEqual(st_b_ok, 200)

            st_f_ok, _, _ = _dispatch_http(
                fe_handler,
                "GET",
                "/index.html",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8080"},
            )
            self.assertEqual(st_f_ok, 200)

            # Unapproved Tailscale peer inside 100.64.0.0/10 rejected 403 on BOTH backend and frontend
            st_b_rogue, b_rogue, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.88",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
            )
            self.assertEqual(st_b_rogue, 403)
            self.assertEqual(b_rogue["reason_code"], "UNAPPROVED_TAILSCALE_PEER")

            st_b_chg, b_chg, _ = _dispatch_http(
                api_handler,
                "POST",
                "/api/remote/challenge",
                client_ip="100.64.0.88",
                headers={"Host": "100.64.0.10:8000"},
                body={"device_id": "office-mac-1"},
            )
            self.assertEqual(st_b_chg, 403)
            self.assertEqual(b_chg["reason_code"], "UNAPPROVED_TAILSCALE_PEER")

            st_f_rogue, f_rogue, _ = _dispatch_http(
                fe_handler,
                "GET",
                "/index.html",
                client_ip="100.64.0.88",
                headers={"Host": "100.64.0.10:8080"},
            )
            self.assertEqual(st_f_rogue, 403)
            self.assertEqual(f_rogue["reason_code"], "UNAPPROVED_TAILSCALE_PEER")

            # Supervisor probe secret allows health route on bind host IP only with matching secret
            with (
                patch.dict(os.environ, {"TRADELENS_SUPERVISOR_PROBE_SECRET": "sup_probe_secret_abc_123"}, clear=False),
                patch.dict(rt.environ, {"TRADELENS_SUPERVISOR_PROBE_SECRET": "sup_probe_secret_abc_123"}, clear=False),
            ):
                st_h_no, _, _ = _dispatch_http(
                    api_handler,
                    "GET",
                    "/api/health",
                    client_ip="100.64.0.10",
                    headers={"Host": "100.64.0.10:8000"},
                )
                self.assertEqual(st_h_no, 403)

                st_h_ok, _, _ = _dispatch_http(
                    api_handler,
                    "GET",
                    "/api/health",
                    client_ip="100.64.0.10",
                    headers={"Host": "100.64.0.10:8000", "X-TradeLens-Supervisor-Probe": "sup_probe_secret_abc_123"},
                )
                self.assertEqual(st_h_ok, 200)

                st_fh_ok, _, _ = _dispatch_http(
                    fe_handler,
                    "GET",
                    "/frontend-health",
                    client_ip="100.64.0.10",
                    headers={"Host": "100.64.0.10:8080", "X-TradeLens-Supervisor-Probe": "sup_probe_secret_abc_123"},
                )
                self.assertEqual(st_fh_ok, 200)

                st_scan_bad, _, _ = _dispatch_http(
                    api_handler,
                    "GET",
                    "/api/scan",
                    client_ip="100.64.0.10",
                    headers={"Host": "100.64.0.10:8000", "X-TradeLens-Supervisor-Probe": "sup_probe_secret_abc_123"},
                )
                self.assertEqual(st_scan_bad, 403)

    # 2. Peer IP change invalidates active session + enforces per-device IP allowlist
    def test_02_peer_ip_change_invalidates_existing_session_and_per_device_ip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Allow both IPs at global layer so we prove session IP binding AND per-device IP restriction independently
            rt, api_handler, _, priv_pem, pub_pem = self._make_runtime_and_handlers(
                Path(td), allowed_peer_ips=["100.64.0.25", "100.64.0.26"]
            )
            rt.remote_access.register_device(
                device_id="office-pinned",
                device_name="Office Pinned",
                public_key_pem=pub_pem,
                owner_secret=STRONG_OWNER,
                approve=True,
                allowed_ips=["100.64.0.25"],
            )
            token = self._authenticate_http(api_handler, device_id="office-pinned", priv_pem=priv_pem, client_ip="100.64.0.25")
            st_ok, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
            )
            self.assertEqual(st_ok, 200)

            # Office Mac Tailscale IP drifts from .25 -> .26: existing token MUST fail closed
            st_moved, b_moved, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.26",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
            )
            self.assertEqual(st_moved, 403)
            self.assertIn(b_moved["reason_code"], {"DEVICE_IP_NOT_ALLOWED", "SESSION_IP_MISMATCH"})

            # Device pinned to .25 cannot even initiate a new challenge from .26
            st_chg, b_chg, _ = _dispatch_http(
                api_handler,
                "POST",
                "/api/remote/challenge",
                client_ip="100.64.0.26",
                headers={"Host": "100.64.0.10:8000"},
                body={"device_id": "office-pinned"},
            )
            self.assertEqual(st_chg, 403)
            self.assertEqual(b_chg["reason_code"], "DEVICE_IP_NOT_ALLOWED")

    # 3. Missing repo .env never walks parents + missing HMAC fails closed in remote mode
    def test_03_missing_repo_env_never_walks_parent_and_missing_hmac_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            child_repo = parent / "workspace_repo"
            child_repo.mkdir(parents=True)
            (parent / ".env").write_text(
                "ANGEL_API_KEY=SHOULD_NEVER_LOAD_FROM_PARENT\nTRADELENS_OWNER_SECRET=ParentLeak!\n",
                encoding="utf-8",
            )
            with patch.object(main_mod, "REPO_ROOT", child_repo):
                loaded = read_env_dict(base_env={})
                self.assertNotIn("ANGEL_API_KEY", loaded)
                self.assertNotIn("TRADELENS_OWNER_SECRET", loaded)

            # Remote mode without TRADELENS_STATE_HMAC_SECRET (or < 16 chars) fails closed
            for bad_hmac in (None, "", "too_short"):
                with self.assertRaises((PermissionError, RuntimeError, ValueError)):
                    RemoteAccessManager(
                        state_path=parent / "rstate" / "remote_auth.json",
                        allowed_vpn_cidrs=["100.64.0.0/10"],
                        allowed_peer_ips=["100.64.0.25"],
                        state_hmac_secret=bad_hmac,
                        remote_mode_enabled=True,
                    )

    # 4. LaunchAgent wrapper & CLI/main .env loader fail closed on 0644 or symlinked .env
    def test_04_launchd_wrapper_and_cli_dotenv_fail_closed_on_permissive_or_symlink_env(self) -> None:
        self.assertTrue(WRAPPER_SH.exists())
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            canary_secret = "CANARY_WRAPPER_HMAC_SECRET_987654321"
            perm_env = tmp / "perm.env"
            perm_env.write_text(
                f"TRADELENS_STATE_HMAC_SECRET={canary_secret}\nTRADELENS_OWNER_SECRET={STRONG_OWNER}\n",
                encoding="utf-8",
            )
            os.chmod(perm_env, 0o644)

            sym_env = tmp / "sym.env"
            os.symlink(perm_env, sym_env)

            short_env = tmp / "short.env"
            short_env.write_text("TRADELENS_STATE_HMAC_SECRET=short\n", encoding="utf-8")
            os.chmod(short_env, 0o600)

            good_env = tmp / "good.env"
            good_env.write_text(
                f'TRADELENS_STATE_HMAC_SECRET="{canary_secret}"\n'
                f'TRADELENS_OWNER_SECRET="{STRONG_OWNER}"\n'
                'ANGEL_API_KEY="SHOULD_STAY_OUT_OF_CLI"\n',
                encoding="utf-8",
            )
            os.chmod(good_env, 0o600)

            clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp)}

            # Python loaders fail closed
            with self.assertRaises(PermissionError):
                read_env_dict(base_env={}, env_path=perm_env, require_strict_perms=True)
            with self.assertRaises(PermissionError):
                read_env_dict(base_env={}, env_path=sym_env, require_strict_perms=True)

            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(good_env)}, clear=True):
                runtime_cli._load_cli_dotenv(strict_perms=True)
                self.assertEqual(os.environ.get("TRADELENS_STATE_HMAC_SECRET"), canary_secret)
                self.assertNotIn("ANGEL_API_KEY", os.environ)
            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(perm_env)}, clear=False):
                with self.assertRaises(PermissionError):
                    runtime_cli._load_cli_dotenv(strict_perms=True)
            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(sym_env)}, clear=False):
                with self.assertRaises(PermissionError):
                    runtime_cli._load_cli_dotenv(strict_perms=True)

            # Shell wrapper refuses permissive 0644, symlinked, and short-HMAC .env in remote mode (exit 78)
            for bad_file in (perm_env, sym_env, short_env):
                proc = subprocess.run(
                    [
                        "/bin/sh",
                        str(WRAPPER_SH),
                        "/bin/sh",
                        "-c",
                        "echo SHOULD_NOT_RUN",
                        "--remote-access",
                    ],
                    env={**clean_env, "TRADELENS_ENV_FILE": str(bad_file)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 78, f"Expected exit 78 for {bad_file}: {proc.stderr}")
                self.assertNotIn("SHOULD_NOT_RUN", proc.stdout)
                self.assertNotIn(canary_secret, proc.stdout + proc.stderr)
                self.assertNotIn(STRONG_OWNER, proc.stdout + proc.stderr)

            # Shell wrapper succeeds with 0600 valid .env without leaking secret values
            ok_proc = subprocess.run(
                [
                    "/bin/sh",
                    str(WRAPPER_SH),
                    sys.executable,
                    "-c",
                    "import os; print(len(os.environ['TRADELENS_STATE_HMAC_SECRET']))",
                    "--remote-access",
                ],
                env={**clean_env, "TRADELENS_ENV_FILE": str(good_env)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)
            self.assertEqual(ok_proc.stdout.strip(), str(len(canary_secret)))
            self.assertNotIn(canary_secret, ok_proc.stdout + ok_proc.stderr)

    # 5. Restart preserves security config, refuses silent downgrade, and keeps child crash spec
    def test_05_restart_preserves_security_config_and_refuses_silent_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "ops"
            cert = tmp / "tls.crt"
            key = tmp / "tls.key"
            cert.write_text("CERT", encoding="utf-8")
            key.write_text("KEY", encoding="utf-8")
            env_f = tmp / ".env"
            env_f.write_text(f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\n", encoding="utf-8")
            os.chmod(env_f, 0o600)

            seen_specs: List[List[str]] = []

            def fake_startup(self_sup: TradeLensSupervisor) -> Dict[str, Any]:
                for svc in self_sup.specs.values():
                    seen_specs.append(list(svc.command))
                self_sup.running = False
                return {"started": True, "services": list(self_sup.specs.keys())}

            with (
                patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(env_f), "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC}, clear=False),
                patch.object(TradeLensSupervisor, "startup", fake_startup),
            ):
                parser = runtime_cli.build_parser()
                args = parser.parse_args(
                    [
                        "--state-dir",
                        str(state_dir),
                        "supervise",
                        "--bind-host",
                        "100.64.0.10",
                        "--backend-port",
                        "8443",
                        "--frontend-port",
                        "8444",
                        "--vpn-cidr",
                        "100.64.0.0/10",
                        "--allowed-peer-ip",
                        "100.64.0.25",
                        "--allowed-host",
                        "home-mac.tailnet.ts.net",
                        "--allowed-origin",
                        "https://home-mac.tailnet.ts.net:8444",
                        "--tls-cert",
                        str(cert),
                        "--tls-key",
                        str(key),
                        "--remote-access",
                        "--caffeinate",
                    ]
                )
                self.assertEqual(runtime_cli.cmd_supervise(args), 0)

                cfg_path = state_dir / "runtime_config.json"
                self.assertTrue(cfg_path.exists())
                saved_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved_cfg["bind_host"], "100.64.0.10")
                self.assertEqual(saved_cfg["allowed_peer_ip"], ["100.64.0.25"])
                self.assertTrue(saved_cfg["remote_access"])
                self.assertNotIn(STRONG_HMAC, cfg_path.read_text(encoding="utf-8"))

                # Bare restart preserves exact remote + TLS + allowed_peer_ip spec
                seen_specs.clear()
                rst_args = parser.parse_args(["--state-dir", str(state_dir), "restart"])
                self.assertEqual(runtime_cli.cmd_restart(rst_args), 0)
                self.assertEqual(len(seen_specs), 2)
                joined = " ".join(sum(seen_specs, []))
                self.assertIn("100.64.0.10", joined)
                self.assertIn("100.64.0.25", joined)
                self.assertIn(str(cert), joined)

                # Explicit downgrade attempt without --allow-downgrade is refused
                bad_rst = parser.parse_args(["--state-dir", str(state_dir), "restart", "--bind-host", "127.0.0.1"])
                with self.assertRaises(PermissionError):
                    runtime_cli.cmd_restart(bad_rst)

                bad_tls = parser.parse_args(["--state-dir", str(state_dir), "restart", "--allow-insecure-remote-http"])
                with self.assertRaises(PermissionError):
                    runtime_cli.cmd_restart(bad_tls)

            # Crash restart inside TradeLensSupervisor keeps identical SupervisedServiceSpec command & env
            sup_dir = tmp / "sup_crash"
            now_box = [1000.0]
            spawn_cmds: List[List[str]] = []

            class _DeadProc:
                def __init__(self, pid: int) -> None:
                    self.pid = pid

                def poll(self) -> int:
                    return 1

                def terminate(self) -> None:
                    return None

                def kill(self) -> None:
                    return None

                def wait(self, timeout: Optional[float] = None) -> int:
                    return 1

            def fake_spawn(spec: SupervisedServiceSpec) -> _DeadProc:
                spawn_cmds.append(list(spec.command))
                return _DeadProc(61000 + len(spawn_cmds))

            sup = TradeLensSupervisor(
                state_dir=sup_dir,
                services=[
                    SupervisedServiceSpec(
                        name="backend",
                        command=[sys.executable, "-m", "backend.app.main", "--bind-host", "100.64.0.10", "--remote-access"],
                        env={"TRADELENS_ALLOWED_PEER_IPS": "100.64.0.25"},
                    )
                ],
                restart_policy=RestartPolicy(max_restarts=1, window_seconds=60.0, initial_backoff_seconds=1.0),
                power_monitor=PowerAndSleepMonitor(
                    sup_dir / "hb.json",
                    wall_clock=lambda: datetime.fromtimestamp(now_box[0], tz=timezone.utc),
                    mono_clock=lambda: now_box[0],
                ),
                process_spawner=fake_spawn,
                health_probe=lambda _u: (True, "OK"),
                mono_clock=lambda: now_box[0],
                wall_clock=lambda: datetime.fromtimestamp(now_box[0], tz=timezone.utc),
            )
            self.assertTrue(sup.startup()["started"])
            sup.step_once()
            now_box[0] += 3.0
            sup.step_once()
            self.assertEqual(len(spawn_cmds), 2)
            self.assertEqual(spawn_cmds[0], spawn_cmds[1])
            sup.shutdown(owner_initiated=False, reason="TEST_DONE")

    # 6. Browser RAM key loss on reload requires explicit owner re-enrollment approval
    def test_06_browser_ram_key_loss_requires_fresh_owner_approval_no_silent_overwrite(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("false", html)  # non-extractable WebCrypto generateKey
        self.assertIn("namedCurve: 'P-256'", html)
        self.assertIn("digest('SHA-256'", html)
        self.assertIn("order execution is permanently disabled", html)
        self.assertIn("lost on reload", html)
        self.assertIsNone(re.search(r"\bsessionStorage\s*\.", html))
        storage_keys = set(re.findall(r"localStorage\.(?:getItem|setItem)\(\s*['\"]([^'\"]+)['\"]", html))
        self.assertTrue(storage_keys.issubset({"tradelens_theme", "tradelens_positions"}), storage_keys)

        with tempfile.TemporaryDirectory() as td:
            rt, api_handler, _, priv1, _ = self._make_runtime_and_handlers(Path(td))
            rt_mgr = rt.remote_access
            tok1 = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv1, client_ip="100.64.0.25")
            st1, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok1}"},
            )
            self.assertEqual(st1, 200)

            # Browser tab reloads -> loses RAM private key & generates fresh keypair (priv2, pub2)
            priv2, pub2 = DeviceKeyCrypto.generate_keypair()
            chg, _ = rt_mgr.issue_auth_challenge("office-mac-1", "100.64.0.25")
            assert chg is not None
            bad_sig = DeviceKeyCrypto.sign_challenge(priv2, chg["challenge_string"])
            _, dec_bad = rt_mgr.authenticate_remote_session("office-mac-1", chg["challenge_id"], bad_sig, "100.64.0.25")
            self.assertFalse(dec_bad.allowed)
            self.assertEqual(dec_bad.reason_code, "INVALID_DEVICE_SIGNATURE")

            # Pending re-enrollment drops device to PENDING_APPROVAL and revokes old session; cannot auth until owner approves
            pend = rt_mgr.request_device_registration("office-mac-1", "Office Browser", pub2, allowed_ips=["100.64.0.25"])
            self.assertEqual(pend.status, DEVICE_STATUS_PENDING)
            self.assertEqual(pend.re_enrollment_count, 1)

            st_old, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/scan",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok1}"},
            )
            self.assertIn(st_old, (401, 403))

            _, dec_pend = rt_mgr.issue_auth_challenge("office-mac-1", "100.64.0.25")
            self.assertFalse(dec_pend.allowed)

            # Fresh owner approval re-activates new key K2
            rt_mgr.approve_device("office-mac-1", owner_secret=STRONG_OWNER, activate=True)
            tok2 = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv2, client_ip="100.64.0.25")
            self.assertNotEqual(tok1, tok2)

            # Revoked device cannot be re-registered silently unless allow_re_enroll=True
            rt_mgr.revoke_device("office-mac-1", owner_secret=STRONG_OWNER)
            _, pub3 = DeviceKeyCrypto.generate_keypair()
            with self.assertRaises(PermissionError):
                rt_mgr.register_device("office-mac-1", "Office Browser", pub3, owner_secret=STRONG_OWNER, allow_re_enroll=False)

    # 7. Cross-process immediate active-session revocation without server restart
    def test_07_cross_process_active_session_revocation_and_removal_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state" / "remote_auth.json"
            clk = _FakeClock()
            priv_pem, pub_pem = DeviceKeyCrypto.generate_keypair()

            # Manager A: represents long-running API server process
            mgr_server = RemoteAccessManager(
                state_path=state_path,
                owner_secret=STRONG_OWNER,
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.25"],
                state_hmac_secret=STRONG_HMAC,
                remote_mode_enabled=True,
                clock=clk.now_utc,
            )
            mgr_server.register_device("office-xproc", "Office", pub_pem, STRONG_OWNER, allowed_ips=["100.64.0.25"])
            chg, _ = mgr_server.issue_auth_challenge("office-xproc", "100.64.0.25")
            assert chg is not None
            sess, dec = mgr_server.authenticate_remote_session(
                "office-xproc",
                chg["challenge_id"],
                DeviceKeyCrypto.sign_challenge(priv_pem, chg["challenge_string"]),
                "100.64.0.25",
            )
            self.assertTrue(dec.allowed and sess is not None)
            tok = sess["access_token"]

            self.assertTrue(
                mgr_server.authorize_request(
                    "/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {tok}"}
                ).allowed
            )

            # Manager B: separate CLI process revokes device on disk
            mgr_cli = RemoteAccessManager(
                state_path=state_path,
                owner_secret=STRONG_OWNER,
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.25"],
                state_hmac_secret=STRONG_HMAC,
                remote_mode_enabled=True,
                clock=clk.now_utc,
            )
            mgr_cli.revoke_device("office-xproc", owner_secret=STRONG_OWNER, reason="CLI_REVOKE")

            # Immediately rejected in Manager A without restart
            after_rev = mgr_server.authorize_request(
                "/api/scan", "GET", "100.64.0.25", headers={"Authorization": f"Bearer {tok}"}
            )
            self.assertFalse(after_rev.allowed)
            self.assertEqual(after_rev.status_code, 403)

            # Separate CLI process removes device -> Manager A reflects empty device set cleanly
            mgr_cli.remove_device("office-xproc", owner_secret=STRONG_OWNER)
            self.assertEqual(mgr_server.list_devices(), [])

    # 8. Strict TLS X509 SAN mismatch, wrong CA/cert, expired cert, and HTTP non-2xx fail-closed probes
    def test_08_tls_san_mismatch_wrong_ca_expired_cert_and_http_health_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cert_a = tmp / "home.crt"
            key_a = tmp / "home.key"
            cert_other = tmp / "other.crt"
            key_other = tmp / "other.key"

            parser = runtime_cli.build_parser()
            with patch("builtins.print"):
                rc1 = runtime_cli.cmd_generate_tls_cert(
                    parser.parse_args(
                        [
                            "generate-tls-cert",
                            "--bind-host",
                            "127.0.0.1",
                            "--hostname",
                            "home-mac.tailnet.ts.net",
                            "--cert-out",
                            str(cert_a),
                            "--key-out",
                            str(key_a),
                        ]
                    )
                )
                rc2 = runtime_cli.cmd_generate_tls_cert(
                    parser.parse_args(
                        [
                            "generate-tls-cert",
                            "--bind-host",
                            "127.0.0.1",
                            "--hostname",
                            "other.tailnet.ts.net",
                            "--cert-out",
                            str(cert_other),
                            "--key-out",
                            str(key_other),
                        ]
                    )
                )
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)

            def _handshake(srv_cert: Path, srv_key: Path, ca_file: Path, server_hostname: str) -> bytes:
                srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                srv_ctx.load_cert_chain(str(srv_cert), str(srv_key))
                cli_ctx = ssl.create_default_context(cafile=str(ca_file))
                a, b = socket.socketpair()
                a.settimeout(2.0)
                b.settimeout(2.0)

                def _srv() -> None:
                    try:
                        with srv_ctx.wrap_socket(a, server_side=True) as ss:
                            ss.recv(4)
                            ss.sendall(b"pong")
                    except Exception:
                        pass
                    finally:
                        a.close()

                th = threading.Thread(target=_srv, daemon=True)
                th.start()
                try:
                    with cli_ctx.wrap_socket(b, server_hostname=server_hostname) as cs:
                        cs.sendall(b"ping")
                        return cs.recv(4)
                finally:
                    b.close()
                    th.join(timeout=2.0)

            # Matching SAN + matching pinned cert succeeds under OpenSSL VERIFY_X509_STRICT
            self.assertEqual(_handshake(cert_a, key_a, cert_a, "home-mac.tailnet.ts.net"), b"pong")
            # Hostname SAN mismatch fails closed
            with self.assertRaises(ssl.SSLCertVerificationError):
                _handshake(cert_a, key_a, cert_a, "rogue-host.tailnet.ts.net")
            # Wrong CA/pinned cert fails closed
            with self.assertRaises(ssl.SSLCertVerificationError):
                _handshake(cert_a, key_a, cert_other, "home-mac.tailnet.ts.net")

            # Generate an expired leaf cert under a distinct CA & verify strict TLS rejects it
            openssl = "/usr/bin/openssl"
            (tmp / "index.txt").write_text("", encoding="utf-8")
            (tmp / "serial").write_text("01\n", encoding="utf-8")
            ca_cnf = tmp / "ca.cnf"
            ca_cnf.write_text(
                f"[ca]\ndefault_ca=CA_default\n[CA_default]\ndatabase={tmp}/index.txt\nserial={tmp}/serial\n"
                f"new_certs_dir={tmp}\ndefault_md=sha256\npolicy=policy_any\nunique_subject=no\nx509_extensions=v3_leaf\n"
                "[policy_any]\ncommonName=supplied\n[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\nprompt=no\n"
                "[dn]\nCN=TradeLens Test Root CA\n[v3_ca]\nbasicConstraints=critical,CA:TRUE,pathlen:0\n"
                "keyUsage=critical,keyCertSign,cRLSign\n"
                "[v3_leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:home-mac.tailnet.ts.net\n",
                encoding="utf-8",
            )
            ca_key, ca_crt = tmp / "ca.key", tmp / "ca.crt"
            leaf_key, leaf_csr, leaf_exp = tmp / "leaf.key", tmp / "leaf.csr", tmp / "leaf_exp.crt"
            subprocess.check_call(
                [openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-pkeyopt", "ec_param_enc:named_curve", "-out", str(ca_key)],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.check_call(
                [openssl, "req", "-new", "-x509", "-sha256", "-key", str(ca_key), "-out", str(ca_crt), "-days", "30", "-config", str(ca_cnf)],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.check_call(
                [openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-pkeyopt", "ec_param_enc:named_curve", "-out", str(leaf_key)],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.check_call(
                [openssl, "req", "-new", "-sha256", "-key", str(leaf_key), "-out", str(leaf_csr), "-subj", "/CN=home-mac.tailnet.ts.net"],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.check_call(
                [openssl, "ca", "-batch", "-config", str(ca_cnf), "-cert", str(ca_crt), "-keyfile", str(ca_key), "-in", str(leaf_csr), "-out", str(leaf_exp), "-startdate", "20200101000000Z", "-enddate", "20200102000000Z"],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with self.assertRaises(ssl.SSLCertVerificationError) as exp_ctx:
                _handshake(leaf_exp, leaf_key, ca_crt, "home-mac.tailnet.ts.net")
            self.assertIn("expired", str(exp_ctx.exception).lower())

            # Health probe only accepts HTTP 2xx; 401/403/404/500 fail closed + exception redacts secret
            class _FakeResp:
                def __init__(self, status: int) -> None:
                    self.status = status

                def __enter__(self) -> "_FakeResp":
                    return self

                def __exit__(self, *_a: Any) -> bool:
                    return False

            for bad_code in (401, 403, 404, 500):
                with patch("urllib.request.urlopen", return_value=_FakeResp(bad_code)):
                    ok, detail = default_http_health_probe("https://100.64.0.10:8000/api/health", probe_secret="SECRET_PROBE_999")
                    self.assertFalse(ok)
                    self.assertEqual(detail, f"HTTP_{bad_code}")

            with patch("urllib.request.urlopen", side_effect=RuntimeError("boom SECRET_PROBE_999 leaked")):
                ok, detail = default_http_health_probe("https://100.64.0.10:8000/api/health", probe_secret="SECRET_PROBE_999")
                self.assertFalse(ok)
                self.assertNotIn("SECRET_PROBE_999", detail)

    # 9. Remote bearer session strictly read-only + zero indirect broker connect() calls
    def test_09_remote_mutations_and_indirect_broker_login_strictly_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            prov = _DummyProvider()
            prov.is_authenticated = False
            prov.jwt_token = ""
            prov.feed_token = ""
            rt, api_handler, _, priv_pem, _ = self._make_runtime_and_handlers(Path(td), provider=prov)
            tok = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv_pem, client_ip="100.64.0.25")
            rem_hdrs = {"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok}"}

            # Forbidden mutations & control routes remotely -> 403
            for method, path, body in (
                ("POST", "/api/ledger/trade", {"symbol": "INFY"}),
                ("POST", "/api/ledger/trade/delete", {"id": "1"}),
                ("POST", "/api/ledger/clear", {}),
                ("POST", "/api/ledger/settings", {"capital": 500000}),
                ("POST", "/api/ledger/import-chat", {"text": "BUY"}),
                ("POST", "/api/broker/authenticate", {}),
                ("POST", "/api/runtime/shutdown", {"reason": "remote"}),
                ("GET", "/api/remote/devices", None),
                ("POST", "/api/remote/devices/register", {"device_id": "x"}),
                ("POST", "/api/remote/devices/approve", {"device_id": "x"}),
                ("POST", "/api/remote/devices/revoke", {"device_id": "office-mac-1"}),
                ("POST", "/api/remote/devices/remove", {"device_id": "office-mac-1"}),
            ):
                st, b, _ = _dispatch_http(api_handler, method, path, client_ip="100.64.0.25", headers=rem_hdrs, body=body)
                self.assertEqual(st, 403, f"Expected 403 on remote {method} {path}, got {st} ({b})")

            # Indirect broker login test on AngelOneSmartAPIProvider with _allow_reconnect=False
            with patch.dict(os.environ, {"ANGEL_DRY_RUN_NO_LOGIN": "1"}, clear=False):
                real_prov = AngelOneSmartAPIProvider(
                    api_key="AK", client_code="CC", password="PW", totp_secret="JBSWY3DPEHPK3PXP"
                )
            real_prov.jwt_token = ""
            real_prov.is_authenticated = False
            connect_calls = 0

            def _count_connect() -> bool:
                nonlocal connect_calls
                connect_calls += 1
                return True

            real_prov.connect = _count_connect  # type: ignore[method-assign]
            rt2, api2, _, priv2, _ = self._make_runtime_and_handlers(Path(td) / "rt2", provider=real_prov)
            tok2 = self._authenticate_http(api2, device_id="office-mac-1", priv_pem=priv2, client_ip="100.64.0.25")
            for ep in ("/api/scan", "/api/indices", "/api/positions"):
                _dispatch_http(
                    api2,
                    "GET",
                    ep,
                    client_ip="100.64.0.25",
                    headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok2}"},
                )
            self.assertEqual(connect_calls, 0, "Remote passive endpoints must NEVER trigger broker connect()")
            self.assertEqual(prov.connect_calls, 0)

    # 10. Order execution permanently disabled in code, config, health telemetry, and routes
    def test_10_order_execution_permanently_disabled_across_code_config_and_api(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rt, api_handler, _, priv_pem, _ = self._make_runtime_and_handlers(Path(td))
            with patch.dict(
                os.environ,
                {"TRADELENS_ORDER_EXECUTION_ENABLED": "true", "ORDER_EXECUTION_ENABLED": "1"},
                clear=False,
            ):
                rt_env = TradeLensAppRuntime(
                    state_dir=Path(td) / "env_rt",
                    provider=_DummyProvider(),
                    ledger_path=Path(td) / "env_rt" / "ledger.json",
                    environ=dict(os.environ),
                )
                self.assertFalse(rt_env.order_execution_enabled)
                health = rt_env.get_health_payload()
                self.assertFalse(health["order_execution_enabled"])
                self.assertFalse(health["signals"]["remote_client_connectivity"]["order_execution_enabled"])
                self.assertFalse(health["proves_live_market_data"])
                with self.assertRaises(AttributeError):
                    rt_env.order_execution_enabled = True  # type: ignore[misc]

            self.assertTrue(is_forbidden_order_execution_path("/api/orders/place"))
            self.assertTrue(is_forbidden_order_execution_path("/api/trade/execute"))

            tok = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv_pem, client_ip="100.64.0.25")
            for flag in ("execute_order", "place_order", "order_execution_enabled"):
                st_r, b_r, _ = _dispatch_http(
                    api_handler,
                    "POST",
                    "/api/remote/heartbeat",
                    client_ip="100.64.0.25",
                    headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok}"},
                    body={flag: True},
                )
                self.assertEqual(st_r, 403)
                self.assertEqual(b_r["reason_code"], "ORDER_EXECUTION_DISABLED")

                st_l, b_l, _ = _dispatch_http(
                    api_handler,
                    "POST",
                    "/api/positions/monitor",
                    client_ip="127.0.0.1",
                    body={flag: True},
                )
                self.assertEqual(st_l, 403)
                self.assertEqual(b_l["reason_code"], "ORDER_EXECUTION_DISABLED")

            # Static AST inspection across backend provider & runtime modules
            forbidden_names = {"placeOrder", "place_order", "modifyOrder", "cancelOrder", "gttCreateRule", "execute_trade"}
            for src_path in [
                REPO_ROOT / "backend" / "app" / "main.py",
                REPO_ROOT / "backend" / "app" / "data" / "angel_one.py",
                *sorted((REPO_ROOT / "backend" / "app" / "runtime").glob("*.py")),
            ]:
                tree = ast.parse(src_path.read_text(encoding="utf-8"))
                defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                overlap = forbidden_names & defined
                self.assertFalse(overlap, f"Forbidden order method(s) {overlap} found in {src_path}")

    # 11. owner_shutdown.flag vs crash restart + resume keeps security config intact
    def test_11_owner_shutdown_flag_vs_crash_restart_and_resume_preserves_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "ops_state"
            env_f = tmp / ".env"
            env_f.write_text(f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\n", encoding="utf-8")
            os.chmod(env_f, 0o600)
            parser = runtime_cli.build_parser()

            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(env_f), "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC}, clear=False):
                def _initial_start(self_sup: TradeLensSupervisor) -> Dict[str, Any]:
                    self_sup.running = False
                    return {"started": True}

                # Save secured remote runtime_config.json first
                with patch.object(TradeLensSupervisor, "startup", _initial_start):
                    runtime_cli.cmd_supervise(
                        parser.parse_args(
                            [
                                "--state-dir",
                                str(state_dir),
                                "supervise",
                                "--bind-host",
                                "100.64.0.10",
                                "--vpn-cidr",
                                "100.64.0.0/10",
                                "--allowed-peer-ip",
                                "100.64.0.25",
                                "--tls-cert",
                                str(tmp / "c.crt"),
                                "--tls-key",
                                str(tmp / "k.key"),
                                "--remote-access",
                            ]
                        )
                    )

                runtime_cli.cmd_owner_shutdown(parser.parse_args(["--state-dir", str(state_dir), "owner-shutdown", "--reason", "TEST_LOCK"]))
                self.assertTrue((state_dir / "owner_shutdown.flag").exists())

                # Both supervise and restart refuse to run while owner_shutdown.flag exists
                sup_out = io.StringIO()
                with contextlib.redirect_stdout(sup_out):
                    rc_sup = runtime_cli.cmd_supervise(
                        parser.parse_args(["--state-dir", str(state_dir), "supervise", "--bind-host", "100.64.0.10", "--remote-access", "--allow-insecure-remote-http"])
                    )
                self.assertEqual(rc_sup, 0)
                self.assertEqual(json.loads(sup_out.getvalue())["reason"], "OWNER_SHUTDOWN_FLAG_ACTIVE")

                rst_out = io.StringIO()
                with contextlib.redirect_stdout(rst_out):
                    rc_rst = runtime_cli.cmd_restart(parser.parse_args(["--state-dir", str(state_dir), "restart"]))
                self.assertEqual(rc_rst, 0)
                self.assertFalse(json.loads(rst_out.getvalue())["restarted"])

                # Resume clears flag and preserves runtime_config.json untouched
                runtime_cli.cmd_resume(parser.parse_args(["--state-dir", str(state_dir), "resume"]))
                self.assertFalse((state_dir / "owner_shutdown.flag").exists())
                cfg = json.loads((state_dir / "runtime_config.json").read_text(encoding="utf-8"))
                self.assertEqual(cfg["bind_host"], "100.64.0.10")
                self.assertEqual(cfg["allowed_peer_ip"], ["100.64.0.25"])
                self.assertTrue(cfg["remote_access"])

    # 12. Comprehensive secret redaction & --owner-secret-stdin
    def test_12_comprehensive_secret_redaction_and_owner_secret_stdin(self) -> None:
        canaries = [
            _DummyProvider.api_key,
            _DummyProvider.client_code,
            _DummyProvider.password,
            "JWT_CANARY_TOKEN_1122",
            "FEED_CANARY_TOKEN_3344",
            STRONG_OWNER,
            STRONG_HMAC,
            "SUP_PROBE_CANARY_5566",
        ]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "redaction_state"
            _, pub_pem = DeviceKeyCrypto.generate_keypair()
            pub_file = tmp / "office.pub"
            pub_file.write_text(pub_pem, encoding="utf-8")

            env = {
                "ANGEL_API_KEY": _DummyProvider.api_key,
                "ANGEL_CLIENT_CODE": _DummyProvider.client_code,
                "ANGEL_PASSWORD": _DummyProvider.password,
                "ANGEL_TOTP_SECRET": _DummyProvider.totp_secret,
                "TRADELENS_OWNER_SECRET": STRONG_OWNER,
                "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC,
                "TRADELENS_SUPERVISOR_PROBE_SECRET": "SUP_PROBE_CANARY_5566",
            }
            parser = runtime_cli.build_parser()

            # Register device using --owner-secret-stdin -> no CLI arg warning & no secret printed
            reg_out = io.StringIO()
            with (
                patch.dict(os.environ, env, clear=False),
                patch("sys.stdin", io.StringIO(STRONG_OWNER + "\n")),
                contextlib.redirect_stdout(reg_out),
            ):
                rc = runtime_cli.cmd_register_device(
                    parser.parse_args(
                        [
                            "--state-dir",
                            str(state_dir),
                            "register-device",
                            "--device-id",
                            "office-stdin-1",
                            "--public-key-file",
                            str(pub_file),
                            "--allowed-ip",
                            "100.64.0.25",
                            "--owner-secret-stdin",
                            "--remote-access",
                        ]
                    )
                )
            self.assertEqual(rc, 0)
            reg_json = json.loads(reg_out.getvalue())
            self.assertNotIn("security_warning", reg_json)
            self.assertEqual(reg_json["allowed_ips"], ["100.64.0.25"])

            with patch.dict(os.environ, env, clear=False):
                # Supervisor status, log, rendered launchd plists, and API health/error responses contain zero canaries
                sup = TradeLensSupervisor(
                    state_dir=state_dir,
                    services=[
                        SupervisedServiceSpec(
                            name="backend",
                            command=[sys.executable, "-c", "pass"],
                            env=dict(env),
                        )
                    ],
                    power_monitor=PowerAndSleepMonitor(state_dir / "hb.json"),
                )
                sup.logger.error(
                    "Simulated crash %s %s %s %s",
                    _DummyProvider.api_key,
                    STRONG_OWNER,
                    STRONG_HMAC,
                    "SUP_PROBE_CANARY_5566",
                )
                sup.write_status()

            plist_dir = tmp / "plists"
            render_launchd_plists(
                plist_dir,
                REPO_ROOT,
                bind_host="100.64.0.10",
                vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.25"],
                tls_cert_file=tmp / "c.crt",
                tls_key_file=tmp / "k.key",
                remote_access_enabled=True,
            )
            for p_file in plist_dir.glob("*.plist"):
                subprocess.check_call(["plutil", "-lint", str(p_file)], stdout=subprocess.DEVNULL)

            rt, api_handler, _, _, _ = self._make_runtime_and_handlers(tmp / "rt_leak")
            _, b_err, _ = _dispatch_http(
                api_handler,
                "POST",
                "/api/remote/devices/revoke",
                client_ip="127.0.0.1",
                headers={"X-TradeLens-Owner-Secret": STRONG_OWNER},
                body={"device_id": f"missing_{_DummyProvider.api_key}_{STRONG_HMAC}"},
            )
            _, b_health, _ = _dispatch_http(api_handler, "GET", "/api/health", client_ip="127.0.0.1")
            _, b_status, _ = _dispatch_http(api_handler, "GET", "/api/runtime/status", client_ip="127.0.0.1")

            corpus = "\n".join(
                [
                    reg_out.getvalue(),
                    (state_dir / "supervisor.log").read_text(encoding="utf-8"),
                    (state_dir / "supervisor_status.json").read_text(encoding="utf-8"),
                    (state_dir / "remote_auth.json").read_text(encoding="utf-8"),
                    *[pf.read_text(encoding="utf-8") for pf in plist_dir.glob("*.plist")],
                    json.dumps(b_err),
                    json.dumps(b_health),
                    json.dumps(b_status),
                ]
            )
            for secret in canaries:
                self.assertNotIn(secret, corpus, f"Secret leaked in artifacts/outputs: {secret}")

    # 13. LaunchAgent wrapper vs Python .env parser parity & fail-closed syntax/permission hardening
    def test_13_dotenv_parser_parity_and_no_secret_leak_between_python_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            valid_env = tmp / "valid_complex.env"
            # BOM + CRLF + indented comments + export + single/double quotes + special chars + inline comments + duplicate key
            raw_content = (
                "\ufeff"
                "# Leading comment\r\n"
                "   # Indented comment with KEY=fake\r\n"
                "\r\n"
                "  export   TRADELENS_REMOTE_ACCESS_ENABLED = 1\r\n"
                "TRADELENS_STATE_HMAC_SECRET = 'HMAC$v!#1`sq;a|b&c=d+e/f==' # trailing inline comment\r\n"
                'DOUBLE_Q="alpha \\"quoted\\" \\\\slash #kept"   # trailing dq comment\r\n'
                "UNQ_VAL =   hello#fragment   # inline comment after whitespace\r\n"
                "EMPTY_VAL=\r\n"
                "DUP_VAR=first_val\r\n"
                "DUP_VAR=second_val\r\n"
            )
            valid_env.write_bytes(raw_content.encode("utf-8"))
            os.chmod(valid_env, 0o600)

            py_parsed = parse_dotenv_content(valid_env.read_text(encoding="utf-8"), strict_syntax=True)
            py_read_dict = read_env_dict(base_env={}, env_path=valid_env, require_strict_perms=True)
            self.assertEqual(py_parsed["TRADELENS_REMOTE_ACCESS_ENABLED"], "1")
            self.assertEqual(py_parsed["TRADELENS_STATE_HMAC_SECRET"], "HMAC$v!#1`sq;a|b&c=d+e/f==")
            self.assertEqual(py_parsed["DOUBLE_Q"], 'alpha "quoted" \\slash #kept')
            self.assertEqual(py_parsed["UNQ_VAL"], "hello#fragment")
            self.assertEqual(py_parsed["EMPTY_VAL"], "")
            self.assertEqual(py_parsed["DUP_VAR"], "second_val")
            for k, v in py_parsed.items():
                self.assertEqual(py_read_dict.get(k), v)

            clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "SHELLOPTS": "xtrace"}
            keys_to_dump = list(py_parsed.keys())
            sh_proc = subprocess.run(
                [
                    "/bin/sh",
                    str(WRAPPER_SH),
                    sys.executable,
                    "-c",
                    f"import json, os; print(json.dumps({{k: os.environ.get(k) for k in {keys_to_dump!r}}}))",
                ],
                env={**clean_env, "TRADELENS_ENV_FILE": str(valid_env)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(sh_proc.returncode, 0, sh_proc.stderr)
            sh_parsed = json.loads(sh_proc.stdout)
            self.assertEqual(sh_parsed, py_parsed)
            # Zero secret leakage in wrapper stderr (even with xtrace requested in parent SHELLOPTS)
            self.assertNotIn("HMAC$v!#1`sq", sh_proc.stderr)
            self.assertNotIn("quoted", sh_proc.stderr)

            # Malformed lines fail closed identically in BOTH Python and shell wrapper without exposing values
            malformed_cases = [
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nMISSING_EQUALS_LINE\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\n1BAD_ID=SECRET_CANARY_XYZ\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nBAD-ID=SECRET_CANARY_XYZ\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nUNCLOSED_DQ=\"SECRET_CANARY_XYZ\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nUNCLOSED_SQ='SECRET_CANARY_XYZ\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nBAD_TRAIL=\"SECRET_CANARY_XYZ\" unexpected_tok\n",
                f"TRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nSTRAY_Q=ab\"SECRET_CANARY_XYZ\n",
            ]
            for idx, bad_text in enumerate(malformed_cases):
                bad_f = tmp / f"bad_{idx}.env"
                bad_f.write_text(bad_text, encoding="utf-8")
                os.chmod(bad_f, 0o600)
                with self.assertRaises(ValueError) as py_err:
                    parse_dotenv_content(bad_text, strict_syntax=True)
                self.assertNotIn("SECRET_CANARY_XYZ", str(py_err.exception))

                bad_sh = subprocess.run(
                    ["/bin/sh", str(WRAPPER_SH), "/bin/echo", "SHOULD_NOT_RUN"],
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "TRADELENS_ENV_FILE": str(bad_f)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(bad_sh.returncode, 78, bad_sh.stderr)
                self.assertNotIn("SHOULD_NOT_RUN", bad_sh.stdout)
                self.assertNotIn("SECRET_CANARY_XYZ", bad_sh.stdout + bad_sh.stderr)

            # 0644 .env that itself sets TRADELENS_REMOTE_ACCESS_ENABLED=1 fails closed even WITHOUT --remote-access flag!
            perm_remote = tmp / "perm_remote.env"
            perm_remote.write_text(
                f"TRADELENS_REMOTE_ACCESS_ENABLED=1\nTRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\n",
                encoding="utf-8",
            )
            os.chmod(perm_remote, 0o644)
            perm_sh = subprocess.run(
                ["/bin/sh", str(WRAPPER_SH), "/bin/echo", "SHOULD_NOT_RUN"],
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp), "TRADELENS_ENV_FILE": str(perm_remote)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(perm_sh.returncode, 78)
            self.assertNotIn(STRONG_HMAC, perm_sh.stdout + perm_sh.stderr)
            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(perm_remote)}, clear=True):
                with self.assertRaises(PermissionError):
                    runtime_cli._open_remote_manager(
                        runtime_cli.build_parser().parse_args(["--state-dir", str(tmp / "sd"), "list-devices"])
                    )

    # 14. Owner secret prompt/stdin hardening + init-owner & end-to-end CLI device lifecycle
    def test_14_owner_secret_input_paths_init_owner_and_cli_device_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "cli_state"
            env_f = tmp / ".env"
            env_f.write_text(
                f"TRADELENS_REMOTE_ACCESS_ENABLED=1\nTRADELENS_STATE_HMAC_SECRET={STRONG_HMAC}\nTRADELENS_ALLOWED_PEER_IPS=100.64.0.25\n",
                encoding="utf-8",
            )
            os.chmod(env_f, 0o600)
            parser = runtime_cli.build_parser()

            # Deliberately leave TRADELENS_OWNER_SECRET OUT of .env and OUT of os.environ!
            with patch.dict(os.environ, {"TRADELENS_ENV_FILE": str(env_f)}, clear=True):
                # 1. init-owner via --owner-secret-prompt (no env var, no shell arg, confirm mismatch rejects)
                with patch("getpass.getpass", side_effect=[STRONG_OWNER, "WrongConfirm"]):
                    with self.assertRaises(ValueError):
                        runtime_cli.cmd_init_owner(
                            parser.parse_args(["--state-dir", str(state_dir), "init-owner", "--owner-secret-prompt", "--remote-access"])
                        )

                init_out = io.StringIO()
                with (
                    patch("getpass.getpass", side_effect=[STRONG_OWNER, STRONG_OWNER]),
                    contextlib.redirect_stdout(init_out),
                ):
                    rc = runtime_cli.cmd_init_owner(
                        parser.parse_args(["--state-dir", str(state_dir), "init-owner", "--owner-secret-prompt", "--remote-access"])
                    )
                self.assertEqual(rc, 0)
                init_json = json.loads(init_out.getvalue())
                self.assertEqual(init_json["status"], "OWNER_SECRET_INITIALIZED")
                self.assertFalse(init_json["stored_plaintext_in_env"])
                self.assertNotIn(STRONG_OWNER, init_out.getvalue())

                # 2. --owner-secret-stdin MUST fail closed on empty stdin (never silently fall back to .env/env!)
                with patch.dict(os.environ, {"TRADELENS_OWNER_SECRET": STRONG_OWNER}, clear=False):
                    with patch("sys.stdin", io.StringIO("\n")):
                        with self.assertRaises(ValueError):
                            runtime_cli.cmd_revoke_device(
                                parser.parse_args(
                                    ["--state-dir", str(state_dir), "revoke-device", "--device-id", "office-mac-1", "--owner-secret-stdin", "--remote-access"]
                                )
                            )

                # 3. generate-device-key + register-device --owner-secret-prompt + list-devices end-to-end
                pub_path = tmp / "office-mac-1.pub"
                gk_out = io.StringIO()
                with contextlib.redirect_stdout(gk_out):
                    runtime_cli.cmd_generate_device_key(
                        parser.parse_args(["generate-device-key", "--device-id", "office-mac-1", "--public-key-out", str(pub_path)])
                    )
                self.assertTrue(pub_path.exists())

                reg_out = io.StringIO()
                with patch("getpass.getpass", return_value=STRONG_OWNER), contextlib.redirect_stdout(reg_out):
                    rc_reg = runtime_cli.cmd_register_device(
                        parser.parse_args(
                            [
                                "--state-dir",
                                str(state_dir),
                                "register-device",
                                "--device-id",
                                "office-mac-1",
                                "--device-name",
                                "Office Mac",
                                "--public-key-file",
                                str(pub_path),
                                "--allowed-ip",
                                "100.64.0.25",
                                "--owner-secret-prompt",
                                "--remote-access",
                            ]
                        )
                    )
                self.assertEqual(rc_reg, 0)
                reg_json = json.loads(reg_out.getvalue())
                self.assertEqual(reg_json["device_status"], DEVICE_STATUS_ACTIVE)
                self.assertEqual(reg_json["allowed_ips"], ["100.64.0.25"])

                # Wrong owner secret fails uniformly on revoke-device
                with patch("getpass.getpass", return_value="WrongOwnerPass!99"):
                    with self.assertRaises(PermissionError):
                        runtime_cli.cmd_revoke_device(
                            parser.parse_args(
                                ["--state-dir", str(state_dir), "revoke-device", "--device-id", "office-mac-1", "--owner-secret-prompt", "--remote-access"]
                            )
                        )

                # List devices verifies persisted allowed_ips
                ls_out = io.StringIO()
                with contextlib.redirect_stdout(ls_out):
                    runtime_cli.cmd_list_devices(
                        parser.parse_args(["--state-dir", str(state_dir), "list-devices", "--remote-access"])
                    )
                ls_json = json.loads(ls_out.getvalue())
                self.assertEqual(ls_json["devices"][0]["allowed_ips"], ["100.64.0.25"])

                # 4. Every Stage B / runtime documented CLI command & flag parses cleanly via build_parser()
                documented_cli_invocations = [
                    ["--state-dir", str(state_dir), "init-owner", "--owner-secret-prompt", "--remote-access"],
                    ["generate-tls-cert", "--bind-host", "100.64.0.10", "--hostname", "home-mac.tailnet.ts.net", "--cert-out", str(tmp / "server.crt"), "--key-out", str(tmp / "server.key")],
                    ["generate-device-key", "--device-id", "office-mac-1", "--store-keychain", "--public-key-out", str(pub_path)],
                    ["--state-dir", str(state_dir), "register-device", "--device-id", "office-mac-1", "--device-name", "Office Mac", "--public-key-file", str(pub_path), "--allowed-ip", "100.64.0.25", "--owner-secret-prompt", "--remote-access"],
                    ["--state-dir", str(state_dir), "approve-device", "--device-id", "office-mac-1", "--allowed-ip", "100.64.0.25", "--owner-secret-prompt", "--remote-access"],
                    ["--state-dir", str(state_dir), "update-device-ip", "--device-id", "office-mac-1", "--allowed-ip", "100.64.0.31", "--owner-secret-prompt", "--remote-access"],
                    ["--state-dir", str(state_dir), "revoke-device", "--device-id", "office-mac-1", "--reason", "test", "--owner-secret-prompt", "--remote-access"],
                    ["--state-dir", str(state_dir), "remove-device", "--device-id", "office-mac-1", "--reason", "test", "--owner-secret-stdin", "--remote-access"],
                    ["--state-dir", str(state_dir), "list-devices", "--remote-access"],
                    ["sign-challenge", "--device-id", "office-mac-1", "--challenge-stdin"],
                    ["delete-device-key", "--device-id", "office-mac-1"],
                    ["broker-auth", "--owner-secret-prompt"],
                    ["serve-frontend", "--bind-host", "100.64.0.10", "--vpn-cidr", "100.64.0.0/10", "--allowed-peer-ip", "100.64.0.25", "--allowed-host", "100.64.0.10", "--allowed-origin", "https://100.64.0.10:8080", "--tls-cert", "/tmp/cert.pem", "--tls-key", "/tmp/key.pem"],
                    ["render-launchd", "--bind-host", "100.64.0.10", "--vpn-cidr", "100.64.0.0/10", "--allowed-peer-ip", "100.64.0.25", "--allowed-host", "100.64.0.10", "--allowed-origin", "https://100.64.0.10:8080", "--tls-cert", "/tmp/cert.pem", "--tls-key", "/tmp/key.pem", "--remote-access"],
                    ["supervise", "--bind-host", "100.64.0.10", "--vpn-cidr", "100.64.0.0/10", "--allowed-peer-ip", "100.64.0.25", "--tls-cert", "/tmp/cert.pem", "--tls-key", "/tmp/key.pem"],
                    ["health-check"],
                    ["status"],
                    ["owner-shutdown", "--reason", "owner_stop"],
                    ["resume"],
                ]
                for argv in documented_cli_invocations:
                    parsed_cmd = parser.parse_args(argv)
                    self.assertTrue(callable(parsed_cmd.func), msg=f"CLI command missing handler for {argv}")

    # 15. Exact-IP pinning operational recovery plan via update-device-ip
    def test_15_exact_ip_pinning_recovery_invalidates_sessions_and_blocks_old_ip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "ip_recovery_state"
            priv_pem, pub_pem = DeviceKeyCrypto.generate_keypair()

            # Home Mac initially pins Office Mac to 100.64.0.25 (env global allowlist includes both verified IPs after Step 3 or updated handler)
            mgr1 = RemoteAccessManager(
                state_dir / "remote_auth.json",
                owner_secret=STRONG_OWNER,
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.25"],
                allowed_hosts=["100.64.0.10"],
                allowed_origins=["https://100.64.0.10:8080"],
                state_hmac_secret=STRONG_HMAC,
                remote_mode_enabled=True,
            )
            mgr1.register_device(
                device_id="office-mac-1",
                device_name="Office Mac",
                public_key_pem=pub_pem,
                owner_secret=STRONG_OWNER,
                approve=True,
                allowed_ips=["100.64.0.25"],
            )
            rt = TradeLensAppRuntime(
                state_dir=state_dir,
                provider=_DummyProvider(),
                ledger_path=tmp / "ledger.json",
                remote_access=mgr1,
                environ={"TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC},
            )
            api_handler = make_api_handler(rt)

            # Active session on original IP 100.64.0.25 works
            tok_old = self._authenticate_http(api_handler, device_id="office-mac-1", priv_pem=priv_pem, client_ip="100.64.0.25")
            st_ok, _, _ = _dispatch_http(
                api_handler,
                "GET",
                "/api/indices",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok_old}"},
            )
            self.assertEqual(st_ok, 200)

            # New IP 100.64.0.31 is NEVER auto-accepted even with matching device_id
            st_rej, b_rej, _ = _dispatch_http(
                api_handler,
                "POST",
                "/api/remote/challenge",
                client_ip="100.64.0.31",
                headers={"Host": "100.64.0.10:8000"},
                body={"device_id": "office-mac-1"},
            )
            self.assertEqual(st_rej, 403)
            self.assertEqual(b_rej["reason_code"], "UNAPPROVED_TAILSCALE_PEER")

            # Owner runs CLI update-device-ip recovery -> updates device IP to 100.64.0.31 AND revokes tok_old immediately
            parser = runtime_cli.build_parser()
            with patch.dict(os.environ, {"TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC, "TRADELENS_ALLOWED_PEER_IPS": "100.64.0.31"}, clear=False):
                # Reject broad CIDR
                with patch("getpass.getpass", return_value=STRONG_OWNER):
                    with self.assertRaises(ValueError):
                        runtime_cli.cmd_update_device_ip(
                            parser.parse_args(
                                ["--state-dir", str(state_dir), "update-device-ip", "--device-id", "office-mac-1", "--allowed-ip", "100.64.0.0/10", "--owner-secret-prompt", "--remote-access"]
                            )
                        )
                upd_out = io.StringIO()
                with patch("getpass.getpass", return_value=STRONG_OWNER), contextlib.redirect_stdout(upd_out):
                    rc_upd = runtime_cli.cmd_update_device_ip(
                        parser.parse_args(
                            ["--state-dir", str(state_dir), "update-device-ip", "--device-id", "office-mac-1", "--allowed-ip", "100.64.0.31", "--owner-secret-prompt", "--remote-access"]
                        )
                    )
                self.assertEqual(rc_upd, 0)
                upd_json = json.loads(upd_out.getvalue())
                self.assertEqual(upd_json["allowed_ips"], ["100.64.0.31"])
                self.assertEqual(upd_json["revoked_sessions_count"], 1)

            # Simulate updated runtime config with global TRADELENS_ALLOWED_PEER_IPS=100.64.0.31 sharing state file
            mgr2 = RemoteAccessManager(
                state_dir / "remote_auth.json",
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.31"],
                allowed_hosts=["100.64.0.10"],
                allowed_origins=["https://100.64.0.10:8080"],
                state_hmac_secret=STRONG_HMAC,
                remote_mode_enabled=True,
            )
            rt2 = TradeLensAppRuntime(
                state_dir=state_dir,
                provider=_DummyProvider(),
                ledger_path=tmp / "ledger.json",
                remote_access=mgr2,
                environ={"TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC},
            )
            handler2 = make_api_handler(rt2)

            # Old token invalidated even if presented from new IP, and old IP rejected 403
            st_replay, b_replay, _ = _dispatch_http(
                handler2,
                "GET",
                "/api/indices",
                client_ip="100.64.0.31",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok_old}"},
            )
            self.assertEqual(st_replay, 401)
            self.assertEqual(b_replay["reason_code"], "SESSION_REVOKED")

            st_old_ip, b_old_ip, _ = _dispatch_http(
                handler2,
                "POST",
                "/api/remote/challenge",
                client_ip="100.64.0.25",
                headers={"Host": "100.64.0.10:8000"},
                body={"device_id": "office-mac-1"},
            )
            self.assertEqual(st_old_ip, 403)
            self.assertEqual(b_old_ip["reason_code"], "UNAPPROVED_TAILSCALE_PEER")

            # Re-authentication from verified new IP succeeds
            tok_new = self._authenticate_http(handler2, device_id="office-mac-1", priv_pem=priv_pem, client_ip="100.64.0.31")
            st_new_ok, _, _ = _dispatch_http(
                handler2,
                "GET",
                "/api/indices",
                client_ip="100.64.0.31",
                headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {tok_new}"},
            )
            self.assertEqual(st_new_ok, 200)

    # 16. Frontend Keychain CLI vs RAM Key path clarity, serve-frontend env fallback, and broker-auth loopback guard
    def test_16_frontend_auth_path_clarity_env_fallback_and_broker_auth_loopback_guard(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="remoteChallengeRaw"', html)
        self.assertIn("Path A (Persistent Keychain CLI)", html)
        self.assertIn("Path B (Ephemeral Browser RAM Key)", html)
        self.assertIn("do not reuse the same Device ID across both paths", html)

        parser = runtime_cli.build_parser()
        # _cmd_serve_frontend without --allowed-peer-ip preserves TRADELENS_ALLOWED_PEER_IPS from env
        with (
            patch.dict(os.environ, {"TRADELENS_ALLOWED_PEER_IPS": "100.64.0.25", "TRADELENS_ENV_FILE": ""}, clear=False),
            patch.object(runtime_cli, "run_frontend_server") as mock_fe,
        ):
            rc_fe = runtime_cli._cmd_serve_frontend(parser.parse_args(["serve-frontend", "--bind-host", "127.0.0.1"]))
        self.assertEqual(rc_fe, 0)
        self.assertIsNone(mock_fe.call_args.kwargs["allowed_peer_ips"])

        # cmd_broker_auth strictly refuses non-loopback target URLs
        with self.assertRaises(ValueError):
            runtime_cli.cmd_broker_auth(
                parser.parse_args(["broker-auth", "--admin-url", "https://100.64.0.10:8000/api/broker/authenticate"])
            )

        # cmd_broker_auth works cleanly against loopback admin endpoint using --owner-secret-prompt
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse

        class _FakeHTTPResponse:
            def __init__(self, status: int, body: object) -> None:
                self.status = status
                self._buf = json.dumps(body).encode("utf-8")

            def read(self) -> bytes:
                return self._buf

            def __enter__(self) -> "_FakeHTTPResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            rt, api_handler, _, _, _ = self._make_runtime_and_handlers(Path(td))
            captured_urls: List[str] = []

            def _fake_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> _FakeHTTPResponse:
                captured_urls.append(req.full_url)
                parsed = urlparse(req.full_url)
                req_body = json.loads(req.data.decode("utf-8")) if req.data else None
                st, resp_body, _ = _dispatch_http(
                    api_handler,
                    req.get_method(),
                    parsed.path,
                    client_ip=parsed.hostname or "127.0.0.1",
                    headers={k: v for k, v in req.header_items()},
                    body=req_body,
                )
                if st >= 400:
                    raise urllib.error.HTTPError(
                        req.full_url,
                        st,
                        "HTTP Error",
                        {},
                        io.BytesIO(json.dumps(resp_body).encode("utf-8")),
                    )
                return _FakeHTTPResponse(st, resp_body)

            out = io.StringIO()
            with (
                patch("urllib.request.urlopen", side_effect=_fake_urlopen),
                patch("getpass.getpass", return_value=STRONG_OWNER),
                contextlib.redirect_stdout(out),
            ):
                rc_ba = runtime_cli.cmd_broker_auth(
                    parser.parse_args(
                        [
                            "broker-auth",
                            "--admin-url",
                            "http://127.0.0.1:8001/api/broker/authenticate",
                            "--owner-secret-prompt",
                        ]
                    )
                )
            self.assertEqual(rc_ba, 0)
            self.assertEqual(captured_urls, ["http://127.0.0.1:8001/api/broker/authenticate"])
            ba_json = json.loads(out.getvalue())
            self.assertEqual(ba_json["signals"]["broker_auth_status"], "AUTHENTICATED")
            self.assertEqual(rt.provider.connect_calls, 1)

        # main.run_server starts companion loopback admin server strictly on 127.0.0.1:8001 in remote mode
        class _FakeServer:
            def __init__(self, addr: Tuple[str, int], _handler: object) -> None:
                self.server_address = addr
                self.closed = False
                self.shut_down = False
                self._stop_ev = threading.Event()

            def serve_forever(self) -> None:
                self._stop_ev.wait(timeout=0.05)

            def shutdown(self) -> None:
                self.shut_down = True
                self._stop_ev.set()

            def server_close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as td:
            rt_remote, _, _, _, _ = self._make_runtime_and_handlers(Path(td))
            primary_srv = _FakeServer(("100.64.0.10", 8000), object())
            spawned_admin_addrs: List[Tuple[str, int]] = []
            spawned_admin_srvs: List[_FakeServer] = []

            def _fake_admin_ctor(addr: Tuple[str, int], handler: object) -> _FakeServer:
                spawned_admin_addrs.append(addr)
                s = _FakeServer(addr, handler)
                spawned_admin_srvs.append(s)
                return s

            with (
                patch.dict(os.environ, {"TRADELENS_BIND_HOST": "100.64.0.10", "TRADELENS_LOCAL_ADMIN_PORT": ""}, clear=False),
                patch.object(main_mod, "read_env_dict", return_value={"TRADELENS_BIND_HOST": "100.64.0.10"}),
                patch.object(main_mod, "load_env", return_value={}),
                patch.object(main_mod, "build_api_server", return_value=(primary_srv, rt_remote)),
                patch.object(main_mod, "ThreadingTradeLensServer", side_effect=_fake_admin_ctor),
            ):
                main_mod.run_server(port=8000, bind_host="100.64.0.10")

            self.assertEqual(spawned_admin_addrs, [("127.0.0.1", 8001)])
            self.assertTrue(primary_srv.closed)
            self.assertTrue(spawned_admin_srvs[0].shut_down)
            self.assertTrue(spawned_admin_srvs[0].closed)

            # TRADELENS_LOCAL_ADMIN_PORT=0 cleanly disables companion loopback admin server
            spawned_admin_addrs.clear()
            primary_srv2 = _FakeServer(("100.64.0.10", 8000), object())
            with (
                patch.dict(os.environ, {"TRADELENS_BIND_HOST": "100.64.0.10", "TRADELENS_LOCAL_ADMIN_PORT": "0"}, clear=False),
                patch.object(main_mod, "read_env_dict", return_value={"TRADELENS_BIND_HOST": "100.64.0.10"}),
                patch.object(main_mod, "load_env", return_value={}),
                patch.object(main_mod, "build_api_server", return_value=(primary_srv2, rt_remote)),
                patch.object(main_mod, "ThreadingTradeLensServer", side_effect=_fake_admin_ctor),
            ):
                main_mod.run_server(port=8000, bind_host="100.64.0.10")
            self.assertEqual(spawned_admin_addrs, [])


if __name__ == "__main__":
    unittest.main()
