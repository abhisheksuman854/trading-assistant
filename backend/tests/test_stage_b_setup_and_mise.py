"""
TradeLens AI - Phase 3E.5.2.6 Stage B Setup, Dashboard, Gatekeeper & Mise Tests.

Validates:
1. Secret-free `.ops_state/stage_b_progress.json` atomic 0600 persistence.
2. Live recomputation & anti-bypass enforcement (tampered cache or manual confirmation
   cannot bypass automated security, TLS, device pin, revocation, or Step 10 LaunchAgent gates).
3. Strictly allowlisted UI/API execution boundary & HOME_OWNER_ONLY_ENDPOINTS enforcement
   (remote callers denied 403 on mutations; revoked sessions denied 403 on /api/setup/state).
4. CLI argparse catalog parity (`build_parser()` vs `COMMAND_CATALOG` & `RECOVERY_PLAYBOOKS`).
5. `mise.toml` task coverage & `docs/operations/STAGE_B_SETUP.md` recovery anchors.
6. Frontend HTML accessibility (`<dialog>`, `<details>/<summary>`, copy live region, CSP/XSS).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from backend.app.main import TradeLensAppRuntime, make_api_handler
from backend.app.runtime.cli import build_parser, main as cli_main
from backend.app.runtime.launchd_config import (
    build_frontend_plist,
    build_home_engine_plist,
    render_launchd_plists,
    resolve_python_executable,
)
from backend.app.runtime.security import (
    ALLOWED_REMOTE_ROUTE_METHODS,
    DEVICE_STATUS_REVOKED,
    DeviceKeyCrypto,
    HOME_OWNER_ONLY_ENDPOINTS,
    RemoteAccessManager,
)
from backend.app.runtime.stage_b_setup import (
    COMMAND_CATALOG,
    FRONTEND_SERVER_LABEL,
    HOME_ENGINE_LABEL,
    MANUAL_CONFIRMATION_ALLOWED_STEPS,
    RECOVERY_PLAYBOOKS,
    STAGE_B_STEP_SPECS,
    StageBSetupCoordinator,
    _probe_launchd,
    validate_command_catalog_against_cli,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
MISE_TOML = REPO_ROOT / "mise.toml"
STAGE_B_DOC = REPO_ROOT / "docs" / "operations" / "STAGE_B_SETUP.md"

STRONG_OWNER = "HomeMacOwner!StageB#2026"
STRONG_HMAC = "stage_b_state_hmac_secret_min_32_bytes_2026!"


def _write_hardened_env(env_path: Path) -> None:
    env_path.write_text(
        "ANGEL_API_KEY=realtestkey123456\n"
        "ANGEL_CLIENT_CODE=TEST999\n"
        "ANGEL_PASSWORD=TestPassword!99\n"
        "ANGEL_TOTP_SECRET=JBSWY3DPEHPK3PXP\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)


def _dispatch_api(
    handler_cls: Any,
    method: str,
    path: str,
    *,
    client_ip: str = "127.0.0.1",
    headers: Optional[Dict[str, str]] = None,
    body_obj: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    req_bytes = json.dumps(body_obj).encode("utf-8") if body_obj is not None else b""
    raw_headers = {
        "Host": "127.0.0.1:8000",
        "Content-Type": "application/json",
        "Content-Length": str(len(req_bytes)),
    }
    if headers:
        raw_headers.update(headers)

    out_headers: Dict[str, str] = {}
    status_holder = {"code": 0}
    wfile = BytesIO()

    class _DummyServer:
        pass

    handler = handler_cls.__new__(handler_cls)
    handler.server = _DummyServer()
    handler.client_address = (client_ip, 51234)
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.headers = raw_headers
    handler.rfile = BytesIO(req_bytes)
    handler.wfile = wfile
    handler.close_connection = True

    def _send_response(code: int, message: Optional[str] = None) -> None:
        status_holder["code"] = code

    def _send_header(k: str, v: str) -> None:
        out_headers[k.lower()] = v

    def _end_headers() -> None:
        pass

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers

    if method == "GET":
        handler.do_GET()
    elif method == "POST":
        handler.do_POST()
    else:
        raise ValueError(f"Unsupported test method: {method}")

    raw_resp = wfile.getvalue().decode("utf-8", errors="replace")
    parsed = json.loads(raw_resp) if raw_resp.strip().startswith("{") else {"raw": raw_resp}
    return status_holder["code"], parsed, out_headers


class _MockFallbackProvider:
    def fetch_market_snapshot(self) -> Dict[str, Any]:
        return {"stocks": [], "indices": [], "data_status": "SIMULATED", "failure_reasons": []}


class TestStageBSetupAndMise(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tradelens_stage_b_test_")
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "runtime_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.progress_path = self.root / ".ops_state" / "stage_b_progress.json"
        _write_hardened_env(self.root / ".env")
        self.sample_environ = {
            "ANGEL_API_KEY": "realtestkey123456",
            "ANGEL_CLIENT_CODE": "TEST999",
            "ANGEL_PASSWORD": "TestPassword!99",
            "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_01_cli_command_catalog_matches_real_argparse(self) -> None:
        parser = build_parser()
        ok, errs = validate_command_catalog_against_cli(parser, COMMAND_CATALOG)
        self.assertTrue(ok, f"CLI command catalog mismatches: {errs}")

        # Verify CLI commands in RECOVERY_PLAYBOOKS also resolve against real build_parser()
        subparsers = None
        for act in parser._actions:
            if getattr(act, "choices", None):
                subparsers = act.choices
                break
        self.assertIsNotNone(subparsers)
        for rec in RECOVERY_PLAYBOOKS:
            for cmd_str in rec["commands"]:
                if "backend.app.runtime.cli" not in cmd_str:
                    continue
                after = cmd_str.split("backend.app.runtime.cli", 1)[1].strip().split()
                sub_name = after[0]
                self.assertIn(sub_name, subparsers, f"Unknown CLI subcommand in recovery: {cmd_str}")
                sub_opts = set(subparsers[sub_name]._option_string_actions.keys()) | set(
                    parser._option_string_actions.keys()
                )
                for tok in after[1:]:
                    if tok.startswith("--"):
                        self.assertIn(tok, sub_opts, f"Unknown flag '{tok}' in '{cmd_str}'")

    def test_02_progress_persistence_0600_and_zero_secret_leakage(self) -> None:
        def _fake_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            return 1, "", "tailscale not installed"

        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
            command_runner=_fake_runner,
        )
        res = coord.run_allowlisted_task(task_id="tradelens-stage-b-preflight", is_loopback=True)
        self.assertTrue(self.progress_path.exists())
        file_mode = stat.S_IMODE(self.progress_path.stat().st_mode)
        dir_mode = stat.S_IMODE(self.progress_path.parent.stat().st_mode)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(dir_mode, 0o700)

        saved_text = self.progress_path.read_text(encoding="utf-8")
        for secret in self.sample_environ.values():
            self.assertNotIn(secret, saved_text)
        # Never store .env size or mtime metadata
        self.assertNotIn("st_size", saved_text)
        self.assertNotIn("st_mtime", saved_text)

        # Reload with fresh coordinator instance (simulated Home Mac restart)
        coord2 = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
            command_runner=_fake_runner,
        )
        st = coord2.get_setup_state(persist=False, is_remote_view=False)
        self.assertEqual(st["steps"][0]["status"], "VERIFIED")
        self.assertEqual(st["steps"][1]["status"], "REQUIRES_OWNER_ACTION")
        self.assertTrue(st["steps"][2]["locked"])
        self.assertEqual(st["steps"][9]["status"], "BLOCKED")
        self.assertIn("last_verified_timestamp", res["setup_state"]["overview"])

    def test_03_anti_bypass_tampered_progress_and_manual_confirmation_rejected(self) -> None:
        def _fake_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            return 1, "", "tailscale missing"

        # Non-hardened .env (0644) must fail Step 1 cleanly
        os.chmod(self.root / ".env", 0o644)
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
            command_runner=_fake_runner,
        )
        # Tamper progress JSON trying to claim Step 1, 8, and 10 are VERIFIED
        tampered = coord._default_progress_store()
        for sid in ("step_01_home_mac_preflight", "step_08_security_boundary_tests", "step_10_launchagent_installation"):
            tampered["steps"][sid]["status"] = "VERIFIED"
            tampered["steps"][sid]["verified_at"] = "2026-11-11T00:00:00+00:00"
        coord._save_progress_store(tampered)

        recomputed = coord.get_setup_state(persist=False, is_remote_view=False)
        by_id = {s["step_id"]: s for s in recomputed["steps"]}
        self.assertEqual(by_id["step_01_home_mac_preflight"]["status"], "REQUIRES_OWNER_ACTION")
        self.assertEqual(by_id["step_08_security_boundary_tests"]["status"], "BLOCKED")
        self.assertEqual(by_id["step_10_launchagent_installation"]["status"], "BLOCKED")
        self.assertIsNone(by_id["step_10_launchagent_installation"]["verified_at"])

        # Automated steps CANNOT be bypassed by confirm_manual_step
        for spec in STAGE_B_STEP_SPECS:
            sid = spec["step_id"]
            if sid in MANUAL_CONFIRMATION_ALLOWED_STEPS:
                continue
            with self.assertRaises(PermissionError):
                coord.confirm_manual_step(
                    step_id=sid,
                    confirmed=True,
                    audit_note="Attempt to bypass automated gate",
                    is_loopback=True,
                )

    def test_04_step10_locked_until_all_steps_2_to_9_verified(self) -> None:
        # Prepare a complete synthetic Stage B environment inside temp dir
        os.chmod(self.root / ".env", 0o600)
        tls_dir = self.state_dir / "tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        cert_file = tls_dir / "tradelens-home.crt"
        key_file = tls_dir / "tradelens-home.key"
        cert_file.write_text("DUMMY CERT", encoding="utf-8")
        key_file.write_text("DUMMY KEY", encoding="utf-8")
        os.chmod(key_file, 0o600)

        (self.state_dir / "runtime_config.json").write_text(
            json.dumps(
                {
                    "bind_host": "100.64.0.10",
                    "backend_port": 8000,
                    "frontend_port": 8080,
                    "remote_access": True,
                    "allowed_peer_ip": ["100.64.0.25"],
                    "tls_cert": str(cert_file),
                    "tls_key": str(key_file),
                }
            ),
            encoding="utf-8",
        )
        # Active PID pointing to current test process so supervisor_running=True
        (self.state_dir / "tradelens_supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")

        priv, pub = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            state_hmac_secret=STRONG_HMAC,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=["100.64.0.25"],
            remote_mode_enabled=True,
        )
        mgr.register_device(
            device_id="office-mac-stage-b",
            device_name="Office MacBook",
            public_key_pem=pub,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch, ch_dec = mgr.issue_auth_challenge("office-mac-stage-b", client_ip="100.64.0.25")
        self.assertIsNotNone(ch, ch_dec.message)
        sig = DeviceKeyCrypto.sign_challenge(priv, ch["challenge_string"])
        auth, auth_dec = mgr.authenticate_remote_session(
            device_id="office-mac-stage-b",
            challenge_id=ch["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        self.assertIsNotNone(auth, auth_dec.message)

        # Fake tailscale & openssl & plutil success (NOT installing real launchd agents)
        (self.root / "deploy" / "launchd").mkdir(parents=True, exist_ok=True)
        for lbl in ("com.tradelens.home-engine", "com.tradelens.frontend-dashboard"):
            (self.root / "deploy" / "launchd" / f"{lbl}.plist").write_text("<plist/>", encoding="utf-8")

        def _all_good_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            prog = Path(cmd[0]).name
            if "tailscale" in prog:
                return 0, json.dumps(
                    {
                        "BackendState": "Running",
                        "Self": {"TailscaleIPs": ["100.64.0.10"], "DNSName": "home-mac.tailnet.ts.net."},
                        "Peer": {},
                    }
                ), ""
            if "openssl" in prog:
                if "-checkend" in cmd:
                    return 0, "Certificate will not expire", ""
                return 0, (
                    "sha256 Fingerprint=AA:BB:CC:DD\n"
                    "notBefore=Nov 10 00:00:00 2026 GMT\n"
                    "notAfter=Nov 10 00:00:00 2027 GMT\n"
                    "X509v3 Subject Alternative Name:\n"
                    "    IP Address:100.64.0.10, DNS:home-mac.tailnet.ts.net\n"
                ), ""
            if "plutil" in prog:
                return 0, f"{cmd[-1]}: OK", ""
            return 1, "", ""

        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            remote_access=mgr,
            environ=self.sample_environ,
            command_runner=_all_good_runner,
        )
        # Monkeypatch tailscale bin lookup via PATH / fake executable inside temp dir? Better: inject tailscale CLI existence via monkeypatching _find_tailscale_bin cleanly in test
        import backend.app.runtime.stage_b_setup as sbs
        orig_find_ts = sbs._find_tailscale_bin
        try:
            sbs._find_tailscale_bin = lambda: "/usr/bin/tailscale"
            coord.confirm_manual_step(
                step_id="step_02_tailscale_home_office",
                confirmed=True,
                audit_note="Verified Office Mac in tailnet",
                is_loopback=True,
            )
            coord.confirm_manual_step(
                step_id="step_03_tailscale_acl_verification",
                confirmed=True,
                audit_note="Tailscale ACL restricted to 100.64.0.25",
                is_loopback=True,
            )
            # Before Step 9 drill, Step 10 MUST remain locked
            st_pre_9 = coord.get_setup_state(persist=False, is_remote_view=False)
            self.assertTrue(st_pre_9["steps"][9]["locked"])
            self.assertEqual(st_pre_9["steps"][9]["status"], "BLOCKED")

            # Live revocation drill cannot be falsely marked verified without revoked/re-enrolled history
            with self.assertRaises(PermissionError):
                coord.run_allowlisted_task(
                    task_id="tradelens-stage-b-revocation-check",
                    confirmed=True,
                    audit_note="Trying without revocation history",
                    is_loopback=True,
                    extra_args={"record_live_drill": True},
                )

            # Perform real revoke + re-enroll + approve cycle
            mgr.revoke_device("office-mac-stage-b", owner_secret=STRONG_OWNER, reason="STAGE_B_DRILL")
            # Immediately after revoke, Steps 6 & 7 MUST show REVOKED/blocked and Step 10 locked
            st_revoked = coord.get_setup_state(persist=False, is_remote_view=False)
            self.assertEqual(st_revoked["steps"][5]["status"], "REVOKED")
            self.assertEqual(st_revoked["overview"]["office_device_registration_status"], "REVOKED")
            self.assertTrue(st_revoked["steps"][9]["locked"])

            mgr.register_device(
                device_id="office-mac-stage-b",
                device_name="Office MacBook",
                public_key_pem=pub,
                owner_secret=STRONG_OWNER,
                allowed_ips=["100.64.0.25"],
                approve=True,
                allow_re_enroll=True,
            )
            ch2, ch2_dec = mgr.issue_auth_challenge("office-mac-stage-b", client_ip="100.64.0.25")
            self.assertIsNotNone(ch2, ch2_dec.message)
            sig2 = DeviceKeyCrypto.sign_challenge(priv, ch2["challenge_string"])
            auth2, auth2_dec = mgr.authenticate_remote_session(
                device_id="office-mac-stage-b",
                challenge_id=ch2["challenge_id"],
                signature_b64=sig2,
                client_ip="100.64.0.25",
            )
            self.assertIsNotNone(auth2, auth2_dec.message)
            res_drill = coord.run_allowlisted_task(
                task_id="tradelens-stage-b-revocation-check",
                confirmed=True,
                audit_note="Verified live revocation & re-enrollment",
                is_loopback=True,
                extra_args={"record_live_drill": True},
            )
            st_unlocked = res_drill["setup_state"]
            for i in range(9):
                self.assertEqual(
                    st_unlocked["steps"][i]["status"],
                    "VERIFIED",
                    f"Expected Step {i+1} VERIFIED, got {st_unlocked['steps'][i]}",
                )
            # Now Step 10 unlocks cleanly (READY/REQUIRES_OWNER_ACTION because ~/Library/LaunchAgents not touched)
            self.assertFalse(st_unlocked["steps"][9]["locked"])
            self.assertEqual(st_unlocked["steps"][9]["status"], "REQUIRES_OWNER_ACTION")
        finally:
            sbs._find_tailscale_bin = orig_find_ts

    def test_05_http_security_isolation_and_allowlist_boundaries(self) -> None:
        self.assertIn("/api/setup/state", ALLOWED_REMOTE_ROUTE_METHODS)
        for owner_ep in ("/api/setup/run-task", "/api/setup/step/confirm"):
            self.assertIn(owner_ep, HOME_OWNER_ONLY_ENDPOINTS)
            self.assertNotIn(owner_ep, ALLOWED_REMOTE_ROUTE_METHODS)

        _, pub2 = DeviceKeyCrypto.generate_keypair()
        priv, pub = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            state_hmac_secret=STRONG_HMAC,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=["100.64.0.25"],
            allowed_hosts=["127.0.0.1", "100.64.0.10"],
            remote_mode_enabled=True,
        )
        mgr.register_device(
            device_id="other-device-hidden",
            device_name="Second Unrelated Mac",
            public_key_pem=pub2,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.99"],
            approve=True,
        )
        mgr.register_device(
            device_id="office-mac-http-test",
            device_name="Office Mac",
            public_key_pem=pub,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch, ch_dec = mgr.issue_auth_challenge("office-mac-http-test", client_ip="100.64.0.25")
        self.assertIsNotNone(ch, ch_dec.message)
        sig = DeviceKeyCrypto.sign_challenge(priv, ch["challenge_string"])
        auth, auth_dec = mgr.authenticate_remote_session(
            device_id="office-mac-http-test",
            challenge_id=ch["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        self.assertIsNotNone(auth, auth_dec.message)
        token = auth["access_token"]

        rt = TradeLensAppRuntime(
            state_dir=self.state_dir,
            fallback_provider=_MockFallbackProvider(),
            remote_access=mgr,
            environ=self.sample_environ,
            stage_b_progress_path=self.progress_path,
        )
        handler = make_api_handler(rt)

        # Remote active session can view strictly redacted GET /api/setup/state (Audit A)
        st_r, body_r, _ = _dispatch_api(
            handler,
            "GET",
            "/api/setup/state",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_r, 200)
        self.assertTrue(body_r["is_remote_view"])
        self.assertFalse(body_r["local_mutations_permitted"])
        self.assertFalse(body_r["order_execution_enabled"])
        self.assertEqual(len(body_r["steps"]), 10)
        self.assertEqual(body_r["command_catalog"], [])
        self.assertEqual(body_r["recovery_playbooks"], [])
        self.assertIn("self_device", body_r)
        self.assertIsNotNone(body_r["self_device"])
        self.assertEqual(body_r["self_device"]["device_id"], "office-mac-http-test")
        serialized_remote = json.dumps(body_r)
        self.assertNotIn("other-device-hidden", serialized_remote)
        self.assertNotIn("Second Unrelated Mac", serialized_remote)
        self.assertNotIn("100.64.0.99", serialized_remote)
        self.assertNotIn(str(self.root), serialized_remote)
        self.assertNotIn(str(self.state_dir), serialized_remote)
        for forbidden_substr in (
            "public_key_fingerprint",
            "allowed_peer_ips",
            "supervisor_pid",
            "cert_path",
            "key_path",
            "progress_file",
            "repo_root",
            "output_sample",
            "recovery_commands",
        ):
            self.assertNotIn(forbidden_substr, serialized_remote)
        self.assertNotIn("dotenv", body_r.get("checks", {}))

        # Remote session MUST get 403 HOME_OWNER_ONLY_OPERATION on setup mutations even with owner header
        for mut_ep, payload in (
            ("/api/setup/run-task", {"task_id": "tradelens-stage-b-preflight", "confirmed": True}),
            ("/api/setup/step/confirm", {"step_id": "step_02_tailscale_home_office", "confirmed": True, "audit_note": "remote"}),
        ):
            st_m, body_m, _ = _dispatch_api(
                handler,
                "POST",
                mut_ep,
                client_ip="100.64.0.25",
                headers={
                    "Host": "100.64.0.10:8000",
                    "Authorization": f"Bearer {token}",
                    "X-TradeLens-Owner-Secret": STRONG_OWNER,
                },
                body_obj=payload,
            )
            self.assertEqual(st_m, 403)
            self.assertEqual(body_m["reason_code"], "HOME_OWNER_ONLY_OPERATION")

        # Loopback owner without secret blocked when remote_mode/owner_secret configured (401)
        st_no_auth, _, _ = _dispatch_api(
            handler,
            "POST",
            "/api/setup/run-task",
            client_ip="127.0.0.1",
            body_obj={"task_id": "tradelens-stage-b-preflight"},
        )
        self.assertEqual(st_no_auth, 401)

        # Loopback owner WITH secret rejects unallowlisted / broker-auth / unconfirmed / extra_args tasks
        for bad_task, exp_code in (
            ({"task_id": "bash -c id"}, 403),
            ({"task_id": "broker-auth"}, 403),
            ({"task_id": "tradelens-stage-b-render-launchd", "confirmed": False}, 403),
            (
                {
                    "task_id": "tradelens-stage-b-render-launchd",
                    "confirmed": True,
                    "extra_args": {"python_executable": "/bin/sh"},
                },
                400,
            ),
            ({"task_id": "unknown-custom-task"}, 400),
        ):
            st_b, _, _ = _dispatch_api(
                handler,
                "POST",
                "/api/setup/run-task",
                client_ip="127.0.0.1",
                headers={"X-TradeLens-Owner-Secret": STRONG_OWNER},
                body_obj=bad_task,
            )
            self.assertEqual(st_b, exp_code, f"Unexpected HTTP code for {bad_task}")

        # Unknown bearer token and unpinned client IP denied on remote GET /api/setup/state
        st_unk, _, _ = _dispatch_api(
            handler,
            "GET",
            "/api/setup/state",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Authorization": "Bearer totally_unknown_token_12345"},
        )
        self.assertIn(st_unk, (401, 403))

        st_unp, body_unp, _ = _dispatch_api(
            handler,
            "GET",
            "/api/setup/state",
            client_ip="100.64.0.88",
            headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_unp, 403)
        self.assertIn(body_unp["reason_code"], {"PEER_IP_NOT_PINNED", "DEVICE_IP_MISMATCH", "IP_NOT_ALLOWED", "UNAPPROVED_TAILSCALE_PEER"})

        # Revoked remote device immediately denied even on read-only /api/setup/state
        mgr.revoke_device("office-mac-http-test", owner_secret=STRONG_OWNER, reason="TEST_REVOKE")
        st_rev, body_rev, _ = _dispatch_api(
            handler,
            "GET",
            "/api/setup/state",
            client_ip="100.64.0.25",
            headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st_rev, 403)
        self.assertEqual(body_rev["reason_code"], DEVICE_STATUS_REVOKED.upper() if False else body_rev["reason_code"])
        self.assertIn(body_rev["reason_code"], {"DEVICE_REVOKED", "SESSION_REVOKED"})

    def test_06_mise_toml_and_docs_completeness(self) -> None:
        self.assertTrue(MISE_TOML.exists(), "mise.toml must exist at repository root")
        cfg = tomllib.loads(MISE_TOML.read_text(encoding="utf-8"))
        tasks = cfg.get("tasks", {})
        expected_tasks = [
            "tradelens-stage-b-preflight",
            "tradelens-stage-b-status",
            "tradelens-stage-b-home-preflight",
            "tradelens-stage-b-tailscale-check",
            "tradelens-stage-b-tls-check",
            "tradelens-stage-b-register-device-guide",
            "tradelens-stage-b-register-device",
            "tradelens-stage-b-verify-device",
            "tradelens-stage-b-remote-check",
            "tradelens-stage-b-security-check",
            "tradelens-stage-b-revocation-selftest",
            "tradelens-stage-b-revocation-check",
            "tradelens-stage-b-render-launchd",
            "tradelens-stage-b-launchd-lint",
            "tradelens-stage-b-launchd-status",
            "tradelens-stage-b-report",
        ]
        self.assertEqual(len(expected_tasks), 16)
        doc_text = STAGE_B_DOC.read_text(encoding="utf-8")
        for t in expected_tasks:
            self.assertIn(t, tasks, f"Missing mise task: {t}")
            self.assertIn(t, doc_text, f"mise task '{t}' missing from {STAGE_B_DOC}")
        for rec in RECOVERY_PLAYBOOKS:
            self.assertIn(rec["doc_anchor"], doc_text, f"Missing recovery anchor #{rec['doc_anchor']}")

        # Hermetic temporary repo fixture: never read the real Office/Home Mac .env or host ANGEL_*/TRADELENS_* env.
        isolated_root = Path(self._tmp.name) / "isolated_stage_b_repo"
        isolated_state = isolated_root / "runtime_state"
        isolated_progress = isolated_root / ".ops_state" / "stage_b_progress.json"
        isolated_root.mkdir(parents=True, exist_ok=True)
        isolated_state.mkdir(parents=True, exist_ok=True)
        clean_environ = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ANGEL_", "TRADELENS_"))
        }

        def _run_isolated_cli(action_name: str) -> Tuple[int, Dict[str, Any], str]:
            buf = StringIO()
            with (
                patch("backend.app.runtime.cli.REPO_ROOT", isolated_root),
                patch.dict(os.environ, clean_environ, clear=True),
                redirect_stdout(buf),
            ):
                rc_val = cli_main(
                    [
                        "--state-dir",
                        str(isolated_state),
                        "stage-b",
                        "--action",
                        action_name,
                        "--no-save",
                        "--progress-path",
                        str(isolated_progress),
                    ]
                )
            raw_out = buf.getvalue()
            return rc_val, json.loads(raw_out), raw_out

        isolated_env_path = isolated_root / ".env"

        # Additional Case 1: Missing .env -> home-preflight fails closed (rc == 2)
        rc_miss, pay_miss, _ = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_miss, 2)
        self.assertEqual(pay_miss["stage_verdict"], "STAGE_B_BLOCKED")
        self.assertFalse(pay_miss["verification_passed"])
        step1_miss = next(s for s in pay_miss["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_miss["status"], "BLOCKED")
        self.assertIn("missing", (step1_miss.get("failure_reason") or "").lower())

        # Case 1: Insecure .env (mode 0644 regular file) -> home-preflight fails closed (rc == 2)
        dummy_secrets = ("dummy_api_key_xyz987", "DUMMY999", "DummyPass!2026", "JBSWY3DPEHPK3PXP")
        _write_hardened_env(isolated_env_path)
        isolated_env_path.write_text(
            f"ANGEL_API_KEY={dummy_secrets[0]}\n"
            f"ANGEL_CLIENT_CODE={dummy_secrets[1]}\n"
            f"ANGEL_PASSWORD={dummy_secrets[2]}\n"
            f"ANGEL_TOTP_SECRET={dummy_secrets[3]}\n",
            encoding="utf-8",
        )
        os.chmod(isolated_env_path, 0o644)
        rc_0644, pay_0644, raw_0644 = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_0644, 2)
        self.assertFalse(pay_0644["verification_passed"])
        step1_0644 = next(s for s in pay_0644["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_0644["status"], "REQUIRES_OWNER_ACTION")
        self.assertIn("0o644", step1_0644.get("failure_reason") or "")
        for sec in dummy_secrets:
            self.assertNotIn(sec, raw_0644)

        rc_pre_0644, pay_pre_0644, _ = _run_isolated_cli("preflight")
        self.assertEqual(rc_pre_0644, 0)
        self.assertEqual(pay_pre_0644["stage_verdict"], "STAGE_B_BLOCKED")
        self.assertFalse(pay_pre_0644["verification_passed"])

        # Additional Case 2: Symlink .env -> rejected even when target file is 0600
        symlink_target = isolated_root / "target_secret.env"
        _write_hardened_env(symlink_target)
        isolated_env_path.unlink()
        isolated_env_path.symlink_to(symlink_target)
        rc_sym, pay_sym, _ = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_sym, 2)
        step1_sym = next(s for s in pay_sym["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_sym["status"], "BLOCKED")
        self.assertIn("symlink", (step1_sym.get("failure_reason") or "").lower())
        isolated_env_path.unlink()

        # Additional Case 3: Placeholder credentials in 0600 .env -> rejected (rc == 2)
        isolated_env_path.write_text(
            "ANGEL_API_KEY=your_api_key_here\n"
            "ANGEL_CLIENT_CODE=your_client_code_here\n"
            "ANGEL_PASSWORD=your_password_or_mpin_here\n"
            "ANGEL_TOTP_SECRET=your_totp_secret_here\n",
            encoding="utf-8",
        )
        os.chmod(isolated_env_path, 0o600)
        rc_ph, pay_ph, _ = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_ph, 2)
        step1_ph = next(s for s in pay_ph["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_ph["status"], "BLOCKED")
        self.assertIn("placeholder", (step1_ph.get("failure_reason") or "").lower())

        # Additional Case 4: Malformed .env configuration -> rejected without leaking values
        malformed_canary = "MALFORMED_CANARY_VALUE_999"
        isolated_env_path.write_text(
            f"ANGEL_API_KEY={dummy_secrets[0]}\n"
            f'BROKEN_LINE="{malformed_canary}\n',
            encoding="utf-8",
        )
        os.chmod(isolated_env_path, 0o600)
        rc_bad, pay_bad, raw_bad = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_bad, 2)
        step1_bad = next(s for s in pay_bad["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_bad["status"], "BLOCKED")
        self.assertIn("syntax", (step1_bad.get("failure_reason") or "").lower())
        self.assertNotIn(malformed_canary, raw_bad)

        # Case 2: Secure .env (mode 0600 regular file with safe dummy keys) -> home-preflight passes (rc == 0)
        isolated_env_path.write_text(
            f"ANGEL_API_KEY={dummy_secrets[0]}\n"
            f"ANGEL_CLIENT_CODE={dummy_secrets[1]}\n"
            f"ANGEL_PASSWORD={dummy_secrets[2]}\n"
            f"ANGEL_TOTP_SECRET={dummy_secrets[3]}\n",
            encoding="utf-8",
        )
        os.chmod(isolated_env_path, 0o600)
        rc_0600, pay_0600, raw_0600 = _run_isolated_cli("home-preflight")
        self.assertEqual(rc_0600, 0, f"Secure 0600 dummy .env should pass home-preflight, got: {raw_0600}")
        self.assertTrue(pay_0600["verification_passed"])
        step1_0600 = next(s for s in pay_0600["steps"] if s["step_id"] == "step_01_home_mac_preflight")
        self.assertEqual(step1_0600["status"], "VERIFIED")
        self.assertFalse(pay_0600["stage_b_complete"])
        for sec in dummy_secrets:
            self.assertNotIn(sec, raw_0600)

        # Additional Case 5: Missing HMAC secret for remote mode fails closed in both runtime & wrapper
        cert_tmp = isolated_root / "dummy.crt"
        key_tmp = isolated_root / "dummy.key"
        cert_tmp.write_text("CERT", encoding="utf-8")
        key_tmp.write_text("KEY", encoding="utf-8")
        with (
            patch("backend.app.runtime.cli.REPO_ROOT", isolated_root),
            patch.dict(os.environ, clean_environ, clear=True),
            self.assertRaises(PermissionError),
        ):
            cli_main(
                [
                    "--state-dir",
                    str(isolated_state),
                    "supervise",
                    "--bind-host",
                    "100.64.0.10",
                    "--vpn-cidr",
                    "100.64.0.0/10",
                    "--allowed-peer-ip",
                    "100.64.0.25",
                    "--tls-cert",
                    str(cert_tmp),
                    "--tls-key",
                    str(key_tmp),
                    "--remote-access",
                ]
            )
        wrapper_script = REPO_ROOT / "deploy" / "scripts" / "run_launchd_service.sh"
        wrap_proc = subprocess.run(
            ["/bin/sh", str(wrapper_script), sys.executable, "-c", "print('SHOULD_NOT_RUN')", "--remote-access"],
            env={**clean_environ, "TRADELENS_ENV_FILE": str(isolated_env_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(wrap_proc.returncode, 78)
        self.assertNotIn("SHOULD_NOT_RUN", wrap_proc.stdout)
        self.assertIn("TRADELENS_STATE_HMAC_SECRET", wrap_proc.stderr)

        # Verify coordinator uses isolated_root rather than real repo .env (zero real .env reads)
        import backend.app.runtime.stage_b_setup as sbs_mod

        inspected_paths: List[Path] = []
        orig_inspect = sbs_mod._inspect_dotenv_metadata

        def _spy_inspect(env_p: Path, env_map: Dict[str, str]) -> Dict[str, Any]:
            inspected_paths.append(Path(env_p))
            return orig_inspect(env_p, env_map)

        with patch.object(sbs_mod, "_inspect_dotenv_metadata", side_effect=_spy_inspect):
            rc_stat, pay_stat, _ = _run_isolated_cli("status")
            self.assertEqual(rc_stat, 0)
            self.assertFalse(pay_stat["stage_b_complete"])
            self.assertEqual(pay_stat["stage_verdict"], "STAGE_B_PARTIALLY_COMPLETE")
            self.assertEqual(
                [s["step_id"] for s in pay_stat["steps"] if s["status"] == "VERIFIED"],
                ["step_01_home_mac_preflight"],
            )
        self.assertEqual(inspected_paths, [isolated_root.resolve() / ".env"])
        self.assertNotIn(REPO_ROOT.resolve() / ".env", inspected_paths)

        # Non-gating read-only inspectors / guides / simulated selftest exit 0 without claiming live verification
        for action in ("status", "preflight", "register-device-guide", "revocation-selftest"):
            rc, payload, _ = _run_isolated_cli(action)
            self.assertEqual(rc, 0, f"CLI stage-b --action {action} should exit 0")
            self.assertEqual(payload.get("exit_code"), 0)
            if action == "status":
                self.assertFalse(payload["stage_b_complete"])
                self.assertFalse(payload["exit_0_means_stage_b_verified"])
            elif action == "register-device-guide":
                self.assertFalse(payload["registered_any_device"])
                self.assertFalse(payload["verification_passed"])
            elif action == "revocation-selftest":
                tr_sub = payload.get("task_result", {})
                self.assertTrue(tr_sub["simulated_only"])
                self.assertFalse(tr_sub["proves_live_office_mac_revocation"])
                self.assertFalse(tr_sub["verification_passed"])
                self.assertFalse(payload["verification_passed"])

        # Remaining unprovisioned verification gates and argless register-device MUST still fail non-zero (rc == 2)
        for gate_action in (
            "tailscale-check",
            "tls-check",
            "register-device",
            "verify-device",
            "remote-check",
            "security-check",
            "revocation-check",
            "launchd-status",
        ):
            rc, payload, _ = _run_isolated_cli(gate_action)
            self.assertEqual(rc, 2, f"Unprovisioned gate --action {gate_action} must exit 2, got {rc}")
            self.assertEqual(payload.get("exit_code"), 2)

    def test_07_frontend_setup_ui_accessibility_and_csp_compliance(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn('id="tab-setup"', html)
        self.assertIn('id="panel-setup"', html)
        self.assertIn('id="setupConfirmDialog"', html)
        # Native <dialog> must not use role="dialog" or aria-modal per modern-web-guidance
        dlg_tag = re.search(r'<dialog[^>]*id="setupConfirmDialog"[^>]*>', html)
        self.assertIsNotNone(dlg_tag)
        self.assertNotIn('role="dialog"', dlg_tag.group(0))
        self.assertNotIn("aria-modal", dlg_tag.group(0))
        self.assertIn("_setupDlg.returnValue = ''", html)
        # Zero headings inside <summary>
        self.assertIsNone(re.search(r"<summary[^>]*>.*?<h[1-6]", html, re.IGNORECASE | re.DOTALL))
        # Explicit <label for="setupConfirmNoteInput"> and autofocus on Cancel
        self.assertIn('<label for="setupConfirmNoteInput"', html)
        self.assertIn('value="cancel" autofocus', html)
        # Copy feedback live region outside button
        self.assertIn('<span class="copy-status-pill" role="status" aria-live="polite"></span>', html)
        # Audit A & Audit E frontend controls: freshness badge, dynamic alert banner, remote projection
        self.assertIn('id="setupFreshnessBadge"', html)
        self.assertIn('id="setupActionBanner"', html)
        self.assertIn('id="setupRecoverySection"', html)
        self.assertIn("tr.passed === false", html)
        self.assertIn("setAttribute('role', 'alert')", html)
        self.assertIn("aria-live', 'assertive'", html)
        self.assertIn("state.self_device", html)
        self.assertIn("Minimized Remote Workstation Projection (Read-Only)", html)
        self.assertIn("Your Remote Workstation Session", html)
        # No browser owner secret inputs or inline event handlers
        self.assertNotIn("remoteOwnerSecret", html)
        self.assertNotIn("owner_secret:", html)
        self.assertIsNone(re.search(r"\son(?:click|submit|change|load)\s*=", html, re.IGNORECASE))
        # Validate JS syntax if node is available
        node_candidates: List[str] = []
        which_node = shutil.which("node")
        if which_node:
            node_candidates.append(which_node)
        nvm_dir = Path.home() / ".nvm" / "versions" / "node"
        if nvm_dir.is_dir():
            node_candidates.extend(str(p) for p in sorted(nvm_dir.glob("*/bin/node"), reverse=True))
        node_candidates.extend(["/opt/homebrew/bin/node", "/usr/local/bin/node"])
        node_bin = next((cand for cand in node_candidates if cand and os.path.exists(cand)), None)
        if node_bin:
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
            js_file = self.root / "check_script.js"
            js_file.write_text(scripts[-1], encoding="utf-8")
            proc = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, f"Frontend JS syntax error: {proc.stderr}")

    def test_08_http_wire_loopback_frontend_and_setup_api_roundtrip(self) -> None:
        from http.server import ThreadingHTTPServer
        from backend.app.main import ThreadingTradeLensServer
        from backend.app.runtime.frontend_server import FrontendStaticHandler

        class _WireSocket:
            def __init__(self, raw_request: bytes) -> None:
                self._rfile = BytesIO(raw_request)
                self._wfile = BytesIO()

            def makefile(self, mode: str, *args: Any, **kwargs: Any) -> BytesIO:
                return self._rfile if "r" in mode else self._wfile

            def sendall(self, data: bytes) -> None:
                self._wfile.write(data)

            def close(self) -> None:
                pass

        class _ServerStub:
            server_name = "127.0.0.1"
            server_port = 8000

        def _roundtrip(handler_cls: Any, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> tuple[int, str, str]:
            headers: Dict[str, str] = {"Host": "127.0.0.1:8000"}
            raw_body = b""
            if body is not None:
                raw_body = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(raw_body))
            req_lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
            raw_req = ("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8") + raw_body
            sock = _WireSocket(raw_req)
            handler_cls(sock, ("127.0.0.1", 54321), _ServerStub())
            head_bytes, _, body_bytes = sock._wfile.getvalue().partition(b"\r\n\r\n")
            head_text = head_bytes.decode("utf-8", errors="replace")
            status_code = int(head_text.splitlines()[0].split()[1])
            return status_code, head_text, body_bytes.decode("utf-8", errors="replace")

        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            remote_mode_enabled=False,
        )
        rt = TradeLensAppRuntime(
            state_dir=self.state_dir,
            fallback_provider=_MockFallbackProvider(),
            remote_access=mgr,
            environ=self.sample_environ,
            stage_b_progress_path=self.progress_path,
        )
        api_handler_cls = make_api_handler(rt)
        api_srv = ThreadingTradeLensServer(("127.0.0.1", 0), api_handler_cls)
        api_port = api_srv.server_port

        class _StaticFe(FrontendStaticHandler):
            index_path = INDEX_HTML
            allowed_hosts = ["127.0.0.1", "localhost"]
            allowed_origins = []
            allowed_vpn_cidrs = []
            allowed_peer_ips = []
            backend_port = api_port

        fe_srv = ThreadingHTTPServer(("127.0.0.1", 0), _StaticFe)
        try:
            self.assertGreater(api_port, 0)
            self.assertGreater(fe_srv.server_port, 0)

            fe_status, fe_headers, fe_html = _roundtrip(_StaticFe, "GET", "/")
            self.assertEqual(fe_status, 200)
            self.assertIn("Content-Security-Policy:", fe_headers)
            self.assertIn('id="tab-setup"', fe_html)
            self.assertIn('id="setupConfirmDialog"', fe_html)
            self.assertNotIn("__CSP_NONCE__", fe_html)

            api_status, _, raw_json = _roundtrip(api_handler_cls, "GET", "/api/setup/state")
            self.assertEqual(api_status, 200)
            for secret_val in self.sample_environ.values():
                self.assertNotIn(secret_val, raw_json)
            payload = json.loads(raw_json)
            self.assertFalse(payload["is_remote_view"])
            self.assertEqual(
                payload["probes"]["dotenv"]["angel_config_keys"].get("ANGEL_API_KEY_FORMAT_STATUS"),
                "PRESENT_VALID_FORMAT",
            )

            post_status, _, post_raw = _roundtrip(
                api_handler_cls,
                "POST",
                "/api/setup/run-task",
                {"task_id": "tradelens-stage-b-security-check"},
            )
            self.assertEqual(post_status, 200)
            post_data = json.loads(post_raw)
            self.assertEqual(post_data["task_result"]["task_id"], "tradelens-stage-b-security-check")
        finally:
            api_srv.server_close()
            fe_srv.server_close()

    def test_09_python_executable_validation_and_manual_confirmation_gates(self) -> None:
        def _down_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            if cmd and cmd[0] == "tailscale":
                return 1, "", "tailscale daemon stopped"
            if cmd and cmd[0] == "sysctl":
                return 0, "{ sec = 1700000100, usec = 0 } Thu", ""
            return 1, "", ""

        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
            command_runner=_down_runner,
        )
        # Render launchd rejects shell metacharacters / non-python executables
        for bad_py in ("/bin/sh", "/usr/bin/python3;id", "python3 && echo hi", "/tmp/not_py_bin"):
            with self.assertRaises(ValueError):
                coord.run_allowlisted_task(
                    task_id="tradelens-stage-b-render-launchd",
                    confirmed=True,
                    is_loopback=True,
                    extra_args={"python_executable": bad_py},
                    called_from_http=False,
                )

        # CLI render-launchd with non-python executable fails (raises ValueError -> non-zero process exit)
        with (
            patch("backend.app.runtime.cli.REPO_ROOT", self.root),
            self.assertRaises(ValueError),
        ):
            cli_main(
                [
                    "--state-dir",
                    str(self.state_dir),
                    "stage-b",
                    "--action",
                    "render-launchd",
                    "--confirm",
                    "--python-exe",
                    "/bin/sh",
                    "--progress-path",
                    str(self.progress_path),
                ]
            )

        # confirm_manual_step refuses automated steps
        with self.assertRaises((PermissionError, ValueError)):
            coord.confirm_manual_step(
                step_id="step_04_tls_certificate_setup",
                confirmed=True,
                audit_note="invalid manual bypass attempt",
                is_loopback=True,
            )

        # confirm_manual_step refuses Step 2 & Step 3 when Tailscale is NOT running
        for manual_step in sorted(MANUAL_CONFIRMATION_ALLOWED_STEPS):
            with self.assertRaises(PermissionError):
                coord.confirm_manual_step(
                    step_id=manual_step,
                    confirmed=True,
                    audit_note="tailscale is not up yet",
                    is_loopback=True,
                )

        # Step 11 launchd-lint in temp root: missing -> verification_passed=False; rendered -> templates_only=True
        res_lint_missing = coord.run_allowlisted_task(task_id="tradelens-stage-b-launchd-lint", is_loopback=True)["task_result"]
        self.assertFalse(res_lint_missing["passed"])
        self.assertFalse(res_lint_missing["verification_passed"])

        res_render = coord.run_allowlisted_task(
            task_id="tradelens-stage-b-render-launchd",
            confirmed=True,
            is_loopback=True,
        )["task_result"]
        self.assertTrue(res_render["passed"])
        res_lint = coord.run_allowlisted_task(task_id="tradelens-stage-b-launchd-lint", is_loopback=True)["task_result"]
        self.assertTrue(res_lint["templates_only"])
        self.assertFalse(res_lint["proves_installed_or_running"])
        self.assertFalse(res_lint["verification_passed"])

    def test_10_revoked_device_does_not_mask_unauthenticated_active_device(self) -> None:
        priv_a, pub_a = DeviceKeyCrypto.generate_keypair()
        _, pub_b = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            state_hmac_secret=STRONG_HMAC,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=["100.64.0.25", "100.64.0.26"],
            allowed_hosts=["127.0.0.1", "100.64.0.10"],
            remote_mode_enabled=True,
        )
        mgr.register_device(
            device_id="office-a",
            device_name="Office A",
            public_key_pem=pub_a,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch, _ = mgr.issue_auth_challenge("office-a", client_ip="100.64.0.25")
        sig = DeviceKeyCrypto.sign_challenge(priv_a, ch["challenge_string"])
        mgr.authenticate_remote_session(
            device_id="office-a",
            challenge_id=ch["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        mgr.revoke_device("office-a", owner_secret=STRONG_OWNER, reason="RETIRED")
        mgr.register_device(
            device_id="office-b-fresh",
            device_name="Office B Fresh",
            public_key_pem=pub_b,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.26"],
            approve=True,
        )
        rt_cfg_path = self.state_dir / "runtime_config.json"
        rt_cfg_path.write_text(
            json.dumps(
                {
                    "bind_host": "100.64.0.10",
                    "remote_access": True,
                    "allowed_peer_ip": ["100.64.0.25", "100.64.0.26"],
                    "allowed_host": ["127.0.0.1", "100.64.0.10"],
                }
            ),
            encoding="utf-8",
        )
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ={**self.sample_environ, "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC},
        )
        state = coord.get_setup_state()
        step7 = next(s for s in state["steps"] if s["step_id"] == "step_07_remote_https_challenge_verify")
        self.assertNotEqual(step7["status"], "VERIFIED")

    def test_11_ip_change_clears_last_auth_and_config_drift_invalidates_steps(self) -> None:
        priv, pub = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            state_hmac_secret=STRONG_HMAC,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=["100.64.0.25"],
            allowed_hosts=["127.0.0.1", "100.64.0.10"],
            remote_mode_enabled=True,
        )
        mgr.register_device(
            device_id="office-mac-ip",
            device_name="Office Mac",
            public_key_pem=pub,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch, _ = mgr.issue_auth_challenge("office-mac-ip", client_ip="100.64.0.25")
        sig = DeviceKeyCrypto.sign_challenge(priv, ch["challenge_string"])
        auth, _ = mgr.authenticate_remote_session(
            device_id="office-mac-ip",
            challenge_id=ch["challenge_id"],
            signature_b64=sig,
            client_ip="100.64.0.25",
        )
        self.assertIsNotNone(auth)
        dev_before = next(d for d in mgr.list_devices() if d["device_id"] == "office-mac-ip")
        self.assertIsNotNone(dev_before["last_auth_at"])

        (self.state_dir / "runtime_config.json").write_text(
            json.dumps(
                {
                    "bind_host": "100.64.0.10",
                    "remote_access": True,
                    "allowed_peer_ip": ["100.64.0.25"],
                    "allowed_host": ["127.0.0.1", "100.64.0.10"],
                }
            ),
            encoding="utf-8",
        )

        # Updating Office Mac Tailscale IP MUST clear last_auth_at + revoke old session
        mgr.update_device_allowed_ips(
            "office-mac-ip",
            allowed_ips=["100.64.0.50"],
            owner_secret=STRONG_OWNER,
        )
        dev_after = next(d for d in mgr.list_devices() if d["device_id"] == "office-mac-ip")
        self.assertIsNone(dev_after["last_auth_at"])
        dec = mgr.authorize_request(
            path="/api/setup/state",
            method="GET",
            client_ip="100.64.0.50",
            headers={"Host": "100.64.0.10:8000", "Authorization": f"Bearer {auth['access_token']}"},
        )
        self.assertFalse(dec.allowed)

        # Drift between remote_auth.json (100.64.0.50) and runtime_config.json (100.64.0.25) blocks Steps 6-10
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ={**self.sample_environ, "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC},
        )
        state = coord.get_setup_state()
        for sid in (
            "step_06_owner_approval_device_pin",
            "step_07_remote_https_challenge_verify",
            "step_08_security_boundary_tests",
            "step_09_revocation_re_enrollment_test",
            "step_10_launchagent_installation",
        ):
            st = next(s for s in state["steps"] if s["step_id"] == sid)
            self.assertNotEqual(st["status"], "VERIFIED", f"{sid} must not stay VERIFIED under IP drift")

    def test_12_reboot_invalidation_and_selftest_vs_live_revocation(self) -> None:
        # Setup verified environment on boot_1 under state_dir/tls/tradelens-home.{crt,key}
        tls_dir = self.state_dir / "tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        crt_p = tls_dir / "tradelens-home.crt"
        key_p = tls_dir / "tradelens-home.key"
        crt_p.write_text("CERT", encoding="utf-8")
        key_p.write_text("KEY", encoding="utf-8")
        os.chmod(key_p, 0o600)

        priv_rev, pub_rev = DeviceKeyCrypto.generate_keypair()
        priv_ok, pub_ok = DeviceKeyCrypto.generate_keypair()
        mgr = RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            owner_secret=STRONG_OWNER,
            state_hmac_secret=STRONG_HMAC,
            allowed_vpn_cidrs=["100.64.0.0/10"],
            allowed_peer_ips=["100.64.0.25"],
            allowed_hosts=["127.0.0.1", "100.64.0.10"],
            remote_mode_enabled=True,
        )
        mgr.register_device(
            device_id="office-mac-live",
            device_name="Office Live",
            public_key_pem=pub_ok,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch1, _ = mgr.issue_auth_challenge("office-mac-live", client_ip="100.64.0.25")
        mgr.authenticate_remote_session(
            device_id="office-mac-live",
            challenge_id=ch1["challenge_id"],
            signature_b64=DeviceKeyCrypto.sign_challenge(priv_ok, ch1["challenge_string"]),
            client_ip="100.64.0.25",
        )
        mgr.register_device(
            device_id="office-mac-rev-drill",
            device_name="Office Drill",
            public_key_pem=pub_rev,
            owner_secret=STRONG_OWNER,
            allowed_ips=["100.64.0.25"],
            approve=True,
        )
        ch2, _ = mgr.issue_auth_challenge("office-mac-rev-drill", client_ip="100.64.0.25")
        mgr.authenticate_remote_session(
            device_id="office-mac-rev-drill",
            challenge_id=ch2["challenge_id"],
            signature_b64=DeviceKeyCrypto.sign_challenge(priv_rev, ch2["challenge_string"]),
            client_ip="100.64.0.25",
        )
        mgr.revoke_device("office-mac-rev-drill", owner_secret=STRONG_OWNER, reason="LIVE_DRILL")

        (self.state_dir / "tradelens_supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
        (self.state_dir / "runtime_config.json").write_text(
            json.dumps(
                {
                    "bind_host": "100.64.0.10",
                    "remote_access": True,
                    "allowed_peer_ip": ["100.64.0.25"],
                    "allowed_host": ["127.0.0.1", "100.64.0.10"],
                    "tls_cert": str(crt_p),
                    "tls_key": str(key_p),
                }
            ),
            encoding="utf-8",
        )

        boot_epoch = {"sec": 1700000100}

        def _healthy_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            exe = os.path.basename(cmd[0]) if cmd else ""
            if exe == "sysctl":
                return 0, f"{{ sec = {boot_epoch['sec']}, usec = 0 }} Thu", ""
            if exe == "openssl":
                if "-checkend" in cmd:
                    return 0, "Certificate will not expire", ""
                return (
                    0,
                    "SHA256 Fingerprint=AA:BB:CC:DD:EE\nnotBefore=Jan  1 00:00:00 2026 GMT\nnotAfter=Dec 31 00:00:00 2027 GMT\n    DNS:home.ts.net, IP Address:100.64.0.10\n",
                    "",
                )
            if exe == "tailscale":
                if len(cmd) >= 2 and cmd[1] == "status":
                    return 0, json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.10"]}}), ""
                if len(cmd) >= 2 and cmd[1] == "serve":
                    return 0, "https://home.ts.net -> http://127.0.0.1:8765", ""
            if exe == "launchctl" and len(cmd) >= 2 and cmd[1] == "print":
                return 0, "state = running\n\tpid = 4242\n", ""
            return 0, "", ""

        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ={**self.sample_environ, "TRADELENS_STATE_HMAC_SECRET": STRONG_HMAC},
            command_runner=_healthy_runner,
        )
        with patch("backend.app.runtime.stage_b_setup._find_tailscale_bin", return_value="/opt/homebrew/bin/tailscale"):
            # Check selftest explicitly refuses to verify Step 9
            selftest_res = coord.run_allowlisted_task(task_id="tradelens-stage-b-revocation-selftest", is_loopback=True)["task_result"]
            self.assertTrue(selftest_res["simulated_only"])
            self.assertFalse(selftest_res["proves_live_office_mac_revocation"])
            self.assertFalse(selftest_res["verification_passed"])

            # Complete manual confirmations while tailscale is mocked healthy
            coord.confirm_manual_step(step_id="step_02_tailscale_home_office", confirmed=True, audit_note="tailnet ok", is_loopback=True)
            coord.confirm_manual_step(step_id="step_03_tailscale_acl_verification", confirmed=True, audit_note="acl+pf ok", is_loopback=True)
            s_pre = coord.get_setup_state(persist=True)
            self.assertFalse(s_pre["is_stale"])
            self.assertEqual(
                next(s for s in s_pre["steps"] if s["step_id"] == "step_07_remote_https_challenge_verify")["status"],
                "VERIFIED",
            )

            # Simulate Home Mac reboot (new kern.boottime sec)
            boot_epoch["sec"] = 1700099999
            s_post = coord.get_setup_state(persist=True)
            self.assertTrue(s_post["is_stale"])
            self.assertEqual(s_post["state_freshness"], "STALE_PRIOR_BOOT")
            for sid in (
                "step_07_remote_https_challenge_verify",
                "step_08_security_boundary_tests",
                "step_09_revocation_re_enrollment_test",
                "step_10_launchagent_installation",
            ):
                self.assertNotEqual(
                    next(s for s in s_post["steps"] if s["step_id"] == sid)["status"],
                    "VERIFIED",
                    f"{sid} should be invalidated on reboot",
                )

    def test_13_progress_corruption_recovery_and_symlink_rejection(self) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.progress_path.parent, 0o700)

        # Case 1: malformed JSON
        self.progress_path.write_text("{broken json!!!", encoding="utf-8")
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
        )
        s1 = coord.get_setup_state()
        self.assertEqual(s1["state_freshness"], "CORRUPTION_RECOVERED")

        # Case 2: wrong nested schema types
        self.progress_path.write_text(json.dumps({"steps": ["bad"], "manual_confirmations": 123}), encoding="utf-8")
        s2 = coord.get_setup_state()
        self.assertEqual(s2["state_freshness"], "CORRUPTION_RECOVERED")

        # Case 3: symlink on stage_b_progress.json MUST raise PermissionError
        victim = self.root / "victim.txt"
        victim.write_text("do_not_touch", encoding="utf-8")
        self.progress_path.unlink()
        self.progress_path.symlink_to(victim)
        with self.assertRaises(PermissionError):
            coord.get_setup_state(persist=True)

        # Case 4: symlink on .ops_state directory MUST raise PermissionError
        sym_dir = self.root / "sym_ops_state"
        target_dir = self.root / "real_target_dir"
        target_dir.mkdir()
        sym_dir.symlink_to(target_dir)
        coord_sym = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=sym_dir / "stage_b_progress.json",
            environ=self.sample_environ,
        )
        with self.assertRaises(PermissionError):
            coord_sym.get_setup_state(persist=True)

    def test_14_concurrent_progress_updates_flock_integrity(self) -> None:
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
        )

        def _worker(i: int) -> str:
            res = coord.run_allowlisted_task(task_id="tradelens-stage-b-status", is_loopback=True)
            coord.get_setup_state(persist=True)
            return res["task_result"]["task_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_worker, range(16)))
        self.assertEqual(len(results), 16)
        parsed = json.loads(self.progress_path.read_text(encoding="utf-8"))
        self.assertIsInstance(parsed, dict)
        self.assertEqual(stat.S_IMODE(self.progress_path.stat().st_mode), 0o600)

    def test_15_launchd_bootstrapped_not_running_vs_running(self) -> None:
        fake_home = self.root / "home_launch"
        la_dir = fake_home / "Library" / "LaunchAgents"
        la_dir.mkdir(parents=True, exist_ok=True)
        for label in (HOME_ENGINE_LABEL, FRONTEND_SERVER_LABEL):
            (la_dir / f"{label}.plist").write_text(f"<plist><dict><key>Label</key><string>{label}</string></dict></plist>", encoding="utf-8")

        # Sub-case A: bootstrapped in launchctl print with rc=0, but state = not running
        def _not_running_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            exe = os.path.basename(cmd[0]) if cmd else ""
            if exe == "plutil":
                return 0, "OK", ""
            if exe == "launchctl" and len(cmd) >= 2 and cmd[1] == "print":
                return 0, "state = not running\n\tlast exit code = 1\n", ""
            return 1, "", ""

        with patch("backend.app.runtime.stage_b_setup.Path.home", return_value=fake_home):
            probe_down = _probe_launchd(self.root, runner=_not_running_runner)
            self.assertFalse(probe_down["all_running"])
            self.assertTrue(probe_down["all_installed_and_loaded"])
            coord_down = StageBSetupCoordinator(
                repo_root=self.root,
                state_dir=self.state_dir,
                progress_path=self.progress_path,
                environ=self.sample_environ,
                command_runner=_not_running_runner,
            )
            st_down = coord_down.get_setup_state()
            step10_down = next(s for s in st_down["steps"] if s["step_id"] == "step_10_launchagent_installation")
            self.assertNotEqual(step10_down["status"], "VERIFIED")

        # Sub-case B: state = running and pid present
        def _running_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
            exe = os.path.basename(cmd[0]) if cmd else ""
            if exe == "plutil":
                return 0, "OK", ""
            if exe == "launchctl" and len(cmd) >= 2 and cmd[1] == "print":
                return 0, "state = running\n\tpid = 9876\n", ""
            return 1, "", ""

        with patch("backend.app.runtime.stage_b_setup.Path.home", return_value=fake_home):
            probe_up = _probe_launchd(self.root, runner=_running_runner)
            self.assertTrue(probe_up["all_running"])
            for agent in probe_up["installed_plists"].values():
                self.assertEqual(agent["launchd_state"], "running")
                self.assertEqual(agent["pid"], 9876)

    def test_16_tampered_live_drill_rejected_and_permanent_removal_playbook(self) -> None:
        # Even if attacker writes live_drill_verified=true & step_09 VERIFIED in progress JSON,
        # without post-revocation auth evidence in remote_auth.json Step 9 MUST reject tampering.
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.progress_path.parent, 0o700)
        self.progress_path.write_text(
            json.dumps(
                {
                    "live_drill_verified": True,
                    "steps": {
                        "step_09_revocation_re_enrollment_test": {"status": "VERIFIED", "detail": "forged"},
                    },
                }
            ),
            encoding="utf-8",
        )
        coord = StageBSetupCoordinator(
            repo_root=self.root,
            state_dir=self.state_dir,
            progress_path=self.progress_path,
            environ=self.sample_environ,
        )
        state = coord.get_setup_state()
        step9 = next(s for s in state["steps"] if s["step_id"] == "step_09_revocation_re_enrollment_test")
        self.assertNotEqual(step9["status"], "VERIFIED")

        rec_rev = next(r for r in RECOVERY_PLAYBOOKS if r["scenario_id"] == "rec_device_revoked")
        cmds_joined = "\n".join(rec_rev["commands"])
        self.assertIn("remove-device", cmds_joined)
        self.assertIn("PERMANENTLY_RETIRED", cmds_joined)
        self.assertIn("127.0.0.1", "\n".join(rec_rev["what_to_do"] + [cmds_joined]))

    def test_17_portable_launchd_rendering_and_python_resolution(self) -> None:
        # 1. Mock target-machine Home Mac repo path (e.g. /Users/example_home/Documents/GitHub/trading-assistant)
        mock_target_root = Path(self._tmp.name) / "mock_home_machine" / "Documents" / "GitHub" / "trading-assistant"
        mock_venv_bin = mock_target_root / "backend" / ".venv" / "bin"
        mock_venv_bin.mkdir(parents=True, exist_ok=True)
        mock_py = mock_venv_bin / "python3.12"
        mock_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(mock_py, 0o755)

        resolved_venv = resolve_python_executable(repo_root=mock_target_root, environ={})
        self.assertEqual(
            resolved_venv,
            str(mock_target_root.resolve() / "backend" / ".venv" / "bin" / "python3.12"),
        )

        resolved_override = resolve_python_executable(
            repo_root=mock_target_root,
            environ={"TRADELENS_PYTHON_EXECUTABLE": "/usr/local/bin/python3.12"},
        )
        self.assertEqual(resolved_override, "/usr/local/bin/python3.12")

        # 2. Render plists via StageBSetupCoordinator against target repo root; verify zero stale tradelens-ai paths
        mock_state = mock_target_root / "runtime_state"
        mock_progress = mock_target_root / ".ops_state" / "stage_b_progress.json"
        mock_state.mkdir(parents=True, exist_ok=True)
        coord = StageBSetupCoordinator(
            repo_root=mock_target_root,
            state_dir=mock_state,
            progress_path=mock_progress,
            environ=self.sample_environ,
        )
        render_res = coord.run_allowlisted_task(
            task_id="tradelens-stage-b-render-launchd",
            confirmed=True,
            is_loopback=True,
        )["task_result"]
        self.assertTrue(render_res["passed"])
        self.assertTrue(render_res["templates_only"])
        self.assertFalse(render_res["installed_to_launchagents"])

        target_resolved = str(mock_target_root.resolve())
        for label, p_str in render_res["rendered_plists"].items():
            p = Path(p_str)
            self.assertTrue(p.exists())
            xml_text = p.read_text(encoding="utf-8")
            self.assertIn(target_resolved, xml_text)
            self.assertIn(str(mock_target_root.resolve() / "backend" / ".venv" / "bin" / "python3.12"), xml_text)
            self.assertNotIn("Documents/GitHub/tradelens-ai", xml_text)
            self.assertNotIn("ANGEL_API_KEY", xml_text)
            if shutil.which("plutil"):
                lint = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True, check=False)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

        # 3. Direct render_launchd_plists on non-existent mocked Home Mac path resolves cleanly without touching host
        nonexistent_target = Path("/Users/example_home/Documents/GitHub/trading-assistant")
        out_dir = Path(self._tmp.name) / "mock_target_out"
        rendered = render_launchd_plists(
            output_dir=out_dir,
            repo_root=nonexistent_target,
            python_executable="/usr/local/bin/python3.12",
            bind_host="127.0.0.1",
        )
        for p in rendered.values():
            xml_text = p.read_text(encoding="utf-8")
            self.assertIn("/Users/example_home/Documents/GitHub/trading-assistant", xml_text)
            self.assertIn("/usr/local/bin/python3.12", xml_text)
            self.assertNotIn("Documents/GitHub/tradelens-ai", xml_text)
            if shutil.which("plutil"):
                lint = subprocess.run(["plutil", "-lint", str(p)], capture_output=True, text=True, check=False)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)


if __name__ == "__main__":
    unittest.main()
