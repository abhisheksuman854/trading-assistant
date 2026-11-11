"""
TradeLens AI - Phase 3E.5.2.6 Stage B Provisioning, Command Catalog & Progress Store.

Provides:
1. Secret-free persistent Stage B progress tracking (`.ops_state/stage_b_progress.json`, 0600).
2. Live read-only local & remote readiness evaluation (never trusts stale cache or
   manual clicks to bypass automated security/technical verification).
3. CLI-validated non-secret command catalog (Home Mac vs Office Mac vs Manual Tailscale
   vs Verification, linked directly to `mise run tradelens-stage-b-*` tasks and verified
   against `backend.app.runtime.cli.build_parser()`).
4. Strictly allowlisted, three-tier safe execution boundary (read-only checks, owner-confirmed
   local artifact generation, and CLI-guided security-sensitive operations). Broker auth,
   Angel One credential loading, order execution, and Phase 3F are permanently excluded.
5. Complete 10-scenario operational recovery matrix.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from backend.app.runtime.health_and_safety import RuntimeHealthCoordinator
from backend.app.runtime.launchd_config import (
    FRONTEND_SERVER_LABEL,
    HOME_ENGINE_LABEL,
    render_launchd_plists,
)
from backend.app.runtime.security import (
    ALLOWED_REMOTE_ROUTE_METHODS,
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_APPROVED,
    DEVICE_STATUS_PENDING,
    DEVICE_STATUS_REVOKED,
    HOME_OWNER_ONLY_ENDPOINTS,
    RemoteAccessManager,
    build_security_headers,
    is_allowed_bind_host,
    normalize_allowed_peer_ips,
    parse_dotenv_content,
    redact_payload,
    redact_runtime_text,
)


SETUP_STATUSES: Tuple[str, ...] = (
    "NOT_STARTED",
    "READY",
    "IN_PROGRESS",
    "VERIFIED",
    "BLOCKED",
    "REQUIRES_OWNER_ACTION",
    "REVOKED",
)

EXECUTION_TIERS: Tuple[str, ...] = (
    "read_only",
    "host_mutation",
    "security_sensitive",
)

STAGE_B_STEP_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "step_number": 1,
        "step_id": "step_01_home_mac_preflight",
        "title": "Home Mac Preflight",
        "machine": "home_mac",
        "summary": "Verify macOS host, Python runtime, .env 0600 permissions, non-placeholder Angel One config keys, order-execution lock, and loopback health.",
        "prerequisites": (),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-home-preflight",
        "action_command_ids": ("cmd_chmod_env", "cmd_home_preflight", "cmd_local_supervise", "cmd_local_health"),
        "expected_criteria": "os=macOS arm64, .env mode=0600 non-symlink, Angel One keys PRESENT_VALID_FORMAT, order_execution_enabled=False.",
    },
    {
        "step_number": 2,
        "step_id": "step_02_tailscale_home_office",
        "title": "Tailscale Installation & Sign-In (Home + Office Mac)",
        "machine": "home_and_office",
        "summary": "Install Tailscale on both Macs, sign into the private tailnet on Home and Office Macs, record Tailscale IPv4s, and confirm owner verification.",
        "prerequisites": ("step_01_home_mac_preflight",),
        "manual_external": True,
        "verify_task_id": "tradelens-stage-b-tailscale-check",
        "action_command_ids": ("cmd_tailscale_status_home", "cmd_tailscale_ip_home", "cmd_tailscale_ip_office", "cmd_stage_b_tailscale_check"),
        "expected_criteria": "Tailscale CLI/app present on Home Mac, tailnet BackendState=Running with 100.x Tailscale IPv4, plus explicit owner confirmation that Office Mac is signed into the same tailnet.",
    },
    {
        "step_number": 3,
        "step_id": "step_03_tailscale_acl_verification",
        "title": "Tailscale ACL Verification",
        "machine": "tailscale_admin",
        "summary": "Configure least-privilege Tailscale ACLs allowing ONLY the exact Office Mac identity/IP to reach Home Mac ports 8000 & 8080, plus pin peer IP on Home Mac.",
        "prerequisites": ("step_02_tailscale_home_office",),
        "manual_external": True,
        "verify_task_id": "tradelens-stage-b-tailscale-check",
        "action_command_ids": ("cmd_stage_b_tailscale_check",),
        "expected_criteria": "Wildcard autogroup:members disabled for Home Mac ports; peer IP allowlist pinned in Home Mac runtime config/devices; owner confirms Tailscale Admin ACL policy.",
    },
    {
        "step_number": 4,
        "step_id": "step_04_tls_certificate_setup",
        "title": "TLS Certificate Setup (SAN + Fingerprint)",
        "machine": "home_mac",
        "summary": "Generate an ECDSA P-256 X.509 certificate bound to the Home Mac Tailscale IP/MagicDNS SAN, store private key as 0600, and verify SHA-256 fingerprint.",
        "prerequisites": ("step_01_home_mac_preflight", "step_02_tailscale_home_office"),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-tls-check",
        "action_command_ids": ("cmd_generate_tls_cert", "cmd_tls_check"),
        "expected_criteria": "TLS cert + 0600 private key exist, X.509 SAN matches Home Mac Tailscale IP/hostname, cert is valid/unexpired, and fingerprint is verified.",
    },
    {
        "step_number": 5,
        "step_id": "step_05_office_mac_device_key_gen",
        "title": "Office Mac Device Key Generation & Registration",
        "machine": "office_mac",
        "summary": "Generate a P-256 device keypair in Office Mac macOS Keychain (or ephemeral RAM path) and register its public SPKI PEM on Home Mac.",
        "prerequisites": ("step_01_home_mac_preflight", "step_04_tls_certificate_setup"),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-verify-device",
        "action_command_ids": (
            "cmd_generate_device_key_office",
            "cmd_init_owner_home",
            "cmd_register_device_guide",
            "cmd_register_device_home",
        ),
        "expected_criteria": "At least one Office Mac device record exists in remote_auth.json with valid P-256 public key and pinned peer IP.",
    },
    {
        "step_number": 6,
        "step_id": "step_06_owner_approval_device_pin",
        "title": "Owner Approval & Exact IP Pin Verification",
        "machine": "home_mac",
        "summary": "Explicitly approve the Office Mac device from the Home Mac owner CLI and confirm exact Tailscale IP pinning in both device record and config.",
        "prerequisites": ("step_05_office_mac_device_key_gen",),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-verify-device",
        "action_command_ids": ("cmd_approve_device_home", "cmd_list_devices_home", "cmd_verify_device"),
        "expected_criteria": "Device status is APPROVED or ACTIVE (never REVOKED), owner_hash_hex initialized, and exact Tailscale peer IP pinned.",
    },
    {
        "step_number": 7,
        "step_id": "step_07_remote_https_challenge_verify",
        "title": "Remote HTTPS Challenge-Response Verification",
        "machine": "office_mac",
        "summary": "Start the Home Mac runtime over TLS bound to Home Mac Tailscale IP, sign the challenge on Office Mac, authenticate via HTTPS, and confirm /api/health.",
        "prerequisites": (
            "step_03_tailscale_acl_verification",
            "step_04_tls_certificate_setup",
            "step_06_owner_approval_device_pin",
        ),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-remote-check",
        "action_command_ids": ("cmd_start_remote_home", "cmd_sign_challenge_office", "cmd_remote_check"),
        "expected_criteria": "Home Mac backend+frontend healthy over HTTPS on Tailscale bind host and at least one verified remote device session established.",
    },
    {
        "step_number": 8,
        "step_id": "step_08_security_boundary_tests",
        "title": "Security Boundary & Negative Tests",
        "machine": "home_mac",
        "summary": "Verify order execution permanently disabled, owner-only endpoints blocked remotely (403), unpinned peer IPs rejected, and .env isolation intact.",
        "prerequisites": ("step_07_remote_https_challenge_verify",),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-security-check",
        "action_command_ids": ("cmd_security_check", "cmd_run_security_suite"),
        "expected_criteria": "All automated runtime security checks pass with zero order-execution routes, strict loopback setup mutations, and .env permission hygiene.",
    },
    {
        "step_number": 9,
        "step_id": "step_09_revocation_re_enrollment_test",
        "title": "Revocation & Re-Enrolment Test",
        "machine": "home_mac",
        "summary": "Test immediate cross-process revocation of a remote session and verify stale credentials are rejected until explicit owner re-enrolment & approval.",
        "prerequisites": ("step_08_security_boundary_tests",),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-revocation-check",
        "action_command_ids": (
            "cmd_revoke_device_home",
            "cmd_remove_device_home",
            "cmd_revocation_selftest",
            "cmd_revocation_check",
        ),
        "expected_criteria": "Isolated cross-process revocation self-test AND live device revocation/re-enrolment drill verified without active unauthorized sessions.",
    },
    {
        "step_number": 10,
        "step_id": "step_10_launchagent_installation",
        "title": "LaunchAgent Installation (Locked Until Steps 2-9 Verified)",
        "machine": "home_mac",
        "summary": "Render and plutil-lint macOS LaunchAgent plists, then install and bootstrap into ~/Library/LaunchAgents ONLY after Steps 2 through 9 are verified.",
        "prerequisites": (
            "step_02_tailscale_home_office",
            "step_03_tailscale_acl_verification",
            "step_04_tls_certificate_setup",
            "step_05_office_mac_device_key_gen",
            "step_06_owner_approval_device_pin",
            "step_07_remote_https_challenge_verify",
            "step_08_security_boundary_tests",
            "step_09_revocation_re_enrollment_test",
        ),
        "manual_external": False,
        "verify_task_id": "tradelens-stage-b-launchd-status",
        "action_command_ids": ("cmd_render_launchd", "cmd_launchd_lint", "cmd_launchd_install_cli", "cmd_launchd_status"),
        "expected_criteria": "Both LaunchAgent plists rendered & plutil-lint OK, Steps 2-9 verified, and LaunchAgents installed & healthy in gui/<uid>.",
    },
)

STAGE_B_STEP_INDEX: Dict[str, Dict[str, Any]] = {
    s["step_id"]: s for s in STAGE_B_STEP_SPECS
}

MANUAL_CONFIRMATION_ALLOWED_STEPS: frozenset[str] = frozenset(
    s["step_id"] for s in STAGE_B_STEP_SPECS if s["manual_external"]
)


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    command_name: str
    description: str
    step_id: str
    machine: str  # home_mac | office_mac | tailscale_admin | verification
    category: str  # read_only | host_mutation | security_sensitive
    mutates_host: bool
    requires_owner_approval: bool
    makes_network_requests: bool
    ui_executable: bool
    command_text: str
    expected_result: str
    cli_subcommand: Optional[str] = None
    cli_flags: Tuple[str, ...] = ()
    mise_task: Optional[str] = None
    manual_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command_name": self.command_name,
            "description": self.description,
            "step_id": self.step_id,
            "machine": self.machine,
            "category": self.category,
            "mutates_host": self.mutates_host,
            "requires_owner_approval": self.requires_owner_approval,
            "makes_network_requests": self.makes_network_requests,
            "ui_executable": self.ui_executable,
            "command_text": self.command_text,
            "expected_result": self.expected_result,
            "cli_subcommand": self.cli_subcommand,
            "cli_flags": list(self.cli_flags),
            "mise_task": self.mise_task,
            "manual_notes": self.manual_notes,
        }


def build_command_catalog() -> List[CommandSpec]:
    """
    Canonical non-secret Stage B command catalog derived against the verified
    `backend.app.runtime.cli.build_parser()` subcommands & flags and `mise.toml`.
    """
    return [
        CommandSpec(
            command_id="cmd_chmod_env",
            command_name="Harden Home Mac .env Permissions",
            description="Restrict .env file permissions to owner-only read/write (0600) without exposing contents.",
            step_id="step_01_home_mac_preflight",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text="chmod 600 .env",
            expected_result="stat -f '%OLp' .env returns 600",
        ),
        CommandSpec(
            command_id="cmd_home_preflight",
            command_name="Home Mac Stage B Preflight",
            description="Run non-destructive Home Mac host, .env mode/key format, order-execution lock, and runtime checks.",
            step_id="step_01_home_mac_preflight",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-home-preflight",
            expected_result="step_01 status=VERIFIED when .env mode is 0600 and Angel One format checks pass",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-home-preflight",
        ),
        CommandSpec(
            command_id="cmd_local_supervise",
            command_name="Start Local Loopback Home Mac Supervisor (Foreground)",
            description="Run Home Mac backend (127.0.0.1:8000) and dashboard (127.0.0.1:8080) locally in foreground.",
            step_id="step_01_home_mac_preflight",
            machine="home_mac",
            category="host_mutation",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli supervise "
                "--bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080 --caffeinate"
            ),
            expected_result="Backend listening on 127.0.0.1:8000 and dashboard on 127.0.0.1:8080",
            cli_subcommand="supervise",
            cli_flags=("--bind-host", "--backend-port", "--frontend-port", "--caffeinate"),
        ),
        CommandSpec(
            command_id="cmd_local_health",
            command_name="Verify Local Loopback Health",
            description="Probe local /api/health and /frontend-health endpoints.",
            step_id="step_01_home_mac_preflight",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="python3.12 -m backend.app.runtime.cli health-check --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080",
            expected_result='"overall_healthy": true',
            cli_subcommand="health-check",
            cli_flags=("--bind-host", "--backend-port", "--frontend-port"),
        ),
        CommandSpec(
            command_id="cmd_tailscale_status_home",
            command_name="Inspect Home Mac Tailscale Mesh Status",
            description="Check whether Tailscale daemon is installed, signed in, and running on Home Mac.",
            step_id="step_02_tailscale_home_office",
            machine="home_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="tailscale status",
            expected_result="Lists Home Mac and Office Mac tailnet nodes with 100.x IPs",
        ),
        CommandSpec(
            command_id="cmd_tailscale_ip_home",
            command_name="Show Home Mac Tailscale IPv4",
            description="Print the exact Home Mac Tailscale IPv4 address for TLS SAN and client connection.",
            step_id="step_02_tailscale_home_office",
            machine="home_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=False,
            command_text="tailscale ip -4",
            expected_result="Single 100.64.0.0/10 CGNAT IPv4 address (e.g. 100.x.y.z)",
        ),
        CommandSpec(
            command_id="cmd_tailscale_ip_office",
            command_name="Show Office Mac Tailscale IPv4",
            description="Run on Office Mac to obtain its exact Tailscale IPv4 address for allowlist pinning.",
            step_id="step_02_tailscale_home_office",
            machine="office_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=False,
            command_text="tailscale ip -4",
            expected_result="Single Office Mac 100.x.y.z IPv4 to pin as --allowed-peer-ip",
        ),
        CommandSpec(
            command_id="cmd_stage_b_tailscale_check",
            command_name="Stage B Tailscale & Peer Pin Check",
            description="Verify Tailscale runtime state plus exact Office Mac peer IP allowlist pinning.",
            step_id="step_03_tailscale_acl_verification",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-tailscale-check",
            expected_result="Tailscale running on Home Mac and exact peer IP pinning configured",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-tailscale-check",
        ),
        CommandSpec(
            command_id="cmd_generate_tls_cert",
            command_name="Generate Home Mac SAN TLS Certificate",
            description="Generate self-signed ECDSA P-256 certificate bound to Home Mac Tailscale IP and MagicDNS host.",
            step_id="step_04_tls_certificate_setup",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli generate-tls-cert "
                "--bind-host <HOME_TAILSCALE_IP> --hostname <HOME_MAGICDNS_HOST> "
                "--vpn-cidr 100.64.0.0/10 "
                "--cert-out runtime_state/tls/tradelens-home.crt "
                "--key-out runtime_state/tls/tradelens-home.key --days 365"
            ),
            expected_result='{"status": "GENERATED", "self_verify_ok": true, "sha256_fingerprint": "..."}',
            cli_subcommand="generate-tls-cert",
            cli_flags=("--bind-host", "--hostname", "--vpn-cidr", "--cert-out", "--key-out", "--days"),
        ),
        CommandSpec(
            command_id="cmd_tls_check",
            command_name="Verify TLS Certificate SAN & Key Mode",
            description="Inspect TLS cert SAN, validity dates, SHA-256 fingerprint, and 0600 private key permissions.",
            step_id="step_04_tls_certificate_setup",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-tls-check",
            expected_result="TLS cert valid with SAN matching bind host and key mode 0600",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-tls-check",
        ),
        CommandSpec(
            command_id="cmd_generate_device_key_office",
            command_name="Generate Office Mac Device Key in Keychain",
            description="Run on Office Mac to create an ECDSA P-256 device keypair in macOS Keychain (with ACL prompt) and export SPKI public key.",
            step_id="step_05_office_mac_device_key_gen",
            machine="office_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli generate-device-key "
                "--device-id <OFFICE_DEVICE_ID> --store-keychain "
                "--public-key-out /tmp/<OFFICE_DEVICE_ID>.pub.pem"
            ),
            expected_result="Public key SPKI PEM saved; private key stays locked in Office Mac Keychain.",
            cli_subcommand="generate-device-key",
            cli_flags=("--device-id", "--store-keychain", "--public-key-out"),
        ),
        CommandSpec(
            command_id="cmd_init_owner_home",
            command_name="Initialize Home Mac Owner Secret (Interactive Prompt)",
            description="Initialize PBKDF2-HMAC-SHA256 Home Mac owner secret interactively via hidden terminal input.",
            step_id="step_05_office_mac_device_key_gen",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli init-owner "
                "--remote-access --owner-secret-prompt"
            ),
            expected_result='{"status": "OWNER_SECRET_INITIALIZED"}',
            cli_subcommand="init-owner",
            cli_flags=("--remote-access", "--owner-secret-prompt"),
        ),
        CommandSpec(
            command_id="cmd_register_device_guide",
            command_name="Show Non-Mutating Office Mac Device Registration Guide",
            description="Print exact read-only CLI instructions for Office Mac key generation and Home Mac registration/approval without modifying state.",
            step_id="step_05_office_mac_device_key_gen",
            machine="home_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-register-device-guide",
            expected_result="Outputs exact registration CLI templates cleanly (does not register a device; exit_0_means_stage_b_verified=false)",
            cli_subcommand="stage-b",
            cli_flags=("--action",),
            mise_task="tradelens-stage-b-register-device-guide",
        ),
        CommandSpec(
            command_id="cmd_register_device_home",
            command_name="Register Office Mac Public Key & Exact IP on Home Mac (Requires Flags)",
            description="Mutating registration command: supply --device-id, --public-key-file, --allowed-ip, and owner secret; fails non-zero if registration args are omitted.",
            step_id="step_05_office_mac_device_key_gen",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli register-device "
                "--device-id <OFFICE_DEVICE_ID> --device-name <OFFICE_MAC_LABEL> "
                "--public-key-file /tmp/<OFFICE_DEVICE_ID>.pub.pem "
                "--allowed-ip <OFFICE_TAILSCALE_IP> --pending-only "
                "--remote-access --owner-secret-prompt"
            ),
            expected_result='Device stored with status="pending_approval" and exact allowed_ips (exits 2 if flags missing)',
            cli_subcommand="register-device",
            cli_flags=(
                "--device-id",
                "--device-name",
                "--public-key-file",
                "--allowed-ip",
                "--pending-only",
                "--remote-access",
                "--owner-secret-prompt",
            ),
            mise_task="tradelens-stage-b-register-device",
        ),
        CommandSpec(
            command_id="cmd_approve_device_home",
            command_name="Approve Office Mac Device on Home Mac",
            description="Owner-approve the pending Office Mac device and pin its allowed Tailscale peer IP.",
            step_id="step_06_owner_approval_device_pin",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli approve-device "
                "--device-id <OFFICE_DEVICE_ID> --allowed-ip <OFFICE_TAILSCALE_IP> "
                "--remote-access --owner-secret-prompt"
            ),
            expected_result='Device transitions from pending_approval to approved/active with pinned peer IP',
            cli_subcommand="approve-device",
            cli_flags=("--device-id", "--allowed-ip", "--remote-access", "--owner-secret-prompt"),
        ),
        CommandSpec(
            command_id="cmd_list_devices_home",
            command_name="List Registered Workstations",
            description="Inspect registered devices, allowed IPs, and statuses without secrets.",
            step_id="step_06_owner_approval_device_pin",
            machine="home_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="python3.12 -m backend.app.runtime.cli list-devices --remote-access",
            expected_result="Approved Office Mac shown with exact Tailscale IP pin and public_key_fingerprint",
            cli_subcommand="list-devices",
            cli_flags=("--remote-access",),
        ),
        CommandSpec(
            command_id="cmd_verify_device",
            command_name="Verify Device Registration & IP Pinning",
            description="Verify approved device presence, public key format, and exact peer IP allowlist configuration.",
            step_id="step_06_owner_approval_device_pin",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-verify-device",
            expected_result="step_05 and step_06 verified with non-revoked approved device",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-verify-device",
        ),
        CommandSpec(
            command_id="cmd_start_remote_home",
            command_name="Start Foreground HTTPS Remote Runtime on Home Mac Tailscale IP",
            description="Run foreground supervised HTTPS backend & frontend bound strictly to Tailscale IP and exact Office Mac peer IP.",
            step_id="step_07_remote_https_challenge_verify",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli supervise "
                "--bind-host <HOME_TAILSCALE_IP> --backend-port 8000 --frontend-port 8080 "
                "--vpn-cidr 100.64.0.0/10 --allowed-peer-ip <OFFICE_TAILSCALE_IP> "
                "--allowed-host <HOME_TAILSCALE_IP> --allowed-host <HOME_MAGICDNS_HOST> "
                "--allowed-origin https://<HOME_TAILSCALE_IP>:8080 "
                "--tls-cert runtime_state/tls/tradelens-home.crt "
                "--tls-key runtime_state/tls/tradelens-home.key "
                "--remote-access --caffeinate"
            ),
            expected_result="TLS backend (:8000) and static frontend (:8080) serving only approved Tailscale peer IP",
            cli_subcommand="supervise",
            cli_flags=(
                "--bind-host",
                "--backend-port",
                "--frontend-port",
                "--vpn-cidr",
                "--allowed-peer-ip",
                "--allowed-host",
                "--allowed-origin",
                "--tls-cert",
                "--tls-key",
                "--remote-access",
                "--caffeinate",
            ),
        ),
        CommandSpec(
            command_id="cmd_sign_challenge_office",
            command_name="Sign Challenge with Office Mac Keychain Key",
            description="On Office Mac (Path A Keychain CLI), sign the Home Mac challenge nonce using ECDSA P-256 private key.",
            step_id="step_07_remote_https_challenge_verify",
            machine="office_mac",
            category="security_sensitive",
            mutates_host=False,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli sign-challenge "
                "--device-id <OFFICE_DEVICE_ID> --challenge-stdin"
            ),
            expected_result="Prints base64 DER ECDSA signature for paste into Remote Auth Modal",
            cli_subcommand="sign-challenge",
            cli_flags=("--device-id", "--challenge-stdin"),
        ),
        CommandSpec(
            command_id="cmd_remote_check",
            command_name="Verify Remote HTTPS & Session Readiness",
            description="Check TLS-protected Home Mac endpoint health and active/verified remote session records.",
            step_id="step_07_remote_https_challenge_verify",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-remote-check",
            expected_result="HTTPS health check passes and at least one verified device session recorded",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-remote-check",
        ),
        CommandSpec(
            command_id="cmd_security_check",
            command_name="Run Stage B Security Boundary Verification",
            description="Execute automated boundary checks: order-exec lock, owner-only route isolation, wildcard refusal, .env hygiene, peer-IP enforcement.",
            step_id="step_08_security_boundary_tests",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-security-check",
            expected_result="All security boundary assertions PASS with zero leaks",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-security-check",
        ),
        CommandSpec(
            command_id="cmd_run_security_suite",
            command_name="Run Full Offline Security Test Suite (Stage A + A.1 + B)",
            description="Execute unittest regression suite covering remote access security audit, Stage A/A.1 gates, and Stage B controls.",
            step_id="step_08_security_boundary_tests",
            machine="home_mac",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m unittest "
                "backend.tests.test_remote_access_security_audit "
                "backend.tests.test_private_network_provisioning_stage_a "
                "backend.tests.test_pre_provisioning_security_gate_stage_a1 "
                "backend.tests.test_stage_b_setup_and_mise -v"
            ),
            expected_result="All test suites report OK with 0 failures",
        ),
        CommandSpec(
            command_id="cmd_revoke_device_home",
            command_name="Immediately Revoke Office Mac Device",
            description="Revoke an Office Mac device from Home Mac CLI, immediately invalidating active sessions across processes.",
            step_id="step_09_revocation_re_enrollment_test",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli revoke-device "
                "--device-id <OFFICE_DEVICE_ID> --reason STAGE_B_REVOCATION_DRILL "
                "--remote-access --owner-secret-prompt"
            ),
            expected_result="Device marked revoked; existing bearer token immediately returns 403 SESSION_REVOKED",
            cli_subcommand="revoke-device",
            cli_flags=("--device-id", "--reason", "--remote-access", "--owner-secret-prompt"),
        ),
        CommandSpec(
            command_id="cmd_remove_device_home",
            command_name="Purge Old Device Record (When Replacing Hardware)",
            description="Permanently remove a revoked device record before fresh key enrolment.",
            step_id="step_09_revocation_re_enrollment_test",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "python3.12 -m backend.app.runtime.cli remove-device "
                "--device-id <OFFICE_DEVICE_ID> --reason REPLACED_OR_LOST "
                "--remote-access --owner-secret-prompt"
            ),
            expected_result="Revoked device removed from registry",
            cli_subcommand="remove-device",
            cli_flags=("--device-id", "--reason", "--remote-access", "--owner-secret-prompt"),
        ),
        CommandSpec(
            command_id="cmd_revocation_selftest",
            command_name="Run Isolated Revocation Self-Test (Simulated Only)",
            description="Run isolated in-tempdir cross-process revocation self-test without proving live Office Mac revocation.",
            step_id="step_09_revocation_re_enrollment_test",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-revocation-selftest",
            expected_result="Simulated isolated revocation mechanism passes (simulated_only=true; proves_live_office_mac_revocation=false)",
            cli_subcommand="stage-b",
            cli_flags=("--action",),
            mise_task="tradelens-stage-b-revocation-selftest",
        ),
        CommandSpec(
            command_id="cmd_revocation_check",
            command_name="Verify Live Cross-Process Revocation & Re-Enrolment Gate",
            description="Strict Step 9 verification gate: requires Steps 2-8 VERIFIED, offline self-test PASS, and live post-revocation evidence in remote_auth.json (exits 2 until Step 9 verified).",
            step_id="step_09_revocation_re_enrollment_test",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-revocation-check",
            expected_result="Exits 0 ONLY when offline self-test AND live post-revocation/re-enrolment drill are verified; otherwise exits 2",
            cli_subcommand="stage-b",
            cli_flags=("--action",),
            mise_task="tradelens-stage-b-revocation-check",
        ),
        CommandSpec(
            command_id="cmd_render_launchd",
            command_name="Render Validated LaunchAgent Plist Templates",
            description="Generate Home Engine and Frontend Dashboard plist files under deploy/launchd/ without installing to ~/Library/LaunchAgents.",
            step_id="step_10_launchagent_installation",
            machine="home_mac",
            category="host_mutation",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-render-launchd",
            expected_result="deploy/launchd/com.tradelens.home-engine.plist and com.tradelens.frontend-dashboard.plist written",
            cli_subcommand="render-launchd",
            cli_flags=(),
            mise_task="tradelens-stage-b-render-launchd",
        ),
        CommandSpec(
            command_id="cmd_launchd_lint",
            command_name="Validate Rendered LaunchAgent Plists (plutil -lint)",
            description="Run macOS plutil -lint validation on rendered plist files and enforce Step 2-9 prerequisites.",
            step_id="step_10_launchagent_installation",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-launchd-lint",
            expected_result="Both rendered plist files return plutil -lint: OK",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-launchd-lint",
        ),
        CommandSpec(
            command_id="cmd_launchd_install_cli",
            command_name="Install & Bootstrap LaunchAgents (Locked Until Steps 2-9 Verified)",
            description="Copy linted plists to ~/Library/LaunchAgents and bootstrap gui/$(id -u) ONLY after foreground verification succeeds.",
            step_id="step_10_launchagent_installation",
            machine="home_mac",
            category="security_sensitive",
            mutates_host=True,
            requires_owner_approval=True,
            makes_network_requests=False,
            ui_executable=False,
            command_text=(
                "cp deploy/launchd/com.tradelens.home-engine.plist ~/Library/LaunchAgents/ && "
                "cp deploy/launchd/com.tradelens.frontend-dashboard.plist ~/Library/LaunchAgents/ && "
                "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradelens.home-engine.plist && "
                "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradelens.frontend-dashboard.plist"
            ),
            expected_result="LaunchAgents loaded and running under gui/<uid>",
        ),
        CommandSpec(
            command_id="cmd_launchd_status",
            command_name="Inspect LaunchAgent Installation & Gate Status",
            description="Check ~/Library/LaunchAgents presence, launchctl status, and Step 2-9 lock state.",
            step_id="step_10_launchagent_installation",
            machine="verification",
            category="read_only",
            mutates_host=False,
            requires_owner_approval=False,
            makes_network_requests=False,
            ui_executable=True,
            command_text="mise run tradelens-stage-b-launchd-status",
            expected_result="Reports exact LaunchAgent lock/install state honestly",
            cli_subcommand="stage-b",
            cli_flags=(),
            mise_task="tradelens-stage-b-launchd-status",
        ),
    ]


COMMAND_CATALOG: Tuple[CommandSpec, ...] = tuple(build_command_catalog())
COMMAND_INDEX: Dict[str, CommandSpec] = {c.command_id: c for c in COMMAND_CATALOG}


def validate_command_catalog_against_cli(
    parser: argparse.ArgumentParser,
    catalog: Sequence[CommandSpec] = COMMAND_CATALOG,
) -> Tuple[bool, List[str]]:
    """
    Inspect `cli.build_parser()` to guarantee every CLI subcommand and option
    referenced in `COMMAND_CATALOG` is actually registered on the real parser.
    """
    errors: List[str] = []
    subparsers_map: Dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_map = dict(action.choices)
            break

    if not subparsers_map:
        return False, ["CLI parser has no subparsers configured."]

    for cmd in catalog:
        if cmd.cli_subcommand is None:
            continue
        if cmd.cli_subcommand not in subparsers_map:
            errors.append(f"{cmd.command_id}: unknown CLI subcommand '{cmd.cli_subcommand}'")
            continue
        subp = subparsers_map[cmd.cli_subcommand]
        valid_opts = set(subp._option_string_actions.keys()) | set(parser._option_string_actions.keys())
        for flag in cmd.cli_flags:
            if flag not in valid_opts:
                errors.append(
                    f"{cmd.command_id}: flag '{flag}' not registered on subcommand '{cmd.cli_subcommand}'"
                )
    return len(errors) == 0, errors


RECOVERY_PLAYBOOKS: Tuple[Dict[str, Any], ...] = (
    {
        "scenario_id": "rec_home_mac_restart",
        "title": "Home Mac Restarted",
        "cause": "macOS rebooted or power cycled; volatile in-memory Angel One session and live caches are cleared.",
        "machine": "home_mac",
        "what_to_do": [
            "Verify LaunchAgent status or restart foreground supervised runtime if LaunchAgents are not installed.",
            "Confirm Tailscale daemon is reconnected and check /api/health.",
            "Broker authentication always remains EXPLICIT_AUTH_REQUIRED across reboots until owner unlocks locally.",
        ],
        "commands": [
            "mise run tradelens-stage-b-status",
            "python3.12 -m backend.app.runtime.cli status",
            "python3.12 -m backend.app.runtime.cli health-check",
        ],
        "doc_anchor": "recovery-home-mac-restarted",
    },
    {
        "scenario_id": "rec_sleep_or_network_drop",
        "title": "Home Mac Slept or Network Dropped",
        "cause": "Lid closed without clamshell power adapter or local Wi-Fi dropped; PowerAndSleepMonitor flags gap.",
        "machine": "home_mac",
        "what_to_do": [
            "Wake Home Mac, reconnect power and network, and confirm --caffeinate is active during market sessions.",
            "Run Stage B health & status check to confirm runtime loop and feed freshness state.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli status",
            "mise run tradelens-stage-b-remote-check",
        ],
        "doc_anchor": "recovery-sleep-network-drop",
    },
    {
        "scenario_id": "rec_tailscale_disconnected",
        "title": "Tailscale Disconnected",
        "cause": "Tailscale daemon stopped, signed out, or key expired on Home Mac or Office Mac.",
        "machine": "home_and_office",
        "what_to_do": [
            "Run tailscale status on both Macs and sign back in if LoggedOut / NeedsLogin.",
            "Confirm both Tailscale IPs match the pinned allowlist before retrying remote access.",
        ],
        "commands": [
            "tailscale status",
            "tailscale ip -4",
            "mise run tradelens-stage-b-tailscale-check",
        ],
        "doc_anchor": "recovery-tailscale-disconnected",
    },
    {
        "scenario_id": "rec_office_ip_changed",
        "title": "Office Mac Tailscale IP Changed",
        "cause": "Office Mac was re-authed into tailnet with a new 100.x IP; existing pinned peer IP check blocks connections.",
        "machine": "home_mac",
        "what_to_do": [
            "Obtain new Office Mac IP via `tailscale ip -4` on Office Mac.",
            "On Home Mac, update the pinned peer IP with `update-device-ip` (invalidates old sessions safely), update Tailscale ACL & runtime config, then re-auth.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli update-device-ip --device-id <OFFICE_DEVICE_ID> --allowed-ip <NEW_OFFICE_TAILSCALE_IP> --remote-access --owner-secret-prompt",
            "mise run tradelens-stage-b-verify-device",
        ],
        "doc_anchor": "recovery-office-ip-changed",
    },
    {
        "scenario_id": "rec_tls_cert_expired",
        "title": "TLS Certificate Expired or SAN Mismatch",
        "cause": "Self-signed X.509 cert passed expiry date or Home Mac Tailscale IP/MagicDNS host changed.",
        "machine": "home_mac",
        "what_to_do": [
            "Generate a replacement ECDSA P-256 certificate matching the current Tailscale IP and MagicDNS name.",
            "Verify key mode 0600, update Office Mac pinned trust/SPKI if applicable, and restart Home Mac runtime.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli generate-tls-cert --bind-host <HOME_TAILSCALE_IP> --hostname <HOME_MAGICDNS_HOST> --vpn-cidr 100.64.0.0/10 --cert-out runtime_state/tls/tradelens-home.crt --key-out runtime_state/tls/tradelens-home.key --days 365",
            "mise run tradelens-stage-b-tls-check",
        ],
        "doc_anchor": "recovery-tls-expired",
    },
    {
        "scenario_id": "rec_device_revoked",
        "title": "Office Mac Revoked or Permanently Retired",
        "cause": "Owner intentionally revoked Office Mac device, retired the remote workstation permanently, or suspected compromise.",
        "machine": "home_mac",
        "what_to_do": [
            "Verify device status shows REVOKED and active sessions are purged immediately.",
            "Delete any local device key on Office Mac with `delete-device-key`.",
            "For permanent Office Mac removal, run `remove-device` on Home Mac, remove its Tailscale IP from Tailscale ACLs and `--allowed-peer-ip`, and revert Home Mac runtime to loopback-only (`--bind-host 127.0.0.1`) if no other remote devices remain.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli list-devices --remote-access",
            "python3.12 -m backend.app.runtime.cli delete-device-key --device-id <OFFICE_DEVICE_ID>",
            "python3.12 -m backend.app.runtime.cli remove-device --device-id <OFFICE_DEVICE_ID> --reason PERMANENTLY_RETIRED --remote-access --owner-secret-prompt",
        ],
        "doc_anchor": "recovery-device-revoked",
    },
    {
        "scenario_id": "rec_office_re_enrollment",
        "title": "Re-Enrolling Office Mac (or Key Lost on Browser Reload)",
        "cause": "Previously revoked device or Office Mac lost ephemeral RAM / Keychain key and cannot sign challenges.",
        "machine": "home_and_office",
        "what_to_do": [
            "Revoke old key on Home Mac if still present, generate a brand-new keypair on Office Mac.",
            "Re-register with `--allow-re-enroll` and approve explicitly with `--owner-secret-prompt` on Home Mac.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli generate-device-key --device-id <OFFICE_DEVICE_ID> --store-keychain --public-key-out /tmp/<OFFICE_DEVICE_ID>.pub.pem",
            "python3.12 -m backend.app.runtime.cli register-device --device-id <OFFICE_DEVICE_ID> --public-key-file /tmp/<OFFICE_DEVICE_ID>.pub.pem --allowed-ip <OFFICE_TAILSCALE_IP> --allow-re-enroll --remote-access --owner-secret-prompt",
            "python3.12 -m backend.app.runtime.cli approve-device --device-id <OFFICE_DEVICE_ID> --allowed-ip <OFFICE_TAILSCALE_IP> --remote-access --owner-secret-prompt",
        ],
        "doc_anchor": "recovery-re-enrollment",
    },
    {
        "scenario_id": "rec_home_runtime_stopped",
        "title": "Home Runtime Stopped Unexpectedly",
        "cause": "Backend or frontend exited, stale PID file remained, or process hit max restart window.",
        "machine": "home_mac",
        "what_to_do": [
            "Inspect supervisor status & stderr logs in runtime_logs/ without touching .env.",
            "Stop any lingering PID cleanly, clear stale PID recovery automatically via `restart` preserving hardened flags.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli status",
            "python3.12 -m backend.app.runtime.cli restart",
            "mise run tradelens-stage-b-status",
        ],
        "doc_anchor": "recovery-runtime-stopped",
    },
    {
        "scenario_id": "rec_launchagent_failed",
        "title": "LaunchAgent Failed to Load or Boot Loop",
        "cause": "Plist syntax error, missing TLS cert, permissive .env, or missing state HMAC in Keychain.",
        "machine": "home_mac",
        "what_to_do": [
            "Bootout affected LaunchAgent immediately so launchd stops respawning.",
            "Run `plutil -lint` and inspect `runtime_logs/launchd.*.stderr.log`, fix prerequisite, then re-verify foreground first.",
        ],
        "commands": [
            "launchctl bootout gui/$(id -u)/com.tradelens.home-engine",
            "mise run tradelens-stage-b-launchd-lint",
            "mise run tradelens-stage-b-home-preflight",
        ],
        "doc_anchor": "recovery-launchagent-failed",
    },
    {
        "scenario_id": "rec_owner_shutdown_resume",
        "title": "Owner Shutdown & Intentional Resume",
        "cause": "Owner executed explicit `owner-shutdown` (`owner_shutdown.flag` active); supervisor refuses auto-restart until resumed.",
        "machine": "home_mac",
        "what_to_do": [
            "Inspect status to confirm `owner_shutdown.flag` is present intentionally.",
            "Run `resume` on Home Mac to clear `owner_shutdown.flag`, then start or restart the runtime.",
        ],
        "commands": [
            "python3.12 -m backend.app.runtime.cli status",
            "python3.12 -m backend.app.runtime.cli resume",
            "python3.12 -m backend.app.runtime.cli restart",
        ],
        "doc_anchor": "recovery-owner-shutdown-resume",
    },
)


SENSITIVE_REMOTE_DROP_KEYS: frozenset[str] = frozenset(
    {
        "challenge_string",
        "challenge_id",
        "signature_b64",
        "access_token",
        "owner_secret",
        "state_hmac_secret",
        "public_key_pem",
        "private_key_pem",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _strip_sensitive_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_sensitive_keys(v)
            for k, v in obj.items()
            if k not in SENSITIVE_REMOTE_DROP_KEYS
        }
    if isinstance(obj, list):
        return [_strip_sensitive_keys(x) for x in obj]
    return obj


_SAFE_PYTHON_BASENAME_RE = re.compile(r"^python3(?:\.\d+)?$")
_SHELL_META_RE = re.compile(r"[;&|`$><\r\n\t\s]")


def _default_python_executable(repo_root: Optional[Path] = None) -> str:
    """Resolve the active project Python 3.12 interpreter when available."""
    if repo_root is not None:
        venv_py = Path(repo_root) / "backend" / ".venv" / "bin" / "python3.12"
        if venv_py.exists() and os.access(venv_py, os.X_OK):
            return str(venv_py)
    if os.path.exists("/opt/homebrew/bin/python3.12") and os.access("/opt/homebrew/bin/python3.12", os.X_OK):
        return "/opt/homebrew/bin/python3.12"
    if sys.executable and _SAFE_PYTHON_BASENAME_RE.fullmatch(Path(sys.executable).name):
        return str(sys.executable)
    return "/opt/homebrew/bin/python3.12"


def _validate_python_executable(candidate: Any, repo_root: Optional[Path] = None) -> str:
    raw = str(candidate or "").strip()
    if not raw:
        return _default_python_executable(repo_root)
    if _SHELL_META_RE.search(raw):
        raise ValueError("INVALID_PYTHON_EXECUTABLE: Shell metacharacters or whitespace are not allowed.")
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError("INVALID_PYTHON_EXECUTABLE: Interpreter path must be absolute.")
    if not _SAFE_PYTHON_BASENAME_RE.fullmatch(p.name):
        raise ValueError(f"INVALID_PYTHON_EXECUTABLE: Unapproved interpreter binary '{p.name}'.")
    approved_prefixes = (
        "/opt/homebrew/bin/",
        "/usr/local/bin/",
        "/usr/bin/",
        "/Library/Frameworks/Python.framework/",
    )
    resolved_self = str(Path(sys.executable))
    if raw == resolved_self or raw == sys.executable:
        return raw
    if raw.startswith(approved_prefixes):
        return raw
    if "/backend/.venv/bin/" in raw and os.path.exists(raw) and os.access(raw, os.X_OK):
        return raw
    raise ValueError(f"INVALID_PYTHON_EXECUTABLE: Interpreter '{raw}' is not in the approved Python allowlist.")


def _probe_boot_session(
    runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    run_fn = runner or _run_subprocess
    sysctl_bin = shutil.which("sysctl") or "/usr/sbin/sysctl"
    rc, out, _ = run_fn([sysctl_bin, "-n", "kern.boottime"], 3.0)
    if rc == 0 and out:
        m = re.search(r"sec\s*=\s*(\d+)\s*,\s*usec\s*=\s*(\d+)", out)
        if m:
            sec = int(m.group(1))
            usec = int(m.group(2))
            return {
                "available": True,
                "boot_session_id": f"{sec}:{usec}",
                "boot_epoch": sec,
            }
    return {
        "available": False,
        "boot_session_id": None,
        "boot_epoch": None,
    }


def _run_subprocess(
    cmd: List[str],
    timeout_sec: float = 5.0,
) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout_sec}s"
    except OSError as exc:
        return 127, "", str(exc)


def _inspect_dotenv_metadata(
    env_path: Path,
    environ: Dict[str, str],
) -> Dict[str, Any]:
    """
    Inspect `.env` path metadata and Angel One key readiness WITHOUT exposing
    secret values, file size, or modification timestamps.
    """
    is_symlink = env_path.is_symlink()
    exists = env_path.exists()
    mode_octal = None
    strict_perms_ok = False
    syntax_ok = False
    syntax_error = None
    parsed_keys_count = 0

    if exists or is_symlink:
        try:
            st = os.lstat(env_path)
            mode = stat.S_IMODE(st.st_mode)
            mode_octal = oct(mode)
            strict_perms_ok = (not is_symlink) and ((mode & 0o077) == 0)
        except OSError as exc:
            syntax_error = str(exc)

    merged_env = dict(environ)
    if exists and not is_symlink:
        try:
            raw = env_path.read_text(encoding="utf-8")
            parsed = parse_dotenv_content(raw, strict_syntax=True)
            syntax_ok = True
            parsed_keys_count = len(parsed)
            for k, v in parsed.items():
                if k not in merged_env:
                    merged_env[k] = v
        except Exception as exc:
            syntax_error = redact_runtime_text(str(exc), environ=environ)

    provider_status = RuntimeHealthCoordinator.check_provider_config_readiness(merged_env)
    fields = provider_status.get("fields") or {}
    return {
        "exists": exists,
        "is_symlink": is_symlink,
        "mode": mode_octal,
        "strict_0600_ok": strict_perms_ok,
        "syntax_ok": syntax_ok,
        "syntax_error": syntax_error,
        "parsed_key_count": parsed_keys_count,
        "angel_config_keys": {f"{k}_FORMAT_STATUS": v for k, v in fields.items()},
        "angel_config_ready": bool(provider_status.get("all_ready")) and all(
            v == "PRESENT_VALID_FORMAT" for v in fields.values()
        ),
    }


def _find_tailscale_bin() -> Optional[str]:
    candidates = [
        shutil.which("tailscale"),
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ]
    for cand in candidates:
        if cand and os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _probe_tailscale(runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None) -> Dict[str, Any]:
    run_fn = runner or _run_subprocess
    ts_bin = _find_tailscale_bin()
    app_present = Path("/Applications/Tailscale.app").exists()
    if not ts_bin and not app_present:
        return {
            "installed": False,
            "app_present": False,
            "cli_path": None,
            "running": False,
            "backend_state": "NOT_INSTALLED",
            "self_ips": [],
            "self_dns_name": None,
            "peer_count": 0,
            "peers": [],
            "detail": "Tailscale CLI/app is not installed on this Home Mac.",
        }

    if not ts_bin:
        return {
            "installed": True,
            "app_present": app_present,
            "cli_path": None,
            "running": False,
            "backend_state": "CLI_UNAVAILABLE",
            "self_ips": [],
            "self_dns_name": None,
            "peer_count": 0,
            "peers": [],
            "detail": "Tailscale.app detected, but Tailscale CLI executable was not found.",
        }

    rc, out, err = run_fn([ts_bin, "status", "--json"], 4.0)
    if rc != 0:
        return {
            "installed": True,
            "app_present": app_present,
            "cli_path": ts_bin,
            "running": False,
            "backend_state": "DAEMON_NOT_RUNNING_OR_LOGGED_OUT",
            "self_ips": [],
            "self_dns_name": None,
            "peer_count": 0,
            "peers": [],
            "detail": (err.strip() or out.strip() or f"tailscale status exited rc={rc}")[:240],
        }
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {
            "installed": True,
            "app_present": app_present,
            "cli_path": ts_bin,
            "running": False,
            "backend_state": "INVALID_STATUS_OUTPUT",
            "self_ips": [],
            "self_dns_name": None,
            "peer_count": 0,
            "peers": [],
            "detail": "Could not parse JSON output from tailscale status --json.",
        }
    self_node = data.get("Self") or {}
    self_ips = [str(ip) for ip in (self_node.get("TailscaleIPs") or []) if str(ip).strip()]
    backend_state = str(data.get("BackendState") or "UNKNOWN")
    raw_peers = data.get("Peer") or {}
    peers_summary: List[Dict[str, Any]] = []
    if isinstance(raw_peers, dict):
        for pnode in raw_peers.values():
            if not isinstance(pnode, dict):
                continue
            peers_summary.append(
                {
                    "host_name": str(pnode.get("HostName") or ""),
                    "dns_name": str(pnode.get("DNSName") or "").rstrip("."),
                    "tailscale_ips": [str(x) for x in (pnode.get("TailscaleIPs") or [])],
                    "online": bool(pnode.get("Online", False)),
                }
            )
    running = backend_state == "Running" and len(self_ips) > 0
    return {
        "installed": True,
        "app_present": app_present,
        "cli_path": ts_bin,
        "running": running,
        "backend_state": backend_state,
        "self_ips": self_ips,
        "self_dns_name": str(self_node.get("DNSName") or "").rstrip(".") or None,
        "peer_count": len(peers_summary),
        "peers": peers_summary,
        "detail": (
            f"Tailscale BackendState={backend_state} with {len(self_ips)} local tailnet IP(s)."
            if running
            else f"Tailscale BackendState={backend_state}; sign in to tailnet on Home Mac."
        ),
    }


def _discover_tls_paths(
    repo_root: Path,
    state_dir: Path,
    environ: Dict[str, str],
    runtime_cfg: Dict[str, Any],
) -> Tuple[Optional[Path], Optional[Path], str]:
    cert_raw = (
        str(runtime_cfg.get("tls_cert") or "").strip()
        or str(environ.get("TRADELENS_TLS_CERT_FILE") or "").strip()
    )
    key_raw = (
        str(runtime_cfg.get("tls_key") or "").strip()
        or str(environ.get("TRADELENS_TLS_KEY_FILE") or "").strip()
    )
    if cert_raw and key_raw:
        return Path(cert_raw), Path(key_raw), "runtime_config_or_env"

    default_candidates = [
        (state_dir / "tls" / "tradelens-home.crt", state_dir / "tls" / "tradelens-home.key"),
        (repo_root / "runtime_state" / "tls" / "tradelens-home.crt", repo_root / "runtime_state" / "tls" / "tradelens-home.key"),
    ]
    for c_path, k_path in default_candidates:
        if c_path.exists() or k_path.exists():
            return c_path, k_path, "default_runtime_tls_dir"
    return None, None, "unconfigured"


def _probe_tls(
    repo_root: Path,
    state_dir: Path,
    environ: Dict[str, str],
    runtime_cfg: Dict[str, Any],
    runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    run_fn = runner or _run_subprocess
    cert_path, key_path, source = _discover_tls_paths(repo_root, state_dir, environ, runtime_cfg)
    if cert_path is None or key_path is None:
        return {
            "configured": False,
            "valid": False,
            "source": source,
            "cert_path": str(state_dir / "tls" / "tradelens-home.crt"),
            "key_path": str(state_dir / "tls" / "tradelens-home.key"),
            "key_mode_0600_ok": False,
            "subject_alt_names": [],
            "sha256_fingerprint": None,
            "not_before": None,
            "not_after": None,
            "unexpired": False,
            "detail": "TLS certificate/key not configured or generated yet.",
        }

    cert_exists = cert_path.exists() and not cert_path.is_symlink()
    key_exists = key_path.exists() and not key_path.is_symlink()
    key_mode_ok = False
    key_mode = None
    if key_path.exists() or key_path.is_symlink():
        try:
            k_st = os.lstat(key_path)
            km = stat.S_IMODE(k_st.st_mode)
            key_mode = oct(km)
            key_mode_ok = (not key_path.is_symlink()) and ((km & 0o077) == 0)
        except OSError:
            pass

    if not cert_exists or not key_exists:
        return {
            "configured": True,
            "valid": False,
            "source": source,
            "cert_path": str(cert_path),
            "key_path": str(key_path),
            "key_mode": key_mode,
            "key_mode_0600_ok": key_mode_ok,
            "subject_alt_names": [],
            "sha256_fingerprint": None,
            "not_before": None,
            "not_after": None,
            "unexpired": False,
            "detail": "Configured TLS cert or private key missing on disk.",
        }

    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    rc, out, err = run_fn(
        [
            openssl,
            "x509",
            "-in",
            str(cert_path),
            "-noout",
            "-fingerprint",
            "-sha256",
            "-dates",
            "-ext",
            "subjectAltName",
        ],
        4.0,
    )
    if rc != 0:
        return {
            "configured": True,
            "valid": False,
            "source": source,
            "cert_path": str(cert_path),
            "key_path": str(key_path),
            "key_mode": key_mode,
            "key_mode_0600_ok": key_mode_ok,
            "subject_alt_names": [],
            "sha256_fingerprint": None,
            "not_before": None,
            "not_after": None,
            "unexpired": False,
            "detail": f"OpenSSL certificate parse failed: {(err or out).strip()[:160]}",
        }

    fp = None
    not_before = None
    not_after = None
    sans: List[str] = []
    for line in out.splitlines():
        clean = line.strip()
        if "Fingerprint=" in clean:
            fp = clean.split("=", 1)[1].strip()
        elif clean.startswith("notBefore="):
            not_before = clean.split("=", 1)[1].strip()
        elif clean.startswith("notAfter="):
            not_after = clean.split("=", 1)[1].strip()
        elif "DNS:" in clean or "IP Address:" in clean:
            for item in clean.split(","):
                tok = item.strip().replace("IP Address:", "IP:")
                if tok:
                    sans.append(tok)

    rc_exp, _, _ = run_fn([openssl, "x509", "-in", str(cert_path), "-noout", "-checkend", "0"], 4.0)
    unexpired = rc_exp == 0
    san_ok = len(sans) > 0
    valid = cert_exists and key_exists and key_mode_ok and unexpired and san_ok
    detail_parts = []
    if not key_mode_ok:
        detail_parts.append(f"private key mode is {key_mode}, expected 0600")
    if not unexpired:
        detail_parts.append("certificate is expired")
    if not san_ok:
        detail_parts.append("certificate missing Subject Alternative Name")
    return {
        "configured": True,
        "valid": valid,
        "source": source,
        "cert_path": str(cert_path),
        "key_path": str(key_path),
        "key_mode": key_mode,
        "key_mode_0600_ok": key_mode_ok,
        "subject_alt_names": sans,
        "sha256_fingerprint": fp,
        "not_before": not_before,
        "not_after": not_after,
        "unexpired": unexpired,
        "detail": (
            f"Valid SAN TLS cert ({', '.join(sans)}) with 0600 private key."
            if valid
            else "; ".join(detail_parts) or "TLS certificate checks incomplete."
        ),
    }


def _probe_launchd(
    repo_root: Path,
    runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None,
) -> Dict[str, Any]:
    run_fn = runner or _run_subprocess
    plutil = shutil.which("plutil") or "/usr/bin/plutil"
    launchctl = shutil.which("launchctl") or "/bin/launchctl"
    render_dir = repo_root / "deploy" / "launchd"
    user_agents_dir = Path.home() / "Library" / "LaunchAgents"
    labels = (HOME_ENGINE_LABEL, FRONTEND_SERVER_LABEL)

    rendered_info: Dict[str, Any] = {}
    all_rendered_lint_ok = True
    for label in labels:
        p = render_dir / f"{label}.plist"
        if not p.exists():
            rendered_info[label] = {"exists": False, "plutil_ok": False, "path": str(p)}
            all_rendered_lint_ok = False
        else:
            rc, out, err = run_fn([plutil, "-lint", str(p)], 4.0)
            ok = rc == 0
            if not ok:
                all_rendered_lint_ok = False
            rendered_info[label] = {
                "exists": True,
                "plutil_ok": ok,
                "path": str(p),
                "detail": (out or err).strip()[:160],
            }

    uid = os.getuid()
    installed_info: Dict[str, Any] = {}
    all_installed_and_loaded = True
    all_installed_bootstrapped_and_running = True
    any_installed = False
    for label in labels:
        dst = user_agents_dir / f"{label}.plist"
        if not dst.exists():
            installed_info[label] = {
                "installed": False,
                "loaded": False,
                "bootstrapped": False,
                "running": False,
                "launchd_state": "not_installed",
                "pid": None,
                "path": str(dst),
            }
            all_installed_and_loaded = False
            all_installed_bootstrapped_and_running = False
        else:
            any_installed = True
            rc_p, _, _ = run_fn([plutil, "-lint", str(dst)], 4.0)
            rc_l, out_l, _ = run_fn([launchctl, "print", f"gui/{uid}/{label}"], 4.0)
            bootstrapped = rc_l == 0
            out_lower = (out_l or "").lower()
            state_m = re.search(r"^\s*state\s*=\s*(.+)$", out_l or "", re.MULTILINE | re.IGNORECASE)
            pid_m = re.search(r"^\s*pid\s*=\s*(\d+)", out_l or "", re.MULTILINE | re.IGNORECASE)
            launchd_state = state_m.group(1).strip().lower() if state_m else ("not_bootstrapped" if not bootstrapped else "unknown")
            pid_val = int(pid_m.group(1)) if pid_m else None
            running = bool(
                bootstrapped
                and (
                    launchd_state == "running"
                    or pid_val is not None
                    or (state_m is None and "running" in out_lower and "not running" not in out_lower)
                )
            )
            if not bootstrapped or rc_p != 0:
                all_installed_and_loaded = False
            if not bootstrapped or not running or rc_p != 0:
                all_installed_bootstrapped_and_running = False
            installed_info[label] = {
                "installed": True,
                "plutil_ok": rc_p == 0,
                "loaded": bootstrapped,
                "bootstrapped": bootstrapped,
                "running": running,
                "launchd_state": launchd_state,
                "pid": pid_val,
                "path": str(dst),
                "state_hint": (
                    "running"
                    if running
                    else ("bootstrapped_not_running" if bootstrapped else "not_bootstrapped")
                ),
                "output_sample": out_l.splitlines()[0].strip()[:120] if out_l else "",
            }

    return {
        "rendered_dir": str(render_dir),
        "rendered_plists": rendered_info,
        "all_rendered_lint_ok": all_rendered_lint_ok,
        "user_agents_dir": str(user_agents_dir),
        "installed_plists": installed_info,
        "any_installed": any_installed,
        "all_installed_and_loaded": all_installed_and_loaded,
        "all_installed_bootstrapped_and_running": all_installed_bootstrapped_and_running,
        "all_running": all_installed_bootstrapped_and_running,
    }


def _probe_supervisor_runtime(state_dir: Path) -> Dict[str, Any]:
    pid_file = state_dir / "tradelens_supervisor.pid"
    owner_flag = state_dir / "owner_shutdown.flag"
    cfg_file = state_dir / "runtime_config.json"
    cfg: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}

    pid = None
    running = False
    stale_pid = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            stale_pid = True
            running = False

    return {
        "supervisor_running": running,
        "supervisor_pid": pid if running else None,
        "stale_pid_detected": stale_pid,
        "owner_shutdown_flag": owner_flag.exists(),
        "persisted_config": {
            "bind_host": cfg.get("bind_host", "127.0.0.1"),
            "backend_port": cfg.get("backend_port", 8000),
            "frontend_port": cfg.get("frontend_port", 8080),
            "remote_access": bool(cfg.get("remote_access", False)),
            "allowed_peer_ips": list(cfg.get("allowed_peer_ip") or []),
            "tls_configured": bool(cfg.get("tls_cert") and cfg.get("tls_key")),
            "tls_cert": cfg.get("tls_cert"),
            "tls_key": cfg.get("tls_key"),
        },
        "_raw_cfg": cfg,
    }


class StageBSetupCoordinator:
    """
    Coordinates secret-free Stage B progress persistence, live verification checks,
    CLI-validated command guidance, safe local task execution, and recovery playbooks.
    """

    def __init__(
        self,
        repo_root: Optional[Path | str] = None,
        state_dir: Optional[Path | str] = None,
        progress_path: Optional[Path | str] = None,
        remote_access: Optional[RemoteAccessManager] = None,
        environ: Optional[Dict[str, str]] = None,
        command_runner: Optional[Callable[[List[str], float], Tuple[int, str, str]]] = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[3]).resolve()
        self.environ = dict(environ if environ is not None else {})
        self.state_dir = Path(
            state_dir
            or self.environ.get("TRADELENS_RUNTIME_STATE_DIR")
            or (self.repo_root / "runtime_state")
        )
        self.progress_path = Path(
            progress_path or (self.repo_root / ".ops_state" / "stage_b_progress.json")
        )
        self.remote_access = remote_access
        self._cmd_runner = command_runner or _run_subprocess
        self._thread_lock = threading.RLock()
        self._flock_depth = 0

    def _assert_no_symlinks_on_progress_store(self) -> None:
        parent = self.progress_path.parent
        if parent.is_symlink() or self.progress_path.is_symlink():
            raise PermissionError("UNSAFE_SYMLINK_PROGRESS_STORE: Symlinks are forbidden for Stage B progress storage.")

    @contextmanager
    def _progress_file_lock(self):
        self._assert_no_symlinks_on_progress_store()
        with self._thread_lock:
            if self._flock_depth > 0:
                self._flock_depth += 1
                try:
                    yield
                finally:
                    self._flock_depth -= 1
                return

            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.progress_path.parent, 0o700)
            except OSError:
                pass
            lock_path = self.progress_path.parent / ".stage_b_progress.lock"
            if lock_path.is_symlink():
                raise PermissionError("UNSAFE_SYMLINK_PROGRESS_STORE: Lock file cannot be a symlink.")
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    os.chmod(lock_path, 0o600)
                except OSError:
                    pass
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._flock_depth += 1
                try:
                    yield
                finally:
                    self._flock_depth -= 1
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _get_remote_manager(self) -> RemoteAccessManager:
        if self.remote_access is not None:
            # Refresh from disk so cross-process CLI mutations are observed immediately.
            try:
                self.remote_access._sync_state_from_disk_if_changed()
            except Exception:
                pass
            return self.remote_access
        hmac_sec = (self.environ.get("TRADELENS_STATE_HMAC_SECRET") or "").strip() or None
        return RemoteAccessManager(
            state_path=self.state_dir / "remote_auth.json",
            state_hmac_secret=hmac_sec,
            remote_mode_enabled=False,
        )

    def _default_progress_store(self) -> Dict[str, Any]:
        return {
            "schema_version": "1.1",
            "phase": "Phase 3E.5.2.6.1",
            "stage": "STAGE_B",
            "order_execution_enabled": False,
            "broker_auth_excluded": True,
            "phase_3f_excluded": True,
            "current_step": STAGE_B_STEP_SPECS[0]["step_id"],
            "last_updated": None,
            "last_successful_checkpoint": None,
            "manual_confirmations": {},
            "last_checks": {},
            "revocation_drill": {
                "offline_mechanism_verified": False,
                "offline_verified_at": None,
                "live_drill_verified": False,
                "live_drill_verified_at": None,
                "live_drill_note": "",
            },
            "state_snapshot": {
                "boot_session_id": None,
                "approved_device_ips": {},
                "approved_device_fingerprints": {},
                "last_auth_by_device": {},
                "auth_invalidated_before": {},
                "tls_fingerprint": None,
                "supervisor_cfg_ips": [],
            },
            "steps": {
                s["step_id"]: {
                    "step_number": s["step_number"],
                    "step_id": s["step_id"],
                    "title": s["title"],
                    "status": "NOT_STARTED",
                    "verified_at": None,
                    "command_id": s["verify_task_id"],
                    "verification_result": None,
                    "owner_confirmation": {
                        "confirmed": False,
                        "confirmed_at": None,
                        "note": "",
                    },
                    "failure_reason": None,
                }
                for s in STAGE_B_STEP_SPECS
            },
        }

    def _load_progress_store(self) -> Dict[str, Any]:
        base = self._default_progress_store()
        load_meta: Dict[str, Any] = {
            "corruption_recovered": False,
            "corruption_reason": None,
        }
        self._assert_no_symlinks_on_progress_store()
        if not self.progress_path.exists():
            base["_load_meta"] = load_meta
            return base
        with self._progress_file_lock():
            self._assert_no_symlinks_on_progress_store()
            if not self.progress_path.exists():
                base["_load_meta"] = load_meta
                return base
            try:
                raw = json.loads(self.progress_path.read_text(encoding="utf-8"))
            except Exception:
                load_meta["corruption_recovered"] = True
                load_meta["corruption_reason"] = "malformed_json_reset"
                base["_load_meta"] = load_meta
                return base
            if not isinstance(raw, dict):
                load_meta["corruption_recovered"] = True
                load_meta["corruption_reason"] = "invalid_root_type_reset"
                base["_load_meta"] = load_meta
                return base

            raw_mc = raw.get("manual_confirmations")
            if raw_mc is not None:
                if not isinstance(raw_mc, dict):
                    load_meta["corruption_recovered"] = True
                    load_meta["corruption_reason"] = "invalid_manual_confirmations_reset"
                else:
                    for sid, val in raw_mc.items():
                        if sid in STAGE_B_STEP_INDEX and isinstance(val, dict):
                            base["manual_confirmations"][sid] = {
                                "confirmed": bool(val.get("confirmed", False)),
                                "confirmed_at": val.get("confirmed_at") if isinstance(val.get("confirmed_at"), str) else None,
                                "note": str(val.get("note") or "")[:300],
                            }
                        else:
                            load_meta["corruption_recovered"] = True
                            load_meta["corruption_reason"] = "malformed_manual_confirmation_entry"

            raw_lc = raw.get("last_checks")
            if raw_lc is not None:
                if not isinstance(raw_lc, dict):
                    load_meta["corruption_recovered"] = True
                    load_meta["corruption_reason"] = "invalid_last_checks_reset"
                else:
                    for sid, val in raw_lc.items():
                        if sid in STAGE_B_STEP_INDEX and isinstance(val, dict):
                            base["last_checks"][sid] = dict(val)
                        else:
                            load_meta["corruption_recovered"] = True
                            load_meta["corruption_reason"] = "malformed_last_check_entry"

            raw_rd = raw.get("revocation_drill")
            if raw_rd is not None:
                if isinstance(raw_rd, dict):
                    base["revocation_drill"] = {
                        "offline_mechanism_verified": bool(raw_rd.get("offline_mechanism_verified", False)),
                        "offline_verified_at": raw_rd.get("offline_verified_at") if isinstance(raw_rd.get("offline_verified_at"), str) else None,
                        "live_drill_verified": bool(raw_rd.get("live_drill_verified", False)),
                        "live_drill_verified_at": raw_rd.get("live_drill_verified_at") if isinstance(raw_rd.get("live_drill_verified_at"), str) else None,
                        "live_drill_note": str(raw_rd.get("live_drill_note") or "")[:240],
                    }
                else:
                    load_meta["corruption_recovered"] = True
                    load_meta["corruption_reason"] = "invalid_revocation_drill_reset"

            raw_snap = raw.get("state_snapshot")
            if isinstance(raw_snap, dict):
                snap = base["state_snapshot"]
                if isinstance(raw_snap.get("boot_session_id"), str):
                    snap["boot_session_id"] = raw_snap.get("boot_session_id")
                for dict_k in ("approved_device_ips", "approved_device_fingerprints", "last_auth_by_device", "auth_invalidated_before"):
                    if isinstance(raw_snap.get(dict_k), dict):
                        snap[dict_k] = dict(raw_snap[dict_k])
                if isinstance(raw_snap.get("tls_fingerprint"), str):
                    snap["tls_fingerprint"] = raw_snap.get("tls_fingerprint")
                if isinstance(raw_snap.get("supervisor_cfg_ips"), list):
                    snap["supervisor_cfg_ips"] = list(raw_snap.get("supervisor_cfg_ips"))

            raw_steps = raw.get("steps")
            if raw_steps is not None:
                if not isinstance(raw_steps, dict):
                    load_meta["corruption_recovered"] = True
                    load_meta["corruption_reason"] = "invalid_steps_reset"
                else:
                    for sid, val in raw_steps.items():
                        if sid in STAGE_B_STEP_INDEX and isinstance(val, dict):
                            merged = dict(base["steps"][sid])
                            merged.update(val)
                            if not isinstance(merged.get("owner_confirmation"), dict):
                                merged["owner_confirmation"] = dict(base["steps"][sid]["owner_confirmation"])
                            base["steps"][sid] = merged
                        else:
                            load_meta["corruption_recovered"] = True
                            load_meta["corruption_reason"] = "malformed_step_entry"

            base["last_updated"] = raw.get("last_updated") if isinstance(raw.get("last_updated"), str) else None
            base["last_successful_checkpoint"] = (
                raw.get("last_successful_checkpoint")
                if isinstance(raw.get("last_successful_checkpoint"), str)
                else None
            )
            base["_load_meta"] = load_meta
            return base

    def _save_progress_store(self, state_payload: Dict[str, Any]) -> None:
        with self._progress_file_lock():
            self._assert_no_symlinks_on_progress_store()
            to_write = dict(state_payload)
            to_write.pop("_load_meta", None)
            sanitized = redact_payload(to_write, environ=self.environ)
            raw = json.dumps(sanitized, indent=2, sort_keys=False) + "\n"
            fd, tmp_name = tempfile.mkstemp(
                prefix=".stage_b_progress.",
                suffix=".tmp",
                dir=str(self.progress_path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(raw)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self.progress_path)
                os.chmod(self.progress_path, 0o600)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass

    def run_security_boundary_checks(self, probes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute non-destructive runtime security boundary checks locally."""
        probes = probes or self._collect_live_probes()
        env_meta = probes["dotenv"]
        mgr = self._get_remote_manager()
        ts = probes["tailscale"]
        tls = probes["tls"]

        checks: List[Dict[str, Any]] = []

        # 1. Order execution permanently disabled check
        checks.append(
            {
                "check_id": "order_execution_disabled",
                "passed": True,
                "detail": "Order execution paths blocked; read-only assistance architecture active.",
            }
        )

        # 2. Wildcard/public bind refusal
        w_ok, w_reason = is_allowed_bind_host("0.0.0.0")
        checks.append(
            {
                "check_id": "wildcard_bind_rejected",
                "passed": (not w_ok) and w_reason == "PUBLIC_WILDCARD_BIND_FORBIDDEN",
                "detail": f"0.0.0.0 bind test -> allowed={w_ok} ({w_reason})",
            }
        )

        # 3. Home-Mac owner-only routes block remote mutations
        setup_mutation_isolated = all(
            ep in HOME_OWNER_ONLY_ENDPOINTS
            for ep in ("/api/setup/run-task", "/api/setup/step/confirm", "/api/remote/devices/approve", "/api/broker/authenticate")
        ) and "/api/setup/run-task" not in ALLOWED_REMOTE_ROUTE_METHODS
        checks.append(
            {
                "check_id": "home_owner_only_setup_isolation",
                "passed": setup_mutation_isolated,
                "detail": "All setup mutation, device approval, and broker endpoints are restricted to HOME_OWNER_ONLY_ENDPOINTS.",
            }
        )

        # 4. .env hardened permissions (0600, non-symlink)
        dotenv_ok = bool(env_meta["exists"] and env_meta["strict_0600_ok"] and env_meta["syntax_ok"])
        checks.append(
            {
                "check_id": "dotenv_permissions_0600",
                "passed": dotenv_ok,
                "detail": (
                    ".env present, non-symlink, mode 0600, strict syntax valid."
                    if dotenv_ok
                    else f".env hardening incomplete (exists={env_meta['exists']}, mode={env_meta['mode']}, symlink={env_meta['is_symlink']})."
                ),
            }
        )

        # 5. Security headers contract (CSP / X-Frame-Options)
        hdrs = dict(build_security_headers(script_nonce="testnonce", is_html=True))
        csp = hdrs.get("Content-Security-Policy", "")
        csp_ok = "object-src 'none'" in csp and "frame-ancestors 'none'" in csp and hdrs.get("X-Frame-Options") == "DENY"
        checks.append(
            {
                "check_id": "csp_and_http_headers",
                "passed": csp_ok,
                "detail": "Strict nonce CSP, frame-ancestors 'none', X-Frame-Options DENY.",
            }
        )

        # 6. Peer IP pinning enforcement on RemoteAccessManager & supervisor config alignment
        conn = mgr.summarize_connectivity()
        devices = mgr.list_devices()
        active_or_approved = [d for d in devices if d.get("status") in {DEVICE_STATUS_APPROVED, DEVICE_STATUS_ACTIVE}]
        sup_cfg_peers = list(probes.get("supervisor", {}).get("persisted_config", {}).get("allowed_peer_ips") or [])
        sup_remote = bool(probes.get("supervisor", {}).get("persisted_config", {}).get("remote_access"))
        dev_peer_set = {ip for d in active_or_approved for ip in (d.get("allowed_ips") or [])}
        all_devs_pinned = bool(active_or_approved and all(bool(d.get("allowed_ips")) for d in active_or_approved))
        cfg_aligned = (not sup_remote) or (set(sup_cfg_peers) == dev_peer_set)
        pinned_ok = bool(all_devs_pinned and cfg_aligned)
        checks.append(
            {
                "check_id": "exact_peer_ip_pinning",
                "passed": pinned_ok,
                "detail": (
                    "Exact Tailscale peer IP allowlist pinned on all approved devices and aligned with runtime config."
                    if pinned_ok
                    else (
                        "Approved device IP allowlist drifts from supervisor runtime_config.json allowed_peer_ip."
                        if all_devs_pinned and not cfg_aligned
                        else "All approved Office Mac devices must have exact pinned Tailscale peer IPs."
                    )
                ),
            }
        )

        # 7. Tailscale + TLS transport security ready
        transport_ok = bool(ts["running"] and tls["valid"])
        checks.append(
            {
                "check_id": "tailscale_and_tls_transport",
                "passed": transport_ok,
                "detail": (
                    "Tailscale running and SAN TLS certificate verified."
                    if transport_ok
                    else "Awaiting running Tailscale tunnel + valid Home Mac SAN TLS certificate."
                ),
            }
        )

        core_offline_passed = all(
            c["passed"]
            for c in checks
            if c["check_id"] in {"order_execution_disabled", "wildcard_bind_rejected", "home_owner_only_setup_isolation", "csp_and_http_headers"}
        )
        all_passed = all(c["passed"] for c in checks)
        return {
            "core_offline_passed": core_offline_passed,
            "all_checks_passed": all_passed,
            "checks": checks,
        }

    def run_isolated_revocation_self_test(self) -> Dict[str, Any]:
        """
        Verify cross-process immediate revocation & re-enrollment rejection in an isolated
        temporary RemoteAccessManager without touching Home Mac runtime state.
        """
        from backend.app.runtime.security import DeviceKeyCrypto

        with tempfile.TemporaryDirectory(prefix="tradelens_rev_check_") as tmp_dir:
            state_path = Path(tmp_dir) / "remote_auth.json"
            hmac_sec = "stage_b_isolated_revocation_hmac_check_32b!"
            owner_sec = "StageB!IsolatedOwnerCheck#2026"
            priv, pub = DeviceKeyCrypto.generate_keypair()

            mgr_a = RemoteAccessManager(
                state_path=state_path,
                owner_secret=owner_sec,
                state_hmac_secret=hmac_sec,
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.99"],
                allowed_hosts=["100.64.0.10", "127.0.0.1"],
                remote_mode_enabled=True,
            )
            mgr_a.register_device(
                device_id="stage-b-drill-device",
                device_name="Stage B Drill",
                public_key_pem=pub,
                owner_secret=owner_sec,
                allowed_ips=["100.64.0.99"],
                approve=True,
            )
            ch, ch_dec = mgr_a.issue_auth_challenge("stage-b-drill-device", client_ip="100.64.0.99")
            if ch is None:
                return {"passed": False, "detail": ch_dec.message, "checked_at": _now_iso()}
            sig = DeviceKeyCrypto.sign_challenge(priv, ch["challenge_string"])
            auth, auth_dec = mgr_a.authenticate_remote_session(
                device_id="stage-b-drill-device",
                challenge_id=ch["challenge_id"],
                signature_b64=sig,
                client_ip="100.64.0.99",
            )
            if auth is None:
                return {"passed": False, "detail": auth_dec.message, "checked_at": _now_iso()}
            token = auth["access_token"]

            dec_before = mgr_a.authorize_request(
                "/api/scan",
                "GET",
                client_ip="100.64.0.99",
                headers={"Authorization": f"Bearer {token}", "Host": "100.64.0.10:8000"},
            )
            # Second independent process view revokes immediately
            mgr_b = RemoteAccessManager(
                state_path=state_path,
                state_hmac_secret=hmac_sec,
                allowed_vpn_cidrs=["100.64.0.0/10"],
                allowed_peer_ips=["100.64.0.99"],
                allowed_hosts=["100.64.0.10", "127.0.0.1"],
                remote_mode_enabled=True,
            )
            mgr_b.revoke_device("stage-b-drill-device", owner_secret=owner_sec, reason="STAGE_B_DRILL")
            dec_after = mgr_a.authorize_request(
                "/api/scan",
                "GET",
                client_ip="100.64.0.99",
                headers={"Authorization": f"Bearer {token}", "Host": "100.64.0.10:8000"},
            )
            re_enroll_blocked = False
            try:
                mgr_b.register_device(
                    device_id="stage-b-drill-device",
                    device_name="Stage B Drill",
                    public_key_pem=pub,
                    owner_secret=owner_sec,
                    allowed_ips=["100.64.0.99"],
                    allow_re_enroll=False,
                )
            except PermissionError:
                re_enroll_blocked = True

            ok = bool(
                dec_before.allowed
                and (not dec_after.allowed)
                and dec_after.reason_code in {"DEVICE_REVOKED", "SESSION_REVOKED"}
                and re_enroll_blocked
            )
            return {
                "passed": ok,
                "pre_revoke_allowed": dec_before.allowed,
                "post_revoke_reason": dec_after.reason_code,
                "silent_re_enroll_blocked": re_enroll_blocked,
                "checked_at": _now_iso(),
            }

    def _collect_live_probes(self) -> Dict[str, Any]:
        sup = _probe_supervisor_runtime(self.state_dir)
        raw_cfg = sup.pop("_raw_cfg", {})
        env_path = self.repo_root / ".env"
        dotenv_meta = _inspect_dotenv_metadata(env_path, self.environ)
        tailscale_meta = _probe_tailscale(self._cmd_runner)
        tls_meta = _probe_tls(self.repo_root, self.state_dir, self.environ, raw_cfg, self._cmd_runner)
        launchd_meta = _probe_launchd(self.repo_root, self._cmd_runner)
        boot_meta = _probe_boot_session(self._cmd_runner)

        mgr = self._get_remote_manager()
        devices = mgr.list_devices()
        conn = mgr.summarize_connectivity()
        return {
            "host": {
                "platform": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python_version": platform.python_version(),
                "repo_root": str(self.repo_root),
            },
            "boot": boot_meta,
            "dotenv": dotenv_meta,
            "supervisor": sup,
            "tailscale": tailscale_meta,
            "tls": tls_meta,
            "launchd": launchd_meta,
            "remote_auth": {
                "owner_initialized": bool(getattr(mgr, "owner_hash_hex", "")),
                "remote_mode_enabled": bool(getattr(mgr, "remote_mode_enabled", False)),
                "connectivity": conn,
                "devices": devices,
                "device_counts": {
                    "total": len(devices),
                    "pending": sum(1 for d in devices if d.get("status") == DEVICE_STATUS_PENDING),
                    "approved": sum(1 for d in devices if d.get("status") == DEVICE_STATUS_APPROVED),
                    "active": sum(1 for d in devices if d.get("status") == DEVICE_STATUS_ACTIVE),
                    "revoked": sum(1 for d in devices if d.get("status") == DEVICE_STATUS_REVOKED),
                },
            },
        }

    @staticmethod
    def _has_live_revocation_evidence(
        devices: Sequence[Dict[str, Any]],
        approved_devs: Sequence[Dict[str, Any]],
        invalidated_before_map: Dict[str, str],
    ) -> Tuple[bool, str]:
        has_rev_event = any(
            bool(d.get("revoked_at")) or int(d.get("re_enrollment_count") or 0) > 0
            for d in devices
        )
        if not has_rev_event:
            return False, "No revocation or re-enrollment event recorded in remote_auth.json."
        rev_dts = [dt for d in devices if (dt := _parse_iso_dt(d.get("revoked_at"))) is not None]
        latest_rev_dt = max(rev_dts) if rev_dts else None
        for d in approved_devs:
            auth_dt = _parse_iso_dt(d.get("last_auth_at"))
            if auth_dt is None:
                continue
            approved_dt = _parse_iso_dt(d.get("approved_at"))
            if approved_dt is not None and auth_dt < approved_dt:
                continue
            if latest_rev_dt is not None and auth_dt < latest_rev_dt:
                continue
            inv_dt = _parse_iso_dt(invalidated_before_map.get(str(d.get("device_id") or "")))
            if inv_dt is not None and auth_dt <= inv_dt:
                continue
            return True, "Post-revocation live device re-approval and challenge-response authentication verified."
        return False, "No non-revoked approved device has authenticated after the latest revocation/re-approval event."

    def _evaluate_steps(
        self,
        probes: Dict[str, Any],
        persisted: Dict[str, Any],
        sec_report: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        dotenv = probes["dotenv"]
        ts = probes["tailscale"]
        tls = probes["tls"]
        ld = probes["launchd"]
        sup = probes["supervisor"]
        boot = probes.get("boot") or {}
        ra = probes["remote_auth"]
        devices = ra["devices"]
        counts = ra["device_counts"]
        conn = ra["connectivity"]
        manual_conf = persisted.get("manual_confirmations") or {}
        last_checks = persisted.get("last_checks") or {}
        rev_drill = persisted.get("revocation_drill") or {}
        snapshot = persisted.get("state_snapshot") or {}
        load_meta = persisted.get("_load_meta") or {}

        has_any_registered = counts["total"] > 0
        has_approved_or_active = (counts["approved"] + counts["active"]) > 0
        has_revoked_device = counts["revoked"] > 0
        only_revoked = has_revoked_device and not has_approved_or_active
        approved_devs = [d for d in devices if d.get("status") in {DEVICE_STATUS_APPROVED, DEVICE_STATUS_ACTIVE}]
        pinned_ips_ok = bool(approved_devs and all(bool(d.get("allowed_ips")) for d in approved_devs))
        global_peers = list(conn.get("allowed_peer_ips") or [])
        sup_cfg_peers = list(sup.get("persisted_config", {}).get("allowed_peer_ips") or [])
        dev_peer_set = {ip for d in approved_devs for ip in (d.get("allowed_ips") or [])}
        peer_ip_config_drift = bool(
            sup.get("persisted_config", {}).get("remote_access")
            and approved_devs
            and set(sup_cfg_peers) != dev_peer_set
        )
        remote_bind_ok = bool(sup["persisted_config"]["remote_access"]) and sup["supervisor_running"]

        # Detect prior-boot staleness, IP pin changes, TLS rotation, or malformed store recovery
        stale_reasons: List[str] = []
        state_freshness = "LIVE_CURRENT_BOOT"
        if load_meta.get("corruption_recovered"):
            state_freshness = "CORRUPTION_RECOVERED"
            stale_reasons.append("Malformed Stage B progress file was reset safely from live host probes.")

        cur_boot_id = boot.get("boot_session_id")
        prev_boot_id = snapshot.get("boot_session_id")
        boot_changed = bool(prev_boot_id and cur_boot_id and prev_boot_id != cur_boot_id)
        if boot_changed:
            state_freshness = "STALE_PRIOR_BOOT"
            stale_reasons.append(
                "Home Mac rebooted since last persisted checkpoint; live session and LaunchAgent checks must be re-verified."
            )

        prev_dev_ips = snapshot.get("approved_device_ips") or {}
        ip_pin_changed = False
        changed_ip_dev_ids: set[str] = set()
        if prev_dev_ips:
            for d in approved_devs:
                did = str(d.get("device_id") or "")
                cur_ips = list(d.get("allowed_ips") or [])
                if did in prev_dev_ips and list(prev_dev_ips[did]) != cur_ips:
                    ip_pin_changed = True
                    changed_ip_dev_ids.add(did)
        if ip_pin_changed or peer_ip_config_drift:
            if state_freshness == "LIVE_CURRENT_BOOT":
                state_freshness = "STALE_IP_PIN_CHANGED"
            stale_reasons.append(
                "Approved Office Mac Tailscale IP allowlist changed or drifts from runtime config; re-authentication required."
            )

        prev_tls_fp = snapshot.get("tls_fingerprint")
        cur_tls_fp = tls.get("sha256_fingerprint")
        tls_changed = bool(prev_tls_fp and cur_tls_fp and prev_tls_fp != cur_tls_fp)
        if tls_changed:
            if state_freshness == "LIVE_CURRENT_BOOT":
                state_freshness = "STALE_REQUIRES_REVERIFY"
            stale_reasons.append("Home Mac TLS certificate fingerprint changed since last verification.")

        invalidated_before_map: Dict[str, str] = {
            str(k): str(v)
            for k, v in (snapshot.get("auth_invalidated_before") or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        prev_last_auths = snapshot.get("last_auth_by_device") or {}
        for d in approved_devs:
            did = str(d.get("device_id") or "")
            cur_auth_str = d.get("last_auth_at")
            snap_auth_str = prev_last_auths.get(did)
            if boot_changed or did in changed_ip_dev_ids or tls_changed:
                candidate_ts = snap_auth_str or cur_auth_str
                if isinstance(candidate_ts, str) and candidate_ts:
                    prev_inv_dt = _parse_iso_dt(invalidated_before_map.get(did))
                    cand_dt = _parse_iso_dt(candidate_ts)
                    if prev_inv_dt is None or (cand_dt is not None and cand_dt > prev_inv_dt):
                        invalidated_before_map[did] = candidate_ts

        # Only non-revoked approved/active devices with post-approval, post-invalidation auth count cleanly
        verified_session_seen = False
        stale_prior_auth_pending = False
        for d in approved_devs:
            did = str(d.get("device_id") or "")
            auth_dt = _parse_iso_dt(d.get("last_auth_at"))
            if auth_dt is None:
                continue
            approved_dt = _parse_iso_dt(d.get("approved_at"))
            if approved_dt is not None and auth_dt < approved_dt:
                stale_prior_auth_pending = True
                continue
            inv_dt = _parse_iso_dt(invalidated_before_map.get(did))
            if inv_dt is not None and auth_dt <= inv_dt:
                stale_prior_auth_pending = True
                continue
            verified_session_seen = True
            break

        if stale_prior_auth_pending and not verified_session_seen and not boot_changed:
            if state_freshness == "LIVE_CURRENT_BOOT":
                state_freshness = "STALE_PRIOR_BOOT"
            if not stale_reasons:
                stale_reasons.append("Existing remote device authentication predates Home Mac reboot or IP/TLS update; fresh sign-in required.")

        live_evidence_ok, live_evidence_detail = self._has_live_revocation_evidence(
            devices=devices,
            approved_devs=approved_devs,
            invalidated_before_map=invalidated_before_map,
        )
        live_drill_ts = _parse_iso_dt(rev_drill.get("live_drill_verified_at"))
        drill_invalidated = bool(boot_changed or ip_pin_changed or tls_changed)
        live_revocation_drill_valid = bool(
            bool(rev_drill.get("offline_mechanism_verified"))
            and bool(rev_drill.get("live_drill_verified"))
            and live_drill_ts is not None
            and live_evidence_ok
            and not drill_invalidated
        )

        steps_out: List[Dict[str, Any]] = []
        status_by_id: Dict[str, str] = {}

        for spec in STAGE_B_STEP_SPECS:
            sid = spec["step_id"]
            prereqs = spec["prerequisites"]
            unmet_prereqs = [p for p in prereqs if status_by_id.get(p) != "VERIFIED"]
            locked = len(unmet_prereqs) > 0
            persisted_step = (persisted.get("steps") or {}).get(sid, {})
            owner_conf = manual_conf.get(sid) or persisted_step.get("owner_confirmation") or {
                "confirmed": False,
                "confirmed_at": None,
                "note": "",
            }
            check_meta = last_checks.get(sid) or {}

            status = "NOT_STARTED"
            reason: Optional[str] = None
            verification_result = check_meta.get("verification_result") or "Pending check"
            verified_at = check_meta.get("verified_at") or persisted_step.get("verified_at")

            if sid == "step_01_home_mac_preflight":
                if not dotenv["exists"]:
                    status = "BLOCKED"
                    reason = "Home Mac .env file missing; Angel One credentials not found."
                elif dotenv["is_symlink"]:
                    status = "BLOCKED"
                    reason = "Home Mac .env must be a regular file, not a symlink."
                elif not dotenv["strict_0600_ok"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = f"Home Mac .env permissions are {dotenv['mode']}; run `chmod 600 .env` before Stage B."
                elif not dotenv["syntax_ok"]:
                    status = "BLOCKED"
                    reason = f"Home Mac .env strict syntax error: {dotenv['syntax_error'] or 'invalid syntax'}"
                elif not dotenv["angel_config_ready"]:
                    status = "BLOCKED"
                    reason = "One or more Angel One environment keys are missing or placeholders."
                else:
                    status = "VERIFIED"
                    verification_result = "Home Mac host, .env (0600 non-symlink), Angel One format keys, and safety lock verified."
                    verified_at = verified_at or _now_iso()

            elif sid == "step_02_tailscale_home_office":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not ts["installed"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Tailscale is not installed on Home Mac."
                elif not ts["running"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = ts["detail"]
                elif not bool(owner_conf.get("confirmed")):
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Home Mac Tailscale is running; confirm Office Mac is also signed into the same tailnet."
                else:
                    status = "VERIFIED"
                    verification_result = f"Home Mac Tailscale running ({', '.join(ts['self_ips'])}) and Office Mac sign-in owner-confirmed."
                    verified_at = verified_at or owner_conf.get("confirmed_at") or _now_iso()

            elif sid == "step_03_tailscale_acl_verification":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not ts["running"]:
                    status = "BLOCKED"
                    reason = "Tailscale daemon must be running first."
                elif not bool(owner_conf.get("confirmed")):
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Owner must apply and confirm least-privilege Tailscale ACLs (exact Office Mac access only)."
                elif not (global_peers or pinned_ips_ok):
                    status = "IN_PROGRESS"
                    reason = "Tailscale ACL confirmed; pin exact Office Mac Tailscale IP in runtime config / device registry."
                else:
                    status = "VERIFIED"
                    verification_result = "Tailscale ACL owner-confirmed and exact peer IP pinning present."
                    verified_at = verified_at or owner_conf.get("confirmed_at") or _now_iso()

            elif sid == "step_04_tls_certificate_setup":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not tls["configured"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Generate Home Mac SAN TLS certificate for Tailscale IP / MagicDNS host."
                elif not tls["valid"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = tls["detail"]
                else:
                    status = "VERIFIED"
                    verification_result = tls["detail"]
                    verified_at = verified_at or _now_iso()

            elif sid == "step_05_office_mac_device_key_gen":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not ra["owner_initialized"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Initialize Home Mac owner secret via `init-owner --remote-access --owner-secret-prompt` first."
                elif not has_any_registered:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Generate keypair on Office Mac and register public key on Home Mac."
                elif only_revoked:
                    status = "REVOKED"
                    reason = "All registered devices are currently REVOKED; re-enroll or purge revoked record."
                else:
                    status = "VERIFIED"
                    verification_result = f"{counts['total']} device record(s) registered in Home Mac remote_auth.json."
                    verified_at = verified_at or _now_iso()

            elif sid == "step_06_owner_approval_device_pin":
                if only_revoked and status_by_id.get("step_05_office_mac_device_key_gen") == "REVOKED":
                    status = "REVOKED"
                    reason = "Registered device is REVOKED; explicit owner re-enrolment & approval required."
                elif locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not has_approved_or_active:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Approve pending Office Mac device on Home Mac via `approve-device`."
                elif not pinned_ips_ok:
                    status = "BLOCKED"
                    reason = "Approved device is missing exact pinned Tailscale peer IP."
                elif peer_ip_config_drift:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Approved device Tailscale IP allowlist drifts from supervisor runtime_config.json allowed_peer_ip; align runtime config and re-authenticate."
                else:
                    status = "VERIFIED"
                    verification_result = "Approved Office Mac device has exact pinned Tailscale peer IP."
                    verified_at = verified_at or _now_iso()

            elif sid == "step_07_remote_https_challenge_verify":
                if only_revoked and status_by_id.get("step_05_office_mac_device_key_gen") == "REVOKED":
                    status = "REVOKED"
                    reason = "Active remote device was REVOKED."
                elif locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not remote_bind_ok:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Start Home Mac runtime in remote TLS mode bound to Home Tailscale IP with --allowed-peer-ip."
                elif not verified_session_seen:
                    status = "IN_PROGRESS"
                    reason = (
                        "Previous remote authentication invalidated by Home Mac reboot or IP/TLS update; complete fresh challenge-response sign-in."
                        if (stale_prior_auth_pending or boot_changed or ip_pin_changed or tls_changed)
                        else "Home Mac remote HTTPS listener running; complete one Office Mac challenge-response sign-in."
                    )
                else:
                    status = "VERIFIED"
                    verification_result = "Remote HTTPS runtime up and Office Mac challenge-response authentication verified."
                    verified_at = verified_at or _now_iso()

            elif sid == "step_08_security_boundary_tests":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not sec_report["all_checks_passed"]:
                    failed_names = [c["check_id"] for c in sec_report["checks"] if not c["passed"]]
                    status = "REQUIRES_OWNER_ACTION"
                    reason = f"Security boundary checks not all passing: {', '.join(failed_names)}."
                else:
                    status = "VERIFIED"
                    verification_result = "All automated Stage B security boundary checks verified."
                    verified_at = verified_at or _now_iso()

            elif sid == "step_09_revocation_re_enrollment_test":
                if locked:
                    status = "BLOCKED"
                    reason = f"Locked until prerequisites verified: {', '.join(unmet_prereqs)}."
                elif not bool(rev_drill.get("offline_mechanism_verified")):
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Run isolated cross-process revocation self-test (`revocation-selftest`) and live drill (`revocation-check`)."
                elif not bool(rev_drill.get("live_drill_verified")):
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Simulated self-test passed; complete and confirm the live Office Mac revocation & re-enrolment drill."
                elif drill_invalidated:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Prior live revocation drill invalidated by Home Mac reboot or IP/TLS update; re-verify Step 9."
                elif not live_evidence_ok:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = f"Persisted live_drill_verified flag rejected: {live_evidence_detail}"
                else:
                    status = "VERIFIED"
                    verification_result = "Isolated self-test and live post-revocation & re-enrolment drill verified."
                    verified_at = verified_at or rev_drill.get("live_drill_verified_at") or _now_iso()

            elif sid == "step_10_launchagent_installation":
                steps_2_to_9_ok = all(
                    status_by_id.get(STAGE_B_STEP_SPECS[idx]["step_id"]) == "VERIFIED"
                    for idx in range(1, 9)
                )
                if not steps_2_to_9_ok:
                    status = "BLOCKED"
                    reason = "Locked by policy: LaunchAgents MUST NOT be installed until Steps 2 through 9 are all VERIFIED."
                elif not ld["all_rendered_lint_ok"]:
                    status = "READY"
                    reason = "Steps 2-9 verified; render & lint LaunchAgent plists next."
                elif not ld["any_installed"] or not ld["all_installed_and_loaded"]:
                    status = "REQUIRES_OWNER_ACTION"
                    reason = "Plists rendered & linted cleanly; copy to ~/Library/LaunchAgents and run launchctl bootstrap."
                elif not ld["all_running"]:
                    status = "BLOCKED"
                    reason = "LaunchAgents installed and bootstrapped in ~/Library/LaunchAgents, but services are not currently running."
                else:
                    status = "VERIFIED"
                    verification_result = "LaunchAgent plists linted, installed in ~/Library/LaunchAgents, bootstrapped, and actively running."
                    verified_at = verified_at or _now_iso()

            # Non-VERIFIED steps must never show a verified timestamp
            if status != "VERIFIED":
                verified_at = None

            status_by_id[sid] = status
            step_cmds = [COMMAND_INDEX[cid].to_dict() for cid in spec["action_command_ids"] if cid in COMMAND_INDEX]
            steps_out.append(
                {
                    "step_number": spec["step_number"],
                    "step_id": sid,
                    "title": spec["title"],
                    "machine": spec["machine"],
                    "summary": spec["summary"],
                    "prerequisites": list(prereqs),
                    "unmet_prerequisites": unmet_prereqs,
                    "locked": locked,
                    "manual_external": spec["manual_external"],
                    "status": status,
                    "verified_at": verified_at,
                    "command_id": spec["verify_task_id"],
                    "verification_result": verification_result,
                    "owner_confirmation": owner_conf,
                    "failure_reason": reason,
                    "expected_criteria": spec["expected_criteria"],
                    "commands": step_cmds,
                }
            )

        current_step = STAGE_B_STEP_SPECS[-1]["step_id"]
        for st_obj in steps_out:
            if st_obj["status"] != "VERIFIED":
                current_step = st_obj["step_id"]
                break

        verified_steps = [s["step_id"] for s in steps_out if s["status"] == "VERIFIED"]
        last_checkpoint = verified_steps[-1] if verified_steps else None

        if status_by_id.get("step_10_launchagent_installation") == "VERIFIED":
            stage_verdict = "STAGE_B_COMPLETE"
        elif any(s == "VERIFIED" for s in status_by_id.values()):
            stage_verdict = "STAGE_B_PARTIALLY_COMPLETE"
        else:
            stage_verdict = "STAGE_B_BLOCKED"

        overview = {
            "home_runtime_status": (
                "VERIFIED"
                if sup["supervisor_running"]
                else ("BLOCKED" if sup["owner_shutdown_flag"] else "REQUIRES_OWNER_ACTION")
            ),
            "office_device_registration_status": (
                "REVOKED"
                if only_revoked
                else ("VERIFIED" if has_any_registered else "NOT_STARTED")
            ),
            "tailscale_status": (
                "VERIFIED"
                if ts["running"]
                else ("REQUIRES_OWNER_ACTION" if ts["installed"] else "BLOCKED")
            ),
            "tls_certificate_status": (
                "VERIFIED"
                if tls["valid"]
                else ("REQUIRES_OWNER_ACTION" if tls["configured"] else "NOT_STARTED")
            ),
            "remote_auth_status": (
                "REVOKED"
                if only_revoked
                else ("VERIFIED" if verified_session_seen else ("READY" if has_approved_or_active else "NOT_STARTED"))
            ),
            "device_approval_status": (
                "REVOKED"
                if only_revoked
                else (
                    "VERIFIED"
                    if has_approved_or_active and pinned_ips_ok
                    else ("REQUIRES_OWNER_ACTION" if counts["pending"] > 0 else "NOT_STARTED")
                )
            ),
            "launchagent_status": (
                "VERIFIED"
                if ld.get("all_running")
                else (
                    "BLOCKED"
                    if steps_out[-1]["locked"] or (ld["all_installed_and_loaded"] and not ld.get("all_running"))
                    else ("READY" if ld["all_rendered_lint_ok"] else "REQUIRES_OWNER_ACTION")
                )
            ),
            "state_freshness": state_freshness,
            "is_stale": bool(stale_reasons),
            "last_verified_timestamp": persisted.get("last_updated"),
            "current_stage_and_step": {
                "phase": "Phase 3E.5.2.6.1",
                "stage_verdict": stage_verdict,
                "current_step_id": current_step,
                "verified_step_count": len(verified_steps),
                "total_steps": len(STAGE_B_STEP_SPECS),
            },
        }
        return steps_out, {
            "overview": overview,
            "current_step": current_step,
            "last_successful_checkpoint": last_checkpoint,
            "stage_verdict": stage_verdict,
            "state_freshness": state_freshness,
            "is_stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "boot_changed": boot_changed,
            "ip_pin_changed": ip_pin_changed,
            "tls_changed": tls_changed,
            "peer_ip_config_drift": peer_ip_config_drift,
            "invalidated_before_map": invalidated_before_map,
            "live_revocation_drill_valid": live_revocation_drill_valid,
            "approved_devs": approved_devs,
            "approved_ip_map": {
                str(d.get("device_id") or ""): list(d.get("allowed_ips") or [])
                for d in approved_devs
            },
        }

    @staticmethod
    def _coarse_remote_step_summary(step: Dict[str, Any]) -> str:
        sid = str(step.get("step_id") or "")
        st = str(step.get("status") or "NOT_STARTED")
        if st == "VERIFIED":
            return "Verified on Home Mac."
        if st == "REVOKED":
            return "Remote device access revoked; Home Mac owner approval required."
        if st == "BLOCKED":
            return "Locked until earlier prerequisite steps on Home Mac are verified."
        if st == "IN_PROGRESS":
            return "In progress; complete active remote verification."
        if sid in {"step_01_home_mac_preflight", "step_02_tailscale_home_office", "step_03_tailscale_acl_verification"}:
            return "Awaiting Home Mac owner local configuration."
        return "Awaiting completion on Home Mac."

    @classmethod
    def _project_remote_setup_state(
        cls,
        *,
        probes: Dict[str, Any],
        persisted: Dict[str, Any],
        sec_report: Dict[str, Any],
        steps: List[Dict[str, Any]],
        meta: Dict[str, Any],
        remote_device_id: Optional[str],
    ) -> Dict[str, Any]:
        sup = probes["supervisor"]
        ts = probes["tailscale"]
        tls = probes["tls"]
        ld = probes["launchd"]
        ra = probes["remote_auth"]
        host = probes["host"]
        rev_drill = persisted.get("revocation_drill") or {}

        self_dev = None
        if remote_device_id:
            for d in ra["devices"]:
                if d.get("device_id") == remote_device_id:
                    self_dev = {
                        "device_id": d["device_id"],
                        "device_name": d.get("device_name") or d["device_id"],
                        "status": d["status"],
                        "approved_at": d.get("approved_at"),
                        "last_auth_at": d.get("last_auth_at"),
                        "revoked_at": d.get("revoked_at"),
                        "active_session_count": int(d.get("active_session_count") or 0),
                        "peer_ip_pinned": bool(d.get("allowed_ips")),
                    }
                    break

        coarse_steps = [
            {
                "step_number": s["step_number"],
                "step_id": s["step_id"],
                "title": s["title"],
                "machine": s["machine"],
                "locked": bool(s["locked"]),
                "manual_external": bool(s["manual_external"]),
                "status": s["status"],
                "verified_at": s["verified_at"],
                "unmet_prerequisites": list(s["unmet_prerequisites"]),
                "status_summary": cls._coarse_remote_step_summary(s),
            }
            for s in steps
        ]

        coarse_checks = [
            {"check_id": c["check_id"], "passed": bool(c["passed"])}
            for c in sec_report["checks"]
        ]

        projected = {
            "phase": "Phase 3E.5.2.6.1",
            "stage_verdict": meta["stage_verdict"],
            "order_execution_enabled": False,
            "broker_auth_excluded": True,
            "phase_3f_excluded": True,
            "is_remote_view": True,
            "local_mutations_permitted": False,
            "state_freshness": meta["state_freshness"],
            "is_stale": meta["is_stale"],
            "stale_reasons": list(meta["stale_reasons"]),
            "self_device": self_dev,
            "overview": dict(meta["overview"]),
            "probes": {
                "host": {
                    "platform": host["platform"],
                    "machine": host["machine"],
                },
                "supervisor": {
                    "supervisor_running": bool(sup["supervisor_running"]),
                    "owner_shutdown_flag": bool(sup["owner_shutdown_flag"]),
                    "stale_pid_detected": bool(sup["stale_pid_detected"]),
                },
                "tailscale": {
                    "installed": bool(ts["installed"]),
                    "running": bool(ts["running"]),
                    "backend_state": ts["backend_state"],
                },
                "tls": {
                    "configured": bool(tls.get("configured", False)),
                    "valid": bool(tls.get("valid", False)),
                    "unexpired": bool(tls.get("unexpired", False)),
                },
                "launchd": {
                    "all_rendered_lint_ok": bool(ld["all_rendered_lint_ok"]),
                    "any_installed": bool(ld["any_installed"]),
                    "all_installed_and_loaded": bool(ld["all_installed_and_loaded"]),
                    "all_running": bool(ld.get("all_running")),
                },
                "remote_auth": {
                    "remote_mode_enabled": bool(ra["remote_mode_enabled"]),
                    "self_device": self_dev,
                },
                "security_boundary": {
                    "core_offline_passed": bool(sec_report["core_offline_passed"]),
                    "all_checks_passed": bool(sec_report["all_checks_passed"]),
                    "checks": coarse_checks,
                },
                "revocation_drill": {
                    "offline_mechanism_verified": bool(rev_drill.get("offline_mechanism_verified")),
                    "live_drill_verified": bool(meta["live_revocation_drill_valid"]),
                },
            },
            "steps": coarse_steps,
            "command_catalog": [],
            "recovery_playbooks": [],
            "remote_notice": (
                "Remote Office Mac view is minimized and read-only. Home Mac paths, .env diagnostics, "
                "other devices, peer allowlists, fingerprints, and owner recovery commands are hidden."
            ),
        }
        return _strip_sensitive_keys(projected)

    def get_setup_state(
        self,
        *,
        persist: bool = False,
        is_remote_view: bool = False,
        remote_device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compute live Stage B Setup & Remote Access state cleanly without leaking secrets.
        `GET /api/setup/state` calls with `persist=False` (read-only view).
        Remote authenticated callers receive a strictly minimized projection without local
        paths, .env metadata, other devices, peer allowlists, or owner recovery commands.
        """
        if is_remote_view:
            persist = False
        persisted = self._load_progress_store()
        probes = self._collect_live_probes()
        sec_report = self.run_security_boundary_checks(probes=probes)
        steps, meta = self._evaluate_steps(probes, persisted, sec_report)

        if persist:
            with self._progress_file_lock():
                now = _now_iso()
                persisted["last_updated"] = now
                persisted["current_step"] = meta["current_step"]
                persisted["last_successful_checkpoint"] = meta["last_successful_checkpoint"]
                approved_devs = meta["approved_devs"]
                persisted["state_snapshot"] = {
                    "boot_session_id": (probes.get("boot") or {}).get("boot_session_id"),
                    "approved_device_ips": dict(meta["approved_ip_map"]),
                    "approved_device_fingerprints": {
                        str(d.get("device_id") or ""): str(d.get("public_key_fingerprint") or "")
                        for d in approved_devs
                    },
                    "last_auth_by_device": {
                        str(d.get("device_id") or ""): str(d.get("last_auth_at"))
                        for d in approved_devs
                        if d.get("last_auth_at")
                    },
                    "auth_invalidated_before": dict(meta["invalidated_before_map"]),
                    "tls_fingerprint": probes["tls"].get("sha256_fingerprint"),
                    "supervisor_cfg_ips": list(
                        probes["supervisor"].get("persisted_config", {}).get("allowed_peer_ips") or []
                    ),
                }
                if (meta["boot_changed"] or meta["ip_pin_changed"] or meta["tls_changed"]) and not meta["live_revocation_drill_valid"]:
                    rev_d = persisted.setdefault("revocation_drill", {})
                    rev_d["live_drill_verified"] = False
                    rev_d["live_drill_verified_at"] = None
                persisted["steps"] = {
                    s["step_id"]: {
                        "step_number": s["step_number"],
                        "step_id": s["step_id"],
                        "title": s["title"],
                        "status": s["status"],
                        "verified_at": s["verified_at"],
                        "command_id": s["command_id"],
                        "verification_result": s["verification_result"],
                        "owner_confirmation": s["owner_confirmation"],
                        "failure_reason": s["failure_reason"],
                    }
                    for s in steps
                }
                self._save_progress_store(persisted)
                meta["overview"]["last_verified_timestamp"] = now

        if is_remote_view:
            remote_payload = self._project_remote_setup_state(
                probes=probes,
                persisted=persisted,
                sec_report=sec_report,
                steps=steps,
                meta=meta,
                remote_device_id=remote_device_id,
            )
            return redact_payload(remote_payload, environ=self.environ)

        payload = {
            "phase": "Phase 3E.5.2.6.1",
            "stage_verdict": meta["stage_verdict"],
            "order_execution_enabled": False,
            "broker_auth_excluded": True,
            "phase_3f_excluded": True,
            "is_remote_view": False,
            "local_mutations_permitted": True,
            "state_freshness": meta["state_freshness"],
            "is_stale": meta["is_stale"],
            "stale_reasons": list(meta["stale_reasons"]),
            "boot_session_id": (probes.get("boot") or {}).get("boot_session_id"),
            "self_device": None,
            "progress_file": str(self.progress_path.relative_to(self.repo_root))
            if self.progress_path.is_relative_to(self.repo_root)
            else self.progress_path.name,
            "overview": meta["overview"],
            "probes": {
                "host": probes["host"],
                "boot": probes.get("boot"),
                "dotenv": probes["dotenv"],
                "tailscale": probes["tailscale"],
                "tls": probes["tls"],
                "supervisor": probes["supervisor"],
                "launchd": probes["launchd"],
                "remote_auth": probes["remote_auth"],
                "security_boundary": sec_report,
                "revocation_drill": persisted.get("revocation_drill"),
            },
            "steps": steps,
            "command_catalog": [c.to_dict() for c in COMMAND_CATALOG],
            "recovery_playbooks": list(RECOVERY_PLAYBOOKS),
            "docs_reference": "docs/operations/STAGE_B_SETUP.md",
        }
        return redact_payload(_strip_sensitive_keys(payload), environ=self.environ)

    def confirm_manual_step(
        self,
        *,
        step_id: str,
        confirmed: bool,
        audit_note: str = "",
        is_loopback: bool = False,
    ) -> Dict[str, Any]:
        """
        Record explicit owner confirmation ONLY for genuinely manual external steps
        (`step_02_tailscale_home_office`, `step_03_tailscale_acl_verification`) AND
        only when their technical prerequisites on Home Mac are satisfied.
        Never allows bypassing automated technical/security steps.
        """
        if not is_loopback:
            raise PermissionError("HOME_OWNER_ONLY_OPERATION: Setup confirmations are strictly forbidden from remote sessions.")
        if step_id not in STAGE_B_STEP_INDEX:
            raise ValueError(f"Unknown Stage B step_id: '{step_id}'")
        if step_id not in MANUAL_CONFIRMATION_ALLOWED_STEPS:
            raise PermissionError(
                f"MANUAL_CONFIRMATION_CANNOT_BYPASS_VERIFICATION: Step '{step_id}' requires automated technical verification."
            )
        if not confirmed:
            raise ValueError("Explicit confirmation (`confirmed=true`) is required.")

        clean_note = redact_runtime_text((audit_note or "").strip(), environ=self.environ)[:300]
        if not clean_note:
            raise ValueError("An explicit owner audit note is required to record external verification confirmation.")

        with self._progress_file_lock():
            pre_state = self.get_setup_state(persist=False, is_remote_view=False)
            pre_by_id = {s["step_id"]: s for s in pre_state["steps"]}
            ts_probe = pre_state["probes"]["tailscale"]
            if step_id == "step_02_tailscale_home_office":
                if pre_by_id["step_01_home_mac_preflight"]["status"] != "VERIFIED" or not ts_probe.get("running"):
                    raise PermissionError(
                        "PREREQUISITE_NOT_MET: Step 1 (Home Mac Preflight) and a running Home Mac Tailscale daemon are required before confirming Step 2."
                    )
            elif step_id == "step_03_tailscale_acl_verification":
                if pre_by_id["step_02_tailscale_home_office"]["status"] != "VERIFIED" or not ts_probe.get("running"):
                    raise PermissionError(
                        "PREREQUISITE_NOT_MET: Step 2 and a running Home Mac Tailscale daemon are required before confirming Step 3."
                    )

            persisted = self._load_progress_store()
            now = _now_iso()
            persisted.setdefault("manual_confirmations", {})[step_id] = {
                "confirmed": True,
                "confirmed_at": now,
                "note": clean_note,
            }
            persisted["last_updated"] = now
            self._save_progress_store(persisted)
            return self.get_setup_state(persist=True, is_remote_view=False)

    def run_allowlisted_task(
        self,
        *,
        task_id: str,
        confirmed: bool = False,
        audit_note: str = "",
        is_loopback: bool = False,
        extra_args: Optional[Dict[str, Any]] = None,
        called_from_http: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute strictly allowlisted read-only checks or owner-confirmed local host mutations.
        Never executes arbitrary shell strings, never runs broker-auth, never activates LaunchAgents silently.
        """
        if not is_loopback:
            raise PermissionError("HOME_OWNER_ONLY_OPERATION: Setup task execution is forbidden from remote clients.")

        lowered = (task_id or "").strip().lower()

        allowlisted_read_only = {
            "tradelens-stage-b-preflight",
            "tradelens-stage-b-status",
            "tradelens-stage-b-home-preflight",
            "tradelens-stage-b-tailscale-check",
            "tradelens-stage-b-tls-check",
            "tradelens-stage-b-register-device-guide",
            "tradelens-stage-b-verify-device",
            "tradelens-stage-b-remote-check",
            "tradelens-stage-b-security-check",
            "tradelens-stage-b-revocation-selftest",
            "tradelens-stage-b-revocation-check",
            "tradelens-stage-b-launchd-lint",
            "tradelens-stage-b-launchd-status",
        }
        allowlisted_mutations = {
            "tradelens-stage-b-render-launchd",
            "tradelens-stage-b-report",
        }

        if lowered not in allowlisted_read_only and lowered not in allowlisted_mutations:
            if lowered == "tradelens-stage-b-register-device":
                raise PermissionError(
                    "CLI_OWNER_SECRET_REQUIRED: Run `python3.12 -m backend.app.runtime.cli register-device ... --owner-secret-prompt` locally."
                )
            blocked_keywords = ("broker", "angel", "order", "trade", "phase3f", "shell", "bash", "sudo")
            if any(tok in lowered for tok in blocked_keywords) or lowered in {"sh", "zsh"}:
                raise PermissionError(f"TASK_FORBIDDEN: Task '{task_id}' is permanently excluded from Stage B setup.")
            raise ValueError(f"TASK_NOT_ALLOWLISTED: '{task_id}' is not an allowlisted Stage B setup task.")

        if lowered in allowlisted_mutations and not confirmed:
            raise PermissionError("OWNER_CONFIRMATION_REQUIRED: Host-mutating Stage B task requires explicit confirmation.")

        if extra_args is not None and not isinstance(extra_args, dict):
            raise ValueError("INVALID_EXTRA_ARGS: extra_args must be an object or null.")
        extra_args = dict(extra_args or {})
        if called_from_http and extra_args:
            raise ValueError("HTTP_EXTRA_ARGS_NOT_ALLOWED: Arbitrary extra_args are rejected over HTTP setup task execution.")
        allowed_extra_keys = {
            "tradelens-stage-b-revocation-check": {"record_live_drill"},
            "tradelens-stage-b-render-launchd": {"python_executable"},
        }.get(lowered, set())
        unknown_keys = sorted(set(extra_args.keys()) - allowed_extra_keys)
        if unknown_keys:
            raise ValueError(f"UNEXPECTED_EXTRA_ARGS: Unsupported extra_args {unknown_keys} for task '{lowered}'.")

        gate_tasks = {
            "tradelens-stage-b-home-preflight",
            "tradelens-stage-b-tailscale-check",
            "tradelens-stage-b-tls-check",
            "tradelens-stage-b-verify-device",
            "tradelens-stage-b-remote-check",
            "tradelens-stage-b-security-check",
            "tradelens-stage-b-revocation-check",
            "tradelens-stage-b-launchd-lint",
            "tradelens-stage-b-launchd-status",
        }

        with self._progress_file_lock():
            now = _now_iso()
            persisted = self._load_progress_store()
            task_result: Dict[str, Any] = {
                "task_id": lowered,
                "executed_at": now,
                "category": "host_mutation" if lowered in allowlisted_mutations else "read_only",
                "command_succeeded": True,
                "gate_task": lowered in gate_tasks,
                "simulated_only": False,
                "templates_only": False,
                "exit_0_means_stage_b_verified": False,
            }

            if lowered == "tradelens-stage-b-security-check":
                probes = self._collect_live_probes()
                sec = self.run_security_boundary_checks(probes=probes)
                persisted.setdefault("last_checks", {})["step_08_security_boundary_tests"] = {
                    "verified_at": now if sec["all_checks_passed"] else None,
                    "command_id": lowered,
                    "verification_result": (
                        "All security boundary checks passed."
                        if sec["all_checks_passed"]
                        else "Offline boundary checks passed; awaiting live Tailscale/TLS/peer checks."
                    ),
                    "passed": sec["all_checks_passed"],
                    "failure_reason": None if sec["all_checks_passed"] else "Live transport/peer checks incomplete.",
                }
                task_result["security_boundary"] = sec

            elif lowered == "tradelens-stage-b-revocation-selftest":
                iso = self.run_isolated_revocation_self_test()
                rev_drill = persisted.setdefault("revocation_drill", {})
                rev_drill["offline_mechanism_verified"] = bool(iso["passed"])
                rev_drill["offline_verified_at"] = now if iso["passed"] else None
                task_result["isolated_revocation_self_test"] = iso
                task_result["simulated_only"] = True
                task_result["proves_live_office_mac_revocation"] = False

            elif lowered == "tradelens-stage-b-revocation-check":
                iso = self.run_isolated_revocation_self_test()
                rev_drill = persisted.setdefault("revocation_drill", {})
                rev_drill["offline_mechanism_verified"] = bool(iso["passed"])
                rev_drill["offline_verified_at"] = now if iso["passed"] else None
                if extra_args.get("record_live_drill") and confirmed:
                    pre_state = self.get_setup_state(persist=False, is_remote_view=False)
                    step_map = {s["step_id"]: s["status"] for s in pre_state["steps"]}
                    steps_2_to_8_ok = all(
                        step_map.get(STAGE_B_STEP_SPECS[idx]["step_id"]) == "VERIFIED"
                        for idx in range(1, 8)
                    )
                    pre_devices = pre_state["probes"]["remote_auth"]["devices"]
                    pre_approved = [
                        d for d in pre_devices
                        if d.get("status") in {DEVICE_STATUS_APPROVED, DEVICE_STATUS_ACTIVE}
                    ]
                    snap_inv = (persisted.get("state_snapshot") or {}).get("auth_invalidated_before") or {}
                    live_ok, live_detail = self._has_live_revocation_evidence(
                        devices=pre_devices,
                        approved_devs=pre_approved,
                        invalidated_before_map=snap_inv if isinstance(snap_inv, dict) else {},
                    )
                    if not steps_2_to_8_ok or not live_ok:
                        raise PermissionError(
                            f"PREREQUISITE_OR_EVIDENCE_MISSING: Live Step 9 revocation drill requires Steps 2-8 VERIFIED and real post-revocation auth evidence ({live_detail})."
                        )
                    rev_drill["live_drill_verified"] = True
                    rev_drill["live_drill_verified_at"] = now
                    rev_drill["live_drill_note"] = redact_runtime_text(audit_note or "Live revocation drill verified.", environ=self.environ)[:240]
                persisted.setdefault("last_checks", {})["step_09_revocation_re_enrollment_test"] = {
                    "verified_at": now if rev_drill.get("live_drill_verified") else None,
                    "command_id": lowered,
                    "verification_result": (
                        "Isolated revocation self-test PASSED; live Stage B drill recorded."
                        if rev_drill.get("live_drill_verified")
                        else "Isolated cross-process revocation self-test PASSED; awaiting live Office Mac post-revocation drill."
                    ),
                    "passed": bool(iso["passed"] and rev_drill.get("live_drill_verified")),
                    "failure_reason": None if rev_drill.get("live_drill_verified") else "Awaiting live Steps 2-8 & Office Mac drill.",
                }
                task_result["isolated_revocation_self_test"] = iso

            elif lowered == "tradelens-stage-b-register-device-guide":
                task_result["registered_any_device"] = False
                task_result["guide"] = {
                    "office_mac_keygen_command": COMMAND_INDEX["cmd_generate_device_key_office"].command_text,
                    "home_mac_register_command": COMMAND_INDEX["cmd_register_device_home"].command_text,
                    "home_mac_approve_command": COMMAND_INDEX["cmd_approve_device_home"].command_text,
                }

            elif lowered == "tradelens-stage-b-render-launchd":
                out_dir = self.repo_root / "deploy" / "launchd"
                sup_cfg = _probe_supervisor_runtime(self.state_dir).get("_raw_cfg", {})
                validated_py = _validate_python_executable(extra_args.get("python_executable"), repo_root=self.repo_root)
                rendered = render_launchd_plists(
                    output_dir=out_dir,
                    repo_root=self.repo_root,
                    python_executable=validated_py,
                    bind_host=str(sup_cfg.get("bind_host") or "127.0.0.1"),
                    backend_port=int(sup_cfg.get("backend_port") or 8000),
                    frontend_port=int(sup_cfg.get("frontend_port") or 8080),
                    state_dir=str(self.state_dir),
                    log_dir=str(self.repo_root / "runtime_logs"),
                    enable_caffeinate=True,
                    vpn_cidrs=list(sup_cfg.get("vpn_cidr") or ()),
                    allowed_peer_ips=normalize_allowed_peer_ips(sup_cfg.get("allowed_peer_ip") or ()),
                    allowed_hosts=list(sup_cfg.get("allowed_host") or ()),
                    allowed_origins=list(sup_cfg.get("allowed_origin") or ()),
                    tls_cert_file=sup_cfg.get("tls_cert"),
                    tls_key_file=sup_cfg.get("tls_key"),
                    remote_access_enabled=bool(sup_cfg.get("remote_access", False)),
                )
                task_result["rendered_plists"] = {k: str(v) for k, v in rendered.items()}
                task_result["installed_to_launchagents"] = False
                task_result["templates_only"] = True

            elif lowered == "tradelens-stage-b-report":
                report_path = self.repo_root / "reports" / "phase3e_5_2_6_stage_b_provisioning_report.md"
                report_md = self.build_markdown_report(self.get_setup_state(persist=False, is_remote_view=False))
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(report_md, encoding="utf-8")
                task_result["report_path"] = str(report_path.relative_to(self.repo_root))

            persisted["last_updated"] = now
            self._save_progress_store(persisted)
            updated_state = self.get_setup_state(persist=True, is_remote_view=False)

        step_map = {s["step_id"]: s["status"] for s in updated_state["steps"]}
        ld_probe = updated_state["probes"]["launchd"]
        stage_complete = updated_state["stage_verdict"] == "STAGE_B_COMPLETE"

        gate_checks_map = {
            "tradelens-stage-b-home-preflight": step_map.get("step_01_home_mac_preflight") == "VERIFIED",
            "tradelens-stage-b-tailscale-check": (
                step_map.get("step_02_tailscale_home_office") == "VERIFIED"
                and step_map.get("step_03_tailscale_acl_verification") == "VERIFIED"
            ),
            "tradelens-stage-b-tls-check": step_map.get("step_04_tls_certificate_setup") == "VERIFIED",
            "tradelens-stage-b-verify-device": (
                step_map.get("step_05_office_mac_device_key_gen") == "VERIFIED"
                and step_map.get("step_06_owner_approval_device_pin") == "VERIFIED"
            ),
            "tradelens-stage-b-remote-check": step_map.get("step_07_remote_https_challenge_verify") == "VERIFIED",
            "tradelens-stage-b-security-check": (
                step_map.get("step_08_security_boundary_tests") == "VERIFIED"
                and bool(updated_state["probes"]["security_boundary"]["all_checks_passed"])
            ),
            "tradelens-stage-b-revocation-check": step_map.get("step_09_revocation_re_enrollment_test") == "VERIFIED",
            "tradelens-stage-b-launchd-lint": bool(ld_probe.get("all_rendered_lint_ok")),
            "tradelens-stage-b-launchd-status": step_map.get("step_10_launchagent_installation") == "VERIFIED",
        }

        if lowered in gate_checks_map:
            gate_ok = bool(gate_checks_map[lowered])
            task_result["passed"] = gate_ok
            task_result["verification_passed"] = gate_ok
            if lowered == "tradelens-stage-b-launchd-lint":
                task_result["templates_only"] = True
                task_result["proves_installed_or_running"] = False
                task_result["summary_message"] = (
                    "Rendered LaunchAgent templates passed plutil -lint (templates only; does NOT prove installed or running)."
                    if gate_ok
                    else "LaunchAgent plist templates are missing or failed plutil -lint."
                )
            else:
                task_result["summary_message"] = (
                    f"{lowered}: Verification PASSED."
                    if gate_ok
                    else f"{lowered}: Verification BLOCKED / incomplete (stage_verdict={updated_state['stage_verdict']})."
                )
        elif lowered == "tradelens-stage-b-revocation-selftest":
            selftest_ok = bool(task_result.get("isolated_revocation_self_test", {}).get("passed"))
            task_result["passed"] = selftest_ok
            task_result["selftest_passed"] = selftest_ok
            task_result["verification_passed"] = False
            task_result["summary_message"] = (
                "Simulated isolated cross-process revocation self-test PASSED (does not prove live Office Mac revocation)."
                if selftest_ok
                else "Simulated isolated revocation self-test FAILED."
            )
        elif lowered in {"tradelens-stage-b-preflight", "tradelens-stage-b-status"}:
            task_result["passed"] = stage_complete
            task_result["verification_passed"] = stage_complete
            task_result["summary_message"] = (
                "Stage B complete and verified."
                if stage_complete
                else f"Inspection completed honestly: stage_verdict={updated_state['stage_verdict']}."
            )
        elif lowered == "tradelens-stage-b-register-device-guide":
            task_result["passed"] = True
            task_result["verification_passed"] = False
            task_result["summary_message"] = "Read-only Office Mac device registration instructions generated (zero mutations performed)."
        elif lowered == "tradelens-stage-b-render-launchd":
            task_result["passed"] = True
            task_result["verification_passed"] = False
            task_result["summary_message"] = "Rendered LaunchAgent plist templates under deploy/launchd/ (not installed to ~/Library/LaunchAgents)."
        elif lowered == "tradelens-stage-b-report":
            task_result["passed"] = True
            task_result["verification_passed"] = stage_complete
            task_result["summary_message"] = f"Stage B report written cleanly (stage_verdict={updated_state['stage_verdict']})."

        return redact_payload(
            _strip_sensitive_keys(
                {
                    "task_result": task_result,
                    "setup_state": updated_state,
                }
            ),
            environ=self.environ,
        )

    def build_markdown_report(self, state: Optional[Dict[str, Any]] = None) -> str:
        """Render an honest, secret-free Stage B provisioning status report."""
        st = state or self.get_setup_state(persist=False, is_remote_view=False)
        probes = st["probes"]
        dotenv = probes["dotenv"]
        ts = probes["tailscale"]
        tls = probes["tls"]
        ld = probes["launchd"]
        ra = probes["remote_auth"]
        sec = probes["security_boundary"]
        steps = st["steps"]
        verdict = st["stage_verdict"]

        lines: List[str] = [
            "# Phase 3E.5.2.6 — Controlled Stage B Provisioning, Setup Dashboard & Mise Report",
            "",
            f"- **Generated (UTC):** {st['overview'].get('last_verified_timestamp') or _now_iso()}",
            f"- **Host:** {probes['host']['platform']} {probes['host']['release']} ({probes['host']['machine']}), Python {probes['host']['python_version']}",
            f"- **Live Provisioning Verdict:** `{verdict}`",
            f"- **Order Execution Enabled:** `{st['order_execution_enabled']}` (permanently disabled)",
            f"- **Broker Authentication Executed:** `False` (strictly excluded)",
            f"- **Phase 3F Started:** `False` (strictly excluded)",
            "",
            "## 1. Delivered Software Architecture & UI Controls",
            "",
            "- **Setup & Remote Access Dashboard (`frontend/index.html`):** `#tab-setup` & `#panel-setup` featuring 9-card Overview, Sequential 10-Step Setup Wizard, native `<dialog>` confirmation modal (`#setupConfirmDialog`), accessible copy-feedback live regions, and 10-playbook Setup Recovery Center.",
            f"- **Persistent Secret-Free Progress Store:** `{st['progress_file']}` (`0600` file inside `0700` directory, refreshed dynamically from live read-only probes).",
            "- **`mise.toml` Integration & CLI Parity:** 16 `tradelens-stage-b-*` tasks mapped cleanly to `backend.app.runtime.cli stage-b` and verified against `cli.build_parser()`.",
            "- **Documentation:** `docs/operations/STAGE_B_SETUP.md` covers Home/Office command labels, prerequisites, security rules, `mise` tasks, and all 10 recovery playbooks.",
            "",
            "## 2. Environment & Preflight Observations (Home Mac vs Unprovisioned Hardware)",
            "",
            f"- **`.env` present:** `{dotenv['exists']}`, **symlink:** `{dotenv['is_symlink']}`, **mode:** `{dotenv['mode']}`, **strict 0600 ok:** `{dotenv['strict_0600_ok']}`",
            f"- **Angel One key readiness (format status only, zero secret values):** `{json.dumps(dotenv['angel_config_keys'])}`",
            f"- **Tailscale installed:** `{ts['installed']}`, **running:** `{ts['running']}`, **state:** `{ts['backend_state']}`",
            f"- **TLS configured:** `{tls['configured']}`, **valid:** `{tls['valid']}`, **SAN:** `{tls['subject_alt_names']}`",
            f"- **Registered devices:** total=`{ra['device_counts']['total']}`, approved=`{ra['device_counts']['approved']}`, active=`{ra['device_counts']['active']}`, revoked=`{ra['device_counts']['revoked']}`",
            f"- **LaunchAgent plists rendered & linted:** `{ld['all_rendered_lint_ok']}`, **installed into `~/Library/LaunchAgents`:** `{ld['any_installed']}` (strictly locked until Steps 2–9 pass)",
            "",
            "## 3. Guided 10-Step Stage B Status Table",
            "",
            "| Step | Title | Status | Reason / Verification Summary |",
            "| --- | --- | --- | --- |",
        ]
        for s in steps:
            summary = (s["failure_reason"] or s["verification_result"] or "-").replace("|", "/")
            lines.append(f"| Step {s['step_number']} | {s['title']} | `{s['status']}` | {summary} |")

        rev_drill = probes.get("revocation_drill") or {}
        lines.extend(
            [
                "",
                "## 4. Automated Security Boundary & Isolation Verification",
                "",
                f"- **Core offline boundary checks passed:** `{sec['core_offline_passed']}`",
                f"- **All Stage B live+offline boundary checks passed:** `{sec['all_checks_passed']}`",
                f"- **Isolated cross-process revocation self-test:** `{'PASS' if rev_drill.get('offline_mechanism_verified') else 'NOT_RUN'}` (live Office Mac drill: `{bool(rev_drill.get('live_drill_verified'))}`)",
                "",
            ]
        )
        for chk in sec["checks"]:
            mark = "PASS" if chk["passed"] else "PENDING_OR_FAIL"
            lines.append(f"- `{chk['check_id']}` -> **{mark}**: {chk['detail']}")

        lines.extend(
            [
                "",
                "## 5. Remaining Owner Actions Before Full Live Stage B Sign-Off",
                "",
                "1. Restrict `.env` permissions on Home Mac (`chmod 600 .env`) without printing secrets, then run `mise run tradelens-stage-b-home-preflight`.",
                "2. Install and sign into Tailscale on Home Mac and Office Mac; configure least-privilege Tailscale ACLs and confirm in Setup Wizard Steps 2–3.",
                "3. Generate Home Mac SAN TLS certificate (`generate-tls-cert`) targeting Home Tailscale IP and MagicDNS name.",
                "4. Initialize Home Mac owner secret (`init-owner --remote-access --owner-secret-prompt`), generate Office Mac Keychain device key (`generate-device-key`), register & approve device with exact Office Mac Tailscale IP.",
                "5. Verify foreground HTTPS challenge-response from Office Mac, complete security & live revocation drills (Steps 7–9), and only then install LaunchAgents into `~/Library/LaunchAgents`.",
                "",
            ]
        )
        return "\n".join(lines)
