"""
TradeLens AI - Home Mac Runtime Operations CLI.

Provides non-destructive lifecycle management, launchd template rendering,
power/sleep inspection, trusted remote workstation registration/revocation,
and explicit owner-controlled shutdown.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .frontend_server import run_frontend_server
from .launchd_config import get_launchd_lifecycle_commands, render_launchd_plists
from .power_policy import PowerAndSleepMonitor
from .stage_b_setup import (
    MANUAL_CONFIRMATION_ALLOWED_STEPS,
    StageBSetupCoordinator,
    _default_python_executable,
)
from .security import (
    DeviceKeyCrypto,
    MacOSKeychainKeyStore,
    RemoteAccessManager,
    _atomic_write_json,
    _parse_ip_clean,
    normalize_allowed_peer_ips,
    parse_dotenv_content,
    redact_runtime_text,
    validate_bind_host,
)
from .supervisor import (
    DuplicateRuntimeError,
    RestartPolicy,
    SupervisedServiceSpec,
    TradeLensSupervisor,
    default_http_health_probe,
    stop_running_supervisor,
)


REPO_ROOT = Path(__file__).resolve().parents[3]

_SUPERVISOR_DEFAULTS: dict[str, Any] = {
    "bind_host": "127.0.0.1",
    "backend_port": 8000,
    "frontend_port": 8080,
    "vpn_cidr": [],
    "allowed_peer_ip": [],
    "allowed_host": [],
    "allowed_origin": [],
    "tls_cert": None,
    "tls_key": None,
    "remote_access": False,
    "allow_insecure_remote_http": False,
    "no_frontend": False,
    "caffeinate": False,
    "max_restarts": 4,
    "restart_window_sec": 300.0,
    "backoff_sec": 2.0,
    "poll_interval_sec": 2.0,
}

_RUNTIME_CONFIG_KEYS: Tuple[str, ...] = tuple(_SUPERVISOR_DEFAULTS.keys())


def _resolve_active_dotenv_path() -> Optional[Path]:
    if "TRADELENS_ENV_FILE" in os.environ:
        raw = str(os.environ.get("TRADELENS_ENV_FILE", "")).strip()
        return Path(raw) if raw else None
    return REPO_ROOT / ".env"


def _load_cli_dotenv(*, strict_perms: bool = False) -> Optional[Path]:
    """
    Load environment variables strictly from REPO_ROOT/.env (or TRADELENS_ENV_FILE).
    Never walks parent directories above REPO_ROOT. When strict_perms=True and the file
    exists, fails closed unless owner-only (mode & 0o077 == 0).
    """
    target = _resolve_active_dotenv_path()
    if target is None or not target.exists():
        return None
    if target.is_symlink():
        raise PermissionError(f"Refusing symlinked env file '{target}'.")
    if strict_perms:
        mode = stat.S_IMODE(target.stat().st_mode)
        if (mode & 0o077) != 0:
            raise PermissionError(
                f"Refusing to load '{target}' with permissive mode {oct(mode)}; "
                "run `chmod 600 .env` before starting remote mode."
            )
    parsed = parse_dotenv_content(target.read_text(encoding="utf-8"), strict_syntax=True)
    for key, val in parsed.items():
        if key and (key.startswith("TRADELENS_") or key == "PORT") and key not in os.environ:
            os.environ[key] = val
    return target


def _verify_dotenv_perms_if_remote(remote_enabled: bool) -> None:
    if not remote_enabled:
        return
    target = _resolve_active_dotenv_path()
    if target is None or not target.exists():
        return
    if target.is_symlink():
        raise PermissionError(f"Refusing symlinked env file '{target}'.")
    mode = stat.S_IMODE(target.stat().st_mode)
    if (mode & 0o077) != 0:
        raise PermissionError(
            f"Refusing remote launch with permissive permissions ({oct(mode)}) on '{target}'; "
            "run `chmod 600 .env` first."
        )


def _runtime_config_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "runtime_config.json"


def _save_runtime_config(state_dir: Path | str, args: argparse.Namespace, opts: dict[str, Any]) -> Path:
    """Persist strictly non-secret supervisor runtime configuration (`0600`) for safe `restart`."""
    payload: dict[str, Any] = {
        "bind_host": str(args.bind_host),
        "backend_port": int(args.backend_port),
        "frontend_port": int(args.frontend_port),
        "vpn_cidr": list(opts["vpn_cidrs"]),
        "allowed_peer_ip": list(opts["allowed_peer_ips"]),
        "allowed_host": list(opts["allowed_hosts"]),
        "allowed_origin": list(opts["allowed_origins"]),
        "tls_cert": str(opts["tls_cert"]) if opts["tls_cert"] else None,
        "tls_key": str(opts["tls_key"]) if opts["tls_key"] else None,
        "remote_access": bool(opts["remote_enabled"]),
        "allow_insecure_remote_http": bool(getattr(args, "allow_insecure_remote_http", False)),
        "no_frontend": bool(getattr(args, "no_frontend", False)),
        "caffeinate": bool(getattr(args, "caffeinate", False)),
        "max_restarts": int(getattr(args, "max_restarts", 4)),
        "restart_window_sec": float(getattr(args, "restart_window_sec", 300.0)),
        "backoff_sec": float(getattr(args, "backoff_sec", 2.0)),
        "poll_interval_sec": float(getattr(args, "poll_interval_sec", 2.0)),
    }
    cfg_path = _runtime_config_path(state_dir)
    _atomic_write_json(cfg_path, payload)
    return cfg_path


def _load_runtime_config(state_dir: Path | str) -> dict[str, Any]:
    cfg_path = _runtime_config_path(state_dir)
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw if isinstance(raw, dict) else {}


def _default_state_dir() -> Path:
    return Path(os.environ.get("TRADELENS_RUNTIME_STATE_DIR") or (REPO_ROOT / "runtime_state"))


def _format_url_host(host: str) -> str:
    clean = (host or "127.0.0.1").strip()
    if ":" in clean and not clean.startswith("["):
        return f"[{clean}]"
    return clean


def _is_loopback_host(host: str) -> bool:
    clean = (host or "").strip().lower()
    if clean == "localhost":
        return True
    try:
        return _parse_ip_clean(clean).is_loopback
    except ValueError:
        return False


def _resolve_remote_and_tls_options(args: argparse.Namespace) -> dict[str, Any]:
    vpn_cidrs = list(getattr(args, "vpn_cidr", []) or [])
    if not vpn_cidrs and os.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").strip():
        vpn_cidrs = [c.strip() for c in os.environ["TRADELENS_ALLOWED_VPN_CIDRS"].split(",") if c.strip()]

    raw_peers = list(getattr(args, "allowed_peer_ip", []) or [])
    if not raw_peers and os.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").strip():
        raw_peers = [p.strip() for p in os.environ["TRADELENS_ALLOWED_PEER_IPS"].split(",") if p.strip()]
    allowed_peer_ips = normalize_allowed_peer_ips(raw_peers)

    allowed_hosts = list(getattr(args, "allowed_host", []) or [])
    if not allowed_hosts and os.environ.get("TRADELENS_ALLOWED_HOSTS", "").strip():
        allowed_hosts = [h.strip() for h in os.environ["TRADELENS_ALLOWED_HOSTS"].split(",") if h.strip()]

    allowed_origins = list(getattr(args, "allowed_origin", []) or [])
    if not allowed_origins and os.environ.get("TRADELENS_ALLOWED_ORIGINS", "").strip():
        allowed_origins = [o.strip() for o in os.environ["TRADELENS_ALLOWED_ORIGINS"].split(",") if o.strip()]

    tls_cert = getattr(args, "tls_cert", None) or os.environ.get("TRADELENS_TLS_CERT_FILE")
    tls_key = getattr(args, "tls_key", None) or os.environ.get("TRADELENS_TLS_KEY_FILE")
    if (tls_cert and not tls_key) or (tls_key and not tls_cert):
        raise ValueError("Both --tls-cert and --tls-key (or TRADELENS_TLS_CERT_FILE and TRADELENS_TLS_KEY_FILE) must be provided together.")

    remote_flag = bool(getattr(args, "remote_access", False)) or os.environ.get("TRADELENS_REMOTE_ACCESS_ENABLED", "0") == "1"
    is_remote = remote_flag or bool(vpn_cidrs) or bool(allowed_peer_ips) or not _is_loopback_host(args.bind_host)
    if not _is_loopback_host(args.bind_host) and not getattr(args, "allow_insecure_remote_http", False):
        if not (tls_cert and tls_key):
            raise ValueError(
                "Binding to non-loopback Tailscale/WireGuard IP requires TLS (--tls-cert and --tls-key)."
            )
    return {
        "vpn_cidrs": vpn_cidrs,
        "allowed_peer_ips": allowed_peer_ips,
        "allowed_hosts": allowed_hosts,
        "allowed_origins": allowed_origins,
        "tls_cert": tls_cert,
        "tls_key": tls_key,
        "tls_enabled": bool(tls_cert and tls_key),
        "remote_enabled": is_remote,
    }


def _build_services(args: argparse.Namespace, probe_secret: str) -> List[SupervisedServiceSpec]:
    py_exe = sys.executable or "/opt/homebrew/bin/python3.12"
    opts = _resolve_remote_and_tls_options(args)
    env_overrides = {
        "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / 'backend'}",
        "TRADELENS_BIND_HOST": args.bind_host,
        "PORT": str(args.backend_port),
        "TRADELENS_RUNTIME_STATE_DIR": str(args.state_dir),
        "TRADELENS_SUPERVISOR_PROBE_SECRET": probe_secret,
    }
    if opts["vpn_cidrs"]:
        env_overrides["TRADELENS_ALLOWED_VPN_CIDRS"] = ",".join(opts["vpn_cidrs"])
    if opts["allowed_peer_ips"]:
        env_overrides["TRADELENS_ALLOWED_PEER_IPS"] = ",".join(opts["allowed_peer_ips"])
    if opts["allowed_hosts"]:
        env_overrides["TRADELENS_ALLOWED_HOSTS"] = ",".join(opts["allowed_hosts"])
    if opts["allowed_origins"]:
        env_overrides["TRADELENS_ALLOWED_ORIGINS"] = ",".join(opts["allowed_origins"])
    if opts["tls_enabled"]:
        env_overrides["TRADELENS_TLS_CERT_FILE"] = str(opts["tls_cert"])
        env_overrides["TRADELENS_TLS_KEY_FILE"] = str(opts["tls_key"])
    if opts["remote_enabled"]:
        env_overrides["TRADELENS_REMOTE_ACCESS_ENABLED"] = "1"
    if os.environ.get("TRADELENS_STATE_HMAC_SECRET"):
        env_overrides["TRADELENS_STATE_HMAC_SECRET"] = os.environ["TRADELENS_STATE_HMAC_SECRET"]

    scheme = "https" if opts["tls_enabled"] else "http"
    url_host = _format_url_host(args.bind_host)

    services = [
        SupervisedServiceSpec(
            name="backend",
            command=[py_exe, "-m", "backend.app.main"],
            health_url=f"{scheme}://{url_host}:{args.backend_port}/api/health",
            cwd=str(REPO_ROOT),
            env=env_overrides,
            startup_grace_seconds=2.0,
            max_consecutive_health_failures=3,
        )
    ]
    if not getattr(args, "no_frontend", False):
        fe_cmd = [
            py_exe,
            "-m",
            "backend.app.runtime.cli",
            "serve-frontend",
            "--bind-host",
            args.bind_host,
            "--port",
            str(args.frontend_port),
            "--backend-port",
            str(args.backend_port),
        ]
        for cidr in opts["vpn_cidrs"]:
            fe_cmd.extend(["--vpn-cidr", cidr])
        for peer in opts["allowed_peer_ips"]:
            fe_cmd.extend(["--allowed-peer-ip", peer])
        for host in opts["allowed_hosts"]:
            fe_cmd.extend(["--allowed-host", host])
        for origin in opts["allowed_origins"]:
            fe_cmd.extend(["--allowed-origin", origin])
        if opts["tls_enabled"]:
            fe_cmd.extend(["--tls-cert", str(opts["tls_cert"]), "--tls-key", str(opts["tls_key"])])
        services.append(
            SupervisedServiceSpec(
                name="frontend",
                command=fe_cmd,
                health_url=f"{scheme}://{url_host}:{args.frontend_port}/frontend-health",
                cwd=str(REPO_ROOT),
                env=env_overrides,
                startup_grace_seconds=2.0,
                max_consecutive_health_failures=3,
            )
        )
    return services


def cmd_supervise(args: argparse.Namespace) -> int:
    prelim_remote = (
        bool(getattr(args, "remote_access", False))
        or bool(getattr(args, "vpn_cidr", None))
        or bool(getattr(args, "allowed_peer_ip", None))
        or not _is_loopback_host(args.bind_host)
    )
    _load_cli_dotenv(strict_perms=prelim_remote)
    opts = _resolve_remote_and_tls_options(args)
    _verify_dotenv_perms_if_remote(opts["remote_enabled"])
    vpn_cidrs = opts["vpn_cidrs"]
    validate_bind_host(args.bind_host, allowed_vpn_cidrs=vpn_cidrs)
    state_dir = Path(args.state_dir)
    if opts["remote_enabled"]:
        # Fail closed before spawning child listeners if TRADELENS_STATE_HMAC_SECRET is missing/weak or state tampered.
        RemoteAccessManager(
            state_dir / "remote_auth.json",
            allowed_vpn_cidrs=vpn_cidrs,
            allowed_peer_ips=opts["allowed_peer_ips"],
            allowed_hosts=opts["allowed_hosts"],
            allowed_origins=opts["allowed_origins"],
            state_hmac_secret=os.environ.get("TRADELENS_STATE_HMAC_SECRET"),
            remote_mode_enabled=True,
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    probe_secret = secrets.token_urlsafe(24)
    power_monitor = PowerAndSleepMonitor(state_dir / "heartbeat.json")
    supervisor = TradeLensSupervisor(
        state_dir=state_dir,
        services=_build_services(args, probe_secret=probe_secret),
        restart_policy=RestartPolicy(
            max_restarts=args.max_restarts,
            window_seconds=args.restart_window_sec,
            initial_backoff_seconds=args.backoff_sec,
        ),
        power_monitor=power_monitor,
        enable_caffeinate_process=args.caffeinate,
        health_probe=lambda url: default_http_health_probe(
            url,
            probe_secret=probe_secret,
            tls_ca_file=opts["tls_cert"],
        ),
    )

    def _on_signal(signum: int, _frame: object) -> None:
        supervisor.shutdown(owner_initiated=False, reason=f"SIGNAL_{signum}")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        res = supervisor.startup()
    except DuplicateRuntimeError as exc:
        print(json.dumps({"started": False, "error": str(exc)}, indent=2))
        return 1

    if not res.get("started"):
        print(json.dumps(res, indent=2))
        return 0

    _save_runtime_config(state_dir, args, opts)

    try:
        while supervisor.running:
            supervisor.step_once()
            if supervisor.states.get("backend") and supervisor.states["backend"].circuit_open:
                supervisor.shutdown(owner_initiated=False, reason="BACKEND_CIRCUIT_OPEN")
                return 2
            time.sleep(args.poll_interval_sec)
    except KeyboardInterrupt:
        supervisor.shutdown(owner_initiated=False, reason="KEYBOARD_INTERRUPT")
    return supervisor.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    status_file = state_dir / "supervisor_status.json"
    power_monitor = PowerAndSleepMonitor(state_dir / "heartbeat.json")
    out = {
        "state_dir": str(state_dir),
        "owner_shutdown_flag": (state_dir / "owner_shutdown.flag").exists(),
        "power_audit": power_monitor.get_status(),
    }
    if status_file.exists():
        with open(status_file, "r", encoding="utf-8") as fh:
            out["supervisor"] = json.load(fh)
    print(json.dumps(out, indent=2))
    return 0


def cmd_owner_shutdown(args: argparse.Namespace) -> int:
    flag = TradeLensSupervisor.request_owner_shutdown(args.state_dir, reason=args.reason)
    print(json.dumps({"owner_shutdown_flag": str(flag), "status": "OWNER_SHUTDOWN_REQUESTED"}, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    TradeLensSupervisor.clear_owner_shutdown_flag(args.state_dir)
    print(json.dumps({"status": "OWNER_SHUTDOWN_FLAG_CLEARED"}, indent=2))
    return 0


def _open_remote_manager(args: argparse.Namespace) -> RemoteAccessManager:
    state_dir = Path(args.state_dir)
    explicit_env_file = bool(os.environ.get("TRADELENS_ENV_FILE", "").strip())
    should_load_dotenv = explicit_env_file or not os.environ.get("TRADELENS_STATE_HMAC_SECRET")
    if should_load_dotenv:
        prelim_remote = (
            bool(getattr(args, "remote_access", False))
            or os.environ.get("TRADELENS_REMOTE_ACCESS_ENABLED", "0").strip() == "1"
            or bool(os.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").strip())
            or bool(os.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").strip())
        )
        _load_cli_dotenv(strict_perms=prelim_remote)
    hmac_sec = os.environ.get("TRADELENS_STATE_HMAC_SECRET")
    bind_host_env = os.environ.get("TRADELENS_BIND_HOST", "127.0.0.1")
    remote_flag = (
        bool(getattr(args, "remote_access", False))
        or os.environ.get("TRADELENS_REMOTE_ACCESS_ENABLED", "0").strip() == "1"
        or bool(os.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").strip())
        or bool(os.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").strip())
        or not _is_loopback_host(bind_host_env)
    )
    if should_load_dotenv:
        _verify_dotenv_perms_if_remote(remote_flag)
    env_cidrs = [
        c.strip()
        for c in os.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").split(",")
        if c.strip()
    ]
    env_peers = [
        p.strip()
        for p in os.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").split(",")
        if p.strip()
    ]
    return RemoteAccessManager(
        state_dir / "remote_auth.json",
        allowed_vpn_cidrs=env_cidrs,
        allowed_peer_ips=normalize_allowed_peer_ips(env_peers),
        state_hmac_secret=hmac_sec,
        remote_mode_enabled=remote_flag,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    res = stop_running_supervisor(args.state_dir, timeout_seconds=args.timeout_sec)
    print(json.dumps(res, indent=2))
    return 0 if res.get("stopped") or res.get("status") in {"NOT_RUNNING", "STALE_PID_NOT_ALIVE"} else 1


def _hydrate_restart_args(args: argparse.Namespace, prior_cfg: dict[str, Any]) -> argparse.Namespace:
    explicit_keys = {k for k in _RUNTIME_CONFIG_KEYS if hasattr(args, k)}
    allow_downgrade = bool(getattr(args, "allow_downgrade", False))

    if prior_cfg and not allow_downgrade:
        prior_remote = bool(prior_cfg.get("remote_access"))
        prior_tls = bool(prior_cfg.get("tls_cert") and prior_cfg.get("tls_key"))
        prior_bind = str(prior_cfg.get("bind_host", "127.0.0.1"))
        prior_peers = list(prior_cfg.get("allowed_peer_ip") or [])
        if "allow_insecure_remote_http" in explicit_keys and getattr(args, "allow_insecure_remote_http"):
            raise PermissionError("Refusing silent security downgrade: cannot enable --allow-insecure-remote-http on restart without --allow-downgrade.")
        if prior_remote and "bind_host" in explicit_keys and _is_loopback_host(getattr(args, "bind_host")) and not _is_loopback_host(prior_bind):
            raise PermissionError("Refusing silent security/topology downgrade from Tailscale bind to loopback on restart without --allow-downgrade.")
        if prior_tls and (("tls_cert" in explicit_keys and not getattr(args, "tls_cert")) or ("tls_key" in explicit_keys and not getattr(args, "tls_key"))):
            raise PermissionError("Refusing silent TLS downgrade on restart without --allow-downgrade.")
        if prior_peers and "allowed_peer_ip" in explicit_keys and not getattr(args, "allowed_peer_ip"):
            raise PermissionError("Refusing silent peer-IP allowlist removal on restart without --allow-downgrade.")

    merged = argparse.Namespace(**vars(args))
    for k in _RUNTIME_CONFIG_KEYS:
        if hasattr(args, k):
            val = getattr(args, k)
        elif k in prior_cfg:
            val = prior_cfg[k]
        else:
            default_v = _SUPERVISOR_DEFAULTS[k]
            val = list(default_v) if isinstance(default_v, list) else default_v
        setattr(merged, k, val)
    return merged


def cmd_restart(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    if (state_dir / "owner_shutdown.flag").exists():
        print(
            json.dumps(
                {
                    "restarted": False,
                    "reason": "OWNER_SHUTDOWN_FLAG_ACTIVE",
                    "hint": "Run `resume` first to clear explicit owner shutdown lock.",
                },
                indent=2,
            )
        )
        return 0
    prior_cfg = _load_runtime_config(state_dir)
    merged_args = _hydrate_restart_args(args, prior_cfg)
    stop_res = stop_running_supervisor(state_dir, timeout_seconds=getattr(args, "timeout_sec", 8.0))
    if stop_res.get("status") == "STOP_TIMEOUT_STILL_RUNNING":
        print(json.dumps({"restarted": False, "stop_result": stop_res}, indent=2))
        return 1
    return cmd_supervise(merged_args)


def cmd_health_check(args: argparse.Namespace) -> int:
    prior_cfg = _load_runtime_config(args.state_dir)
    bind_host = getattr(args, "bind_host", prior_cfg.get("bind_host", "127.0.0.1"))
    backend_port = int(getattr(args, "backend_port", prior_cfg.get("backend_port", 8000)))
    frontend_port = int(getattr(args, "frontend_port", prior_cfg.get("frontend_port", 8080)))
    tls_cert = (
        getattr(args, "tls_cert")
        if hasattr(args, "tls_cert")
        else (prior_cfg.get("tls_cert") or os.environ.get("TRADELENS_TLS_CERT_FILE"))
    )
    scheme = "https" if tls_cert else "http"
    host = _format_url_host(bind_host)
    backend_url = getattr(args, "url", None) or f"{scheme}://{host}:{backend_port}/api/health"
    b_ok, b_detail = default_http_health_probe(backend_url, tls_ca_file=tls_cert)
    result: dict[str, Any] = {
        "backend": {"url": backend_url, "healthy": b_ok, "detail": b_detail},
    }
    if not getattr(args, "backend_only", False) and not getattr(args, "url", None):
        fe_url = f"{scheme}://{host}:{frontend_port}/frontend-health"
        f_ok, f_detail = default_http_health_probe(fe_url, tls_ca_file=tls_cert)
        result["frontend"] = {"url": fe_url, "healthy": f_ok, "detail": f_detail}
    all_ok = all(item["healthy"] for item in result.values())
    result["overall_healthy"] = all_ok
    result["proves_live_market_data"] = False
    print(json.dumps(result, indent=2))
    return 0 if all_ok else 1


def cmd_generate_tls_cert(args: argparse.Namespace) -> int:
    import tempfile

    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    cert_out = Path(args.cert_out)
    key_out = Path(args.key_out)
    cert_out.parent.mkdir(parents=True, exist_ok=True)
    key_out.parent.mkdir(parents=True, exist_ok=True)

    bind_host = (args.bind_host or "127.0.0.1").strip()
    validate_bind_host(bind_host, allowed_vpn_cidrs=list(args.vpn_cidr or []))
    san_entries = []
    if bind_host.lower() == "localhost":
        san_entries.extend(["DNS:localhost", "IP:127.0.0.1"])
    else:
        try:
            _parse_ip_clean(bind_host)
            san_entries.append(f"IP:{bind_host.strip('[]')}")
        except ValueError:
            san_entries.append(f"DNS:{bind_host}")
    for h in args.hostname or ():
        clean_h = h.strip()
        if clean_h and f"DNS:{clean_h}" not in san_entries:
            san_entries.append(f"DNS:{clean_h}")

    san_value = ",".join(san_entries)
    days_clamped = max(1, min(int(args.days), 825))
    cfg = (
        "[req]\n"
        "distinguished_name = dn\n"
        "x509_extensions = v3_req\n"
        "prompt = no\n"
        "[dn]\n"
        f"CN = {bind_host}\n"
        "O = TradeLens AI Local Runtime\n"
        "[v3_req]\n"
        "basicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, digitalSignature, keyCertSign\n"
        "extendedKeyUsage = serverAuth\n"
        f"subjectAltName = {san_value}\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False, encoding="utf-8") as tmp_cfg:
        tmp_cfg.write(cfg)
        cfg_path = Path(tmp_cfg.name)
    try:
        proc = subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-sha256",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-pkeyopt",
                "ec_param_enc:named_curve",
                "-nodes",
                "-days",
                str(days_clamped),
                "-keyout",
                str(key_out),
                "-out",
                str(cert_out),
                "-config",
                str(cfg_path),
                "-extensions",
                "v3_req",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl certificate generation failed: {proc.stderr.strip()}")
        os.chmod(key_out, 0o600)
        os.chmod(cert_out, 0o644)

        meta_proc = subprocess.run(
            [openssl, "x509", "-in", str(cert_out), "-noout", "-fingerprint", "-sha256", "-dates"],
            capture_output=True,
            text=True,
            check=False,
        )
        verify_proc = subprocess.run(
            [openssl, "verify", "-CAfile", str(cert_out), str(cert_out)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        cfg_path.unlink(missing_ok=True)

    sha256_fp = ""
    not_before = ""
    not_after = ""
    for line in (meta_proc.stdout or "").splitlines():
        if "Fingerprint=" in line:
            sha256_fp = line.split("=", 1)[1].strip()
        elif line.startswith("notBefore="):
            not_before = line.split("=", 1)[1].strip()
        elif line.startswith("notAfter="):
            not_after = line.split("=", 1)[1].strip()

    print(
        json.dumps(
            {
                "status": "GENERATED",
                "cert_path": str(cert_out),
                "key_path": str(key_out),
                "subject_alt_name": san_value,
                "basic_constraints": "critical, CA:TRUE, pathlen:0",
                "key_usage": "critical, digitalSignature, keyCertSign",
                "extended_key_usage": "serverAuth",
                "sha256_fingerprint": sha256_fp,
                "not_before": not_before,
                "not_after": not_after,
                "self_verify_ok": verify_proc.returncode == 0,
                "key_mode": "0600",
                "days": days_clamped,
            },
            indent=2,
        )
    )
    return 0


def cmd_render_launchd(args: argparse.Namespace) -> int:
    rendered = render_launchd_plists(
        output_dir=args.output_dir,
        repo_root=REPO_ROOT,
        python_executable=args.python_exe,
        bind_host=args.bind_host,
        backend_port=args.backend_port,
        frontend_port=args.frontend_port,
        state_dir=str(args.state_dir),
        enable_caffeinate=not args.no_caffeinate,
        vpn_cidrs=list(args.vpn_cidr or []),
        allowed_peer_ips=normalize_allowed_peer_ips(list(getattr(args, "allowed_peer_ip", []) or [])),
        allowed_hosts=list(args.allowed_host or []),
        allowed_origins=list(args.allowed_origin or []),
        tls_cert_file=args.tls_cert,
        tls_key_file=args.tls_key,
        remote_access_enabled=True if args.remote_access else None,
    )
    info = {
        "rendered": {k: str(v) for k, v in rendered.items()},
        "lifecycle_commands": {
            label: get_launchd_lifecycle_commands(path, label=label)
            for label, path in rendered.items()
        },
        "note": (
            "Templates invoke deploy/scripts/run_launchd_service.sh and were written to deploy/launchd ONLY "
            "(not installed or bootstrapped into ~/Library/LaunchAgents). "
            "Provide ANGEL_* , TRADELENS_OWNER_SECRET, and TRADELENS_STATE_HMAC_SECRET via owner-only 0600 .env, never inside plist XML."
        ),
    }
    print(json.dumps(info, indent=2))
    return 0


def _resolve_secret(
    provided: Optional[str],
    env_name: str,
    prompt_text: str,
    *,
    from_stdin: bool = False,
    force_prompt: bool = False,
    confirm_prompt: bool = False,
) -> str:
    if force_prompt:
        first = getpass.getpass(prompt_text)
        if not first or not first.strip():
            raise ValueError("Secret input cannot be empty.")
        if confirm_prompt:
            second = getpass.getpass(f"Confirm {prompt_text}")
            if first != second:
                raise ValueError("Entered secrets do not match.")
        return first
    if from_stdin:
        raw = sys.stdin.readline().rstrip("\r\n")
        if not raw or not raw.strip():
            raise ValueError("Expected non-empty secret on stdin when --owner-secret-stdin is set.")
        return raw
    if provided:
        return provided
    from_env = os.environ.get(env_name)
    if from_env:
        return from_env
    first = getpass.getpass(prompt_text)
    if not first or not first.strip():
        raise ValueError("Secret input cannot be empty.")
    if confirm_prompt:
        second = getpass.getpass(f"Confirm {prompt_text}")
        if first != second:
            raise ValueError("Entered secrets do not match.")
    return first


def _resolve_cli_owner_secret(args: argparse.Namespace, *, confirm_prompt: bool = False) -> str:
    return _resolve_secret(
        getattr(args, "owner_secret", None),
        "TRADELENS_OWNER_SECRET",
        "Home Mac Owner Secret: ",
        from_stdin=bool(getattr(args, "owner_secret_stdin", False)),
        force_prompt=bool(getattr(args, "owner_secret_prompt", False)),
        confirm_prompt=confirm_prompt,
    )


def cmd_generate_device_key(args: argparse.Namespace) -> int:
    priv_pem, pub_pem = DeviceKeyCrypto.generate_keypair()
    fp = DeviceKeyCrypto.compute_public_key_fingerprint(pub_pem)
    stored_in_keychain = False
    if args.store_keychain:
        store = MacOSKeychainKeyStore(require_prompt_acl=not getattr(args, "no_prompt_acl", False))
        store.store_private_key(args.device_id, priv_pem)
        stored_in_keychain = True
    out = {
        "device_id": args.device_id,
        "public_key_fingerprint": fp,
        "public_key_pem": pub_pem,
        "stored_in_keychain": stored_in_keychain,
    }
    if args.public_key_out:
        out_path = Path(args.public_key_out)
        out_path.write_text(pub_pem, encoding="utf-8")
        out["public_key_file"] = str(out_path)
    print(json.dumps(out, indent=2))
    return 0


def cmd_delete_device_key(args: argparse.Namespace) -> int:
    store = MacOSKeychainKeyStore()
    deleted = store.delete_private_key(args.device_id)
    print(json.dumps({"device_id": args.device_id, "deleted_from_keychain": deleted}, indent=2))
    return 0 if deleted else 1


def cmd_sign_challenge(args: argparse.Namespace) -> int:
    challenge_str = getattr(args, "challenge", None)
    if getattr(args, "challenge_stdin", False):
        challenge_str = sys.stdin.readline().rstrip("\r\n")
    if not challenge_str or not str(challenge_str).strip():
        raise ValueError("A non-empty challenge string is required (--challenge or --challenge-stdin).")
    store = MacOSKeychainKeyStore()
    priv_pem = store.load_private_key(args.device_id)
    if not priv_pem:
        raise RuntimeError(f"No private key found in macOS Keychain for device_id='{args.device_id}'")
    sig = DeviceKeyCrypto.sign_challenge(priv_pem, str(challenge_str).strip())
    print(json.dumps({"device_id": args.device_id, "signature": sig}, indent=2))
    return 0


def cmd_init_owner(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    rotated = bool(mgr.owner_hash_hex)
    if rotated:
        current_sec = _resolve_secret(
            getattr(args, "current_owner_secret", None),
            "TRADELENS_CURRENT_OWNER_SECRET",
            "Current Home Mac Owner Secret: ",
            from_stdin=bool(getattr(args, "current_owner_secret_stdin", False)),
            force_prompt=bool(getattr(args, "owner_secret_prompt", False))
            or (
                not getattr(args, "current_owner_secret", None)
                and not getattr(args, "current_owner_secret_stdin", False)
            ),
        )
        if not mgr.verify_owner_secret(current_sec):
            raise PermissionError("Current Home Mac owner secret verification failed.")
    new_sec = _resolve_cli_owner_secret(args, confirm_prompt=True)
    mgr.configure_owner_secret(new_sec)
    print(
        json.dumps(
            {
                "status": "OWNER_SECRET_ROTATED" if rotated else "OWNER_SECRET_INITIALIZED",
                "state_path": str(mgr.state_path),
                "kdf_iterations": mgr.owner_kdf_iterations,
                "stored_plaintext_in_env": False,
            },
            indent=2,
        )
    )
    return 0


def cmd_register_device(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    owner_sec = _resolve_cli_owner_secret(args)
    if not mgr.owner_hash_hex:
        mgr.configure_owner_secret(owner_sec)

    pub_pem = ""
    if getattr(args, "public_key_file", None):
        pub_pem = Path(args.public_key_file).read_text(encoding="utf-8")
    elif getattr(args, "public_key_pem", None):
        pub_pem = args.public_key_pem
    else:
        raise ValueError("--public-key-file or --public-key-pem is required (public key only; private key stays on workstation).")

    allowed_ips = normalize_allowed_peer_ips(list(getattr(args, "allowed_ip", []) or []))
    rec = mgr.register_device(
        device_id=args.device_id,
        device_name=args.device_name or args.device_id,
        public_key_pem=pub_pem,
        owner_secret=owner_sec,
        approve=not getattr(args, "pending_only", False),
        allow_re_enroll=bool(getattr(args, "allow_re_enroll", False)),
        allowed_ips=allowed_ips,
    )
    out: dict[str, Any] = {
        "status": "REGISTERED",
        "device_id": rec.device_id,
        "device_name": rec.device_name,
        "device_status": rec.status,
        "public_key_fingerprint": rec.public_key_fingerprint,
        "allowed_ips": list(rec.allowed_ips),
        "registered_at": rec.registered_at,
        "approved_at": rec.approved_at,
    }
    if args.owner_secret:
        out["security_warning"] = "Avoid --owner-secret CLI arguments in shared shells; prefer --owner-secret-prompt or --owner-secret-stdin."
    print(json.dumps(out, indent=2))
    return 0


def cmd_approve_device(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    owner_sec = _resolve_cli_owner_secret(args)
    raw_ips = list(getattr(args, "allowed_ip", []) or [])
    rec = mgr.approve_device(
        device_id=args.device_id,
        owner_secret=owner_sec,
        activate=not getattr(args, "no_activate", False),
        allowed_ips=normalize_allowed_peer_ips(raw_ips) if raw_ips else None,
    )
    print(
        json.dumps(
            {
                "status": "APPROVED",
                "device_id": rec.device_id,
                "device_status": rec.status,
                "public_key_fingerprint": rec.public_key_fingerprint,
                "allowed_ips": list(rec.allowed_ips),
                "approved_at": rec.approved_at,
            },
            indent=2,
        )
    )
    return 0


def cmd_update_device_ip(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    owner_sec = _resolve_cli_owner_secret(args)
    res = mgr.update_device_allowed_ips(
        device_id=args.device_id,
        allowed_ips=list(getattr(args, "allowed_ip", []) or []),
        owner_secret=owner_sec,
        reason=args.reason,
    )
    print(json.dumps(res, indent=2))
    return 0


def cmd_revoke_device(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    owner_sec = _resolve_cli_owner_secret(args)
    count = mgr.revoke_device(device_id=args.device_id, owner_secret=owner_sec, reason=args.reason)
    print(
        json.dumps(
            {
                "status": "REVOKED",
                "device_id": args.device_id,
                "revoked_sessions_count": count,
            },
            indent=2,
        )
    )
    return 0


def cmd_list_devices(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    devices = mgr.list_devices(status_filter=getattr(args, "status", None))
    print(json.dumps({"devices": devices, "summary": mgr.summarize_connectivity()}, indent=2))
    return 0


def cmd_remove_device(args: argparse.Namespace) -> int:
    mgr = _open_remote_manager(args)
    owner_sec = _resolve_cli_owner_secret(args)
    res = mgr.remove_device(
        device_id=args.device_id,
        owner_secret=owner_sec,
        require_revoked=not getattr(args, "force_revoke", False),
        reason=args.reason,
    )
    print(json.dumps(res, indent=2))
    return 0


def cmd_broker_auth(args: argparse.Namespace) -> int:
    _load_cli_dotenv(strict_perms=False)
    admin_url = (getattr(args, "admin_url", None) or "").strip()
    if not admin_url:
        raw_port = (os.environ.get("TRADELENS_LOCAL_ADMIN_PORT") or "").strip()
        if raw_port.isdigit() and int(raw_port) > 0:
            admin_port = int(raw_port)
        else:
            prior_cfg = _load_runtime_config(args.state_dir)
            bport = int(prior_cfg.get("backend_port") or os.environ.get("PORT") or 8000)
            bind_h = str(prior_cfg.get("bind_host") or os.environ.get("TRADELENS_BIND_HOST") or "127.0.0.1")
            if _is_loopback_host(bind_h):
                admin_port = bport
            else:
                admin_port = bport + 1 if bport + 1 != 8080 else bport + 2
        admin_url = f"http://127.0.0.1:{admin_port}/api/broker/authenticate"

    parsed = urlparse(admin_url)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme != "http" or not _is_loopback_host(host):
        raise ValueError(
            "broker-auth must target a local loopback HTTP URL only (e.g. http://127.0.0.1:8001/api/broker/authenticate)."
        )
    owner_sec = _resolve_cli_owner_secret(args)
    req = urllib.request.Request(
        admin_url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-TradeLens-Owner-Secret": owner_sec,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(body, indent=2))
            return 0 if 200 <= resp.status < 300 else 1
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        print(redact_runtime_text(err_text, extra_secrets=[owner_sec]), file=sys.stderr)
        return 1


def cmd_stage_b(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "status") or "status").strip().lower()
    coord = StageBSetupCoordinator(
        repo_root=REPO_ROOT,
        state_dir=args.state_dir,
        progress_path=getattr(args, "progress_path", None),
        environ=dict(os.environ),
    )

    if action == "register-device-guide":
        mgr = _open_remote_manager(args)
        guidance = {
            "action": "register-device-guide",
            "status": "GUIDE_ONLY",
            "registered_any_device": False,
            "verification_passed": False,
            "exit_0_means_stage_b_verified": False,
            "exit_code": 0,
            "message": "Read-only guidance only. To actually register a device, run register-device with --device-id, --public-key-file, --allowed-ip, and --owner-secret-prompt.",
            "office_mac_keygen_command": (
                "python3.12 -m backend.app.runtime.cli generate-device-key "
                "--device-id <OFFICE_DEVICE_ID> --store-keychain --public-key-out /tmp/<OFFICE_DEVICE_ID>.pub.pem"
            ),
            "home_mac_register_command": (
                "python3.12 -m backend.app.runtime.cli register-device "
                "--device-id <OFFICE_DEVICE_ID> --device-name <OFFICE_MAC_LABEL> "
                "--public-key-file /tmp/<OFFICE_DEVICE_ID>.pub.pem "
                "--allowed-ip <OFFICE_TAILSCALE_IP> --pending-only --remote-access --owner-secret-prompt"
            ),
            "home_mac_approve_command": (
                "python3.12 -m backend.app.runtime.cli approve-device "
                "--device-id <OFFICE_DEVICE_ID> --allowed-ip <OFFICE_TAILSCALE_IP> "
                "--remote-access --owner-secret-prompt"
            ),
            "existing_devices": mgr.list_devices(),
        }
        print(json.dumps(guidance, indent=2))
        return 0

    if action == "register-device":
        dev_id = (getattr(args, "device_id", None) or "").strip()
        pub_file = getattr(args, "public_key_file", None)
        pub_pem = getattr(args, "public_key_pem", None)
        allowed_ips = list(getattr(args, "allowed_ip", None) or [])
        if dev_id and (pub_file or pub_pem) and allowed_ips:
            return cmd_register_device(args)
        err_payload = {
            "action": "register-device",
            "status": "MISSING_REGISTRATION_FLAGS",
            "registered_any_device": False,
            "verification_passed": False,
            "exit_code": 2,
            "error": (
                "register-device requires --device-id, --public-key-file (or --public-key-pem), "
                "--allowed-ip, and --remote-access / owner secret. "
                "Use `stage-b --action register-device-guide` for read-only instructions."
            ),
        }
        print(json.dumps(err_payload, indent=2))
        return 2

    if action == "confirm-step":
        step_id = (getattr(args, "step_id", None) or "").strip()
        if not step_id:
            raise ValueError("--step-id is required for action=confirm-step")
        state = coord.confirm_manual_step(
            step_id=step_id,
            confirmed=bool(getattr(args, "confirm", False)),
            audit_note=str(getattr(args, "note", "") or ""),
            is_loopback=True,
        )
        print(json.dumps({"stage_verdict": state["stage_verdict"], "overview": state["overview"], "steps": state["steps"], "exit_code": 0}, indent=2))
        return 0

    if action == "report":
        state = coord.get_setup_state(persist=not getattr(args, "no_save", False), is_remote_view=False)
        md = coord.build_markdown_report(state)
        if getattr(args, "stdout", False):
            print(md)
            return 0
        out_path = Path(getattr(args, "out", None) or (REPO_ROOT / "reports" / "phase3e_5_2_6_stage_b_provisioning_report.md"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": "REPORT_GENERATED",
                    "report_path": str(out_path),
                    "stage_verdict": state["stage_verdict"],
                    "verification_passed": state["stage_verdict"] == "STAGE_B_COMPLETE",
                    "exit_0_means_stage_b_verified": False,
                    "exit_code": 0,
                    "overview": state["overview"],
                },
                indent=2,
            )
        )
        return 0

    task_map = {
        "preflight": "tradelens-stage-b-preflight",
        "status": "tradelens-stage-b-status",
        "home-preflight": "tradelens-stage-b-home-preflight",
        "tailscale-check": "tradelens-stage-b-tailscale-check",
        "tls-check": "tradelens-stage-b-tls-check",
        "verify-device": "tradelens-stage-b-verify-device",
        "remote-check": "tradelens-stage-b-remote-check",
        "security-check": "tradelens-stage-b-security-check",
        "revocation-selftest": "tradelens-stage-b-revocation-selftest",
        "revocation-check": "tradelens-stage-b-revocation-check",
        "launchd-lint": "tradelens-stage-b-launchd-lint",
        "launchd-status": "tradelens-stage-b-launchd-status",
        "render-launchd": "tradelens-stage-b-render-launchd",
    }
    if action not in task_map:
        raise ValueError(f"Unsupported stage-b action: '{action}'")

    gate_actions = {
        "home-preflight",
        "tailscale-check",
        "tls-check",
        "verify-device",
        "remote-check",
        "security-check",
        "revocation-check",
        "launchd-lint",
        "launchd-status",
    }
    task_id = task_map[action]
    no_save_read_only = {
        "preflight",
        "status",
        "home-preflight",
        "tailscale-check",
        "tls-check",
        "verify-device",
        "remote-check",
        "security-check",
        "revocation-selftest",
        "revocation-check",
        "launchd-lint",
        "launchd-status",
    }
    if getattr(args, "no_save", False) and action in no_save_read_only:
        st = coord.get_setup_state(persist=False, is_remote_view=False)
        step_map_ns = {s["step_id"]: s["status"] for s in st.get("steps", [])}
        ld_ns = st.get("probes", {}).get("launchd", {})
        if action == "revocation-selftest":
            iso = coord.run_isolated_revocation_self_test()
            tr_ns: dict[str, Any] = {
                "task_id": task_id,
                "command_succeeded": True,
                "gate_task": False,
                "simulated_only": True,
                "proves_live_office_mac_revocation": False,
                "passed": bool(iso["passed"]),
                "selftest_passed": bool(iso["passed"]),
                "verification_passed": False,
                "exit_0_means_stage_b_verified": False,
                "isolated_revocation_self_test": iso,
            }
        else:
            ns_gate_map = {
                "home-preflight": step_map_ns.get("step_01_home_mac_preflight") == "VERIFIED",
                "tailscale-check": step_map_ns.get("step_02_tailscale_home_office") == "VERIFIED" and step_map_ns.get("step_03_tailscale_acl_verification") == "VERIFIED",
                "tls-check": step_map_ns.get("step_04_tls_certificate_setup") == "VERIFIED",
                "verify-device": step_map_ns.get("step_05_office_mac_device_key_gen") == "VERIFIED" and step_map_ns.get("step_06_owner_approval_device_pin") == "VERIFIED",
                "remote-check": step_map_ns.get("step_07_remote_https_challenge_verify") == "VERIFIED",
                "security-check": step_map_ns.get("step_08_security_boundary_tests") == "VERIFIED" and bool(st["probes"]["security_boundary"]["all_checks_passed"]),
                "revocation-check": step_map_ns.get("step_09_revocation_re_enrollment_test") == "VERIFIED",
                "launchd-lint": bool(ld_ns.get("all_rendered_lint_ok")),
                "launchd-status": step_map_ns.get("step_10_launchagent_installation") == "VERIFIED",
            }
            is_gate = action in ns_gate_map
            passed_val = bool(ns_gate_map[action]) if is_gate else (st.get("stage_verdict") == "STAGE_B_COMPLETE")
            tr_ns = {
                "task_id": task_id,
                "command_succeeded": True,
                "gate_task": is_gate,
                "simulated_only": False,
                "templates_only": action == "launchd-lint",
                "proves_installed_or_running": False if action == "launchd-lint" else None,
                "passed": passed_val,
                "verification_passed": passed_val,
                "exit_0_means_stage_b_verified": False,
            }
        res = {"setup_state": st, "task_result": tr_ns}
    else:
        extra: dict[str, Any] = {}
        if getattr(args, "record_live_drill", False):
            extra["record_live_drill"] = True
        if action == "render-launchd" and getattr(args, "python_exe", None):
            extra["python_executable"] = str(args.python_exe)
        res = coord.run_allowlisted_task(
            task_id=task_id,
            confirmed=bool(getattr(args, "confirm", False)),
            audit_note=str(getattr(args, "note", "") or ""),
            is_loopback=True,
            extra_args=extra,
        )

    st = res.get("setup_state", {})
    tr = res.get("task_result") or {}
    if action in gate_actions:
        exit_code = 0 if bool(tr.get("passed")) else 2
    elif action == "revocation-selftest":
        exit_code = 0 if bool(tr.get("passed")) else 2
    else:
        exit_code = 0

    stage_complete = st.get("stage_verdict") == "STAGE_B_COMPLETE"
    exit_0_verified = bool(
        stage_complete
        and action not in {"preflight", "status", "report", "launchd-lint", "revocation-selftest", "register-device-guide", "render-launchd"}
    )

    summary = {
        "action": action,
        "task_id": task_id,
        "stage_verdict": st.get("stage_verdict"),
        "stage_b_complete": stage_complete,
        "verification_passed": bool(tr.get("verification_passed")),
        "exit_0_means_stage_b_verified": exit_0_verified,
        "exit_code": exit_code,
        "state_freshness": st.get("state_freshness"),
        "is_stale": st.get("is_stale"),
        "progress_file": st.get("progress_file"),
        "overview": st.get("overview"),
        "task_result": tr,
        "steps": [
            {
                "step_number": s["step_number"],
                "step_id": s["step_id"],
                "status": s["status"],
                "locked": s["locked"],
                "failure_reason": s["failure_reason"],
            }
            for s in st.get("steps", [])
        ],
    }
    if action in {"security-check"}:
        summary["security_boundary"] = st.get("probes", {}).get("security_boundary")
    elif action in {"tailscale-check"}:
        summary["tailscale"] = st.get("probes", {}).get("tailscale")
    elif action in {"tls-check"}:
        summary["tls"] = st.get("probes", {}).get("tls")
    elif action in {"launchd-lint", "launchd-status", "render-launchd"}:
        summary["launchd"] = st.get("probes", {}).get("launchd")
    elif action in {"verify-device"}:
        summary["remote_auth"] = st.get("probes", {}).get("remote_auth")

    print(json.dumps(summary, indent=2))
    return exit_code


def _cmd_serve_frontend(args: argparse.Namespace) -> int:
    prelim_remote = (
        bool(getattr(args, "vpn_cidr", None))
        or bool(getattr(args, "allowed_peer_ip", None))
        or not _is_loopback_host(args.bind_host)
    )
    _load_cli_dotenv(strict_perms=prelim_remote)
    run_frontend_server(
        args.bind_host,
        args.port,
        args.vpn_cidr or None,
        allowed_peer_ips=normalize_allowed_peer_ips(args.allowed_peer_ip) or None,
        allowed_hosts=args.allowed_host or None,
        allowed_origins=args.allowed_origin or None,
        backend_port=args.backend_port,
        tls_certfile=args.tls_cert,
        tls_keyfile=args.tls_key,
        allow_insecure_remote_http=args.allow_insecure_remote_http,
    )
    return 0


def _add_supervisor_args(subparser: argparse.ArgumentParser, *, use_suppress_defaults: bool = False) -> None:
    d = (lambda val: argparse.SUPPRESS) if use_suppress_defaults else (lambda val: val)
    subparser.add_argument("--bind-host", default=d("127.0.0.1"))
    subparser.add_argument("--backend-port", type=int, default=d(8000))
    subparser.add_argument("--frontend-port", type=int, default=d(8080))
    subparser.add_argument("--vpn-cidr", action="append", default=argparse.SUPPRESS if use_suppress_defaults else [])
    subparser.add_argument("--allowed-peer-ip", action="append", default=argparse.SUPPRESS if use_suppress_defaults else [])
    subparser.add_argument("--allowed-host", action="append", default=argparse.SUPPRESS if use_suppress_defaults else [])
    subparser.add_argument("--allowed-origin", action="append", default=argparse.SUPPRESS if use_suppress_defaults else [])
    subparser.add_argument("--tls-cert", default=d(None))
    subparser.add_argument("--tls-key", default=d(None))
    subparser.add_argument("--remote-access", action="store_true", default=d(False))
    subparser.add_argument("--allow-insecure-remote-http", action="store_true", default=d(False))
    subparser.add_argument("--no-frontend", action="store_true", default=d(False))
    subparser.add_argument("--caffeinate", action="store_true", default=d(False))
    subparser.add_argument("--max-restarts", type=int, default=d(4))
    subparser.add_argument("--restart-window-sec", type=float, default=d(300.0))
    subparser.add_argument("--backoff-sec", type=float, default=d(2.0))
    subparser.add_argument("--poll-interval-sec", type=float, default=d(2.0))


def _add_owner_secret_args(subparser: argparse.ArgumentParser) -> None:
    grp = subparser.add_mutually_exclusive_group()
    grp.add_argument("--owner-secret", default=None)
    grp.add_argument("--owner-secret-stdin", action="store_true")
    grp.add_argument(
        "--owner-secret-prompt",
        action="store_true",
        help="Prompt interactively (hidden TTY input) for Home Mac owner secret.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TradeLens AI Home Mac Runtime CLI")
    parser.add_argument("--state-dir", default=str(_default_state_dir()))
    sub = parser.add_subparsers(dest="command", required=True)

    p_sup = sub.add_parser("supervise", help="Run single-instance runtime supervisor")
    _add_supervisor_args(p_sup)
    p_sup.set_defaults(func=cmd_supervise)

    p_start = sub.add_parser("start", help="Start single-instance runtime supervisor")
    _add_supervisor_args(p_start)
    p_start.set_defaults(func=cmd_supervise)

    p_stop = sub.add_parser("stop", help="Gracefully stop running supervisor process without owner shutdown lock")
    p_stop.add_argument("--timeout-sec", type=float, default=8.0)
    p_stop.set_defaults(func=cmd_stop)

    p_restart = sub.add_parser("restart", help="Gracefully stop and restart runtime supervisor preserving persisted config")
    _add_supervisor_args(p_restart, use_suppress_defaults=True)
    p_restart.add_argument("--timeout-sec", type=float, default=8.0)
    p_restart.add_argument("--allow-downgrade", action="store_true")
    p_restart.set_defaults(func=cmd_restart)

    p_hc = sub.add_parser("health-check", help="Probe backend and frontend health endpoints")
    p_hc.add_argument("--bind-host", default=argparse.SUPPRESS)
    p_hc.add_argument("--backend-port", type=int, default=argparse.SUPPRESS)
    p_hc.add_argument("--frontend-port", type=int, default=argparse.SUPPRESS)
    p_hc.add_argument("--tls-cert", default=argparse.SUPPRESS)
    p_hc.add_argument("--url", default=None)
    p_hc.add_argument("--backend-only", action="store_true")
    p_hc.set_defaults(func=cmd_health_check)

    p_fe = sub.add_parser("serve-frontend", help="Run hardened static frontend server")
    p_fe.add_argument("--bind-host", default="127.0.0.1")
    p_fe.add_argument("--port", type=int, default=8080)
    p_fe.add_argument("--backend-port", type=int, default=8000)
    p_fe.add_argument("--vpn-cidr", action="append", default=[])
    p_fe.add_argument("--allowed-peer-ip", action="append", default=[])
    p_fe.add_argument("--allowed-host", action="append", default=[])
    p_fe.add_argument("--allowed-origin", action="append", default=[])
    p_fe.add_argument("--tls-cert", default=None)
    p_fe.add_argument("--tls-key", default=None)
    p_fe.add_argument("--allow-insecure-remote-http", action="store_true")
    p_fe.set_defaults(func=_cmd_serve_frontend)

    p_st = sub.add_parser("status", help="Inspect supervisor & power policy status")
    p_st.set_defaults(func=cmd_status)

    p_down = sub.add_parser("owner-shutdown", help="Request explicit owner-controlled shutdown")
    p_down.add_argument("--reason", default="OWNER_CLI_SHUTDOWN")
    p_down.set_defaults(func=cmd_owner_shutdown)

    p_res = sub.add_parser("resume", help="Clear owner shutdown flag to re-allow startup")
    p_res.set_defaults(func=cmd_resume)

    p_ld = sub.add_parser("render-launchd", help="Render macOS launchd plist templates")
    p_ld.add_argument("--output-dir", default=str(REPO_ROOT / "deploy" / "launchd"))
    p_ld.add_argument("--python-exe", default=_default_python_executable(REPO_ROOT))
    p_ld.add_argument("--bind-host", default="127.0.0.1")
    p_ld.add_argument("--backend-port", type=int, default=8000)
    p_ld.add_argument("--frontend-port", type=int, default=8080)
    p_ld.add_argument("--vpn-cidr", action="append", default=[])
    p_ld.add_argument("--allowed-peer-ip", action="append", default=[])
    p_ld.add_argument("--allowed-host", action="append", default=[])
    p_ld.add_argument("--allowed-origin", action="append", default=[])
    p_ld.add_argument("--tls-cert", default=None)
    p_ld.add_argument("--tls-key", default=None)
    p_ld.add_argument("--remote-access", action="store_true")
    p_ld.add_argument("--no-caffeinate", action="store_true")
    p_ld.set_defaults(func=cmd_render_launchd)

    p_cert = sub.add_parser("generate-tls-cert", help="Generate self-signed SAN TLS cert for private VPN host")
    p_cert.add_argument("--bind-host", required=True)
    p_cert.add_argument("--hostname", action="append", default=[])
    p_cert.add_argument("--vpn-cidr", action="append", default=[])
    p_cert.add_argument("--cert-out", required=True)
    p_cert.add_argument("--key-out", required=True)
    p_cert.add_argument("--days", type=int, default=365)
    p_cert.set_defaults(func=cmd_generate_tls_cert)

    p_gk = sub.add_parser("generate-device-key", help="Generate ECDSA P-256 keypair on workstation")
    p_gk.add_argument("--device-id", required=True)
    p_gk.add_argument("--store-keychain", action="store_true")
    p_gk.add_argument("--no-prompt-acl", action="store_true")
    p_gk.add_argument("--public-key-out", default=None)
    p_gk.set_defaults(func=cmd_generate_device_key)

    p_dk = sub.add_parser("delete-device-key", help="Delete workstation private key from macOS Keychain")
    p_dk.add_argument("--device-id", required=True)
    p_dk.set_defaults(func=cmd_delete_device_key)

    p_sc = sub.add_parser("sign-challenge", help="Sign Home Mac auth challenge using Keychain private key")
    p_sc.add_argument("--device-id", required=True)
    ch_grp = p_sc.add_mutually_exclusive_group(required=True)
    ch_grp.add_argument("--challenge", default=None)
    ch_grp.add_argument("--challenge-stdin", action="store_true")
    p_sc.set_defaults(func=cmd_sign_challenge)

    p_owner = sub.add_parser("init-owner", help="Initialize or rotate Home Mac PBKDF2 owner secret in remote_auth.json")
    _add_owner_secret_args(p_owner)
    cur_grp = p_owner.add_mutually_exclusive_group()
    cur_grp.add_argument("--current-owner-secret", default=None)
    cur_grp.add_argument("--current-owner-secret-stdin", action="store_true")
    p_owner.add_argument("--remote-access", action="store_true")
    p_owner.set_defaults(func=cmd_init_owner)

    p_reg = sub.add_parser("register-device", help="Register a trusted office workstation public key")
    p_reg.add_argument("--device-id", required=True)
    p_reg.add_argument("--device-name", default="")
    p_reg.add_argument("--public-key-file", default=None)
    p_reg.add_argument("--public-key-pem", default=None)
    p_reg.add_argument("--allowed-ip", action="append", default=[])
    p_reg.add_argument("--pending-only", action="store_true")
    p_reg.add_argument("--allow-re-enroll", action="store_true")
    _add_owner_secret_args(p_reg)
    p_reg.add_argument("--remote-access", action="store_true")
    p_reg.set_defaults(func=cmd_register_device)

    p_app = sub.add_parser("approve-device", help="Approve pending remote workstation on Home Mac")
    p_app.add_argument("--device-id", required=True)
    p_app.add_argument("--allowed-ip", action="append", default=[])
    p_app.add_argument("--no-activate", action="store_true")
    _add_owner_secret_args(p_app)
    p_app.add_argument("--remote-access", action="store_true")
    p_app.set_defaults(func=cmd_approve_device)

    p_uip = sub.add_parser(
        "update-device-ip",
        help="Update pinned exact peer IP(s) for a device and invalidate existing sessions",
    )
    p_uip.add_argument("--device-id", required=True)
    p_uip.add_argument("--allowed-ip", action="append", required=True)
    p_uip.add_argument("--reason", default="DEVICE_ALLOWED_IP_UPDATED")
    _add_owner_secret_args(p_uip)
    p_uip.add_argument("--remote-access", action="store_true")
    p_uip.set_defaults(func=cmd_update_device_ip)

    p_rev = sub.add_parser("revoke-device", help="Revoke a remote workstation immediately")
    p_rev.add_argument("--device-id", required=True)
    p_rev.add_argument("--reason", default="OWNER_REVOKED")
    _add_owner_secret_args(p_rev)
    p_rev.add_argument("--remote-access", action="store_true")
    p_rev.set_defaults(func=cmd_revoke_device)

    p_ls = sub.add_parser("list-devices", help="List registered remote workstations on Home Mac")
    p_ls.add_argument("--status", default=None)
    p_ls.add_argument("--remote-access", action="store_true")
    p_ls.set_defaults(func=cmd_list_devices)

    p_rm = sub.add_parser("remove-device", help="Permanently purge a revoked workstation from Home Mac registry")
    p_rm.add_argument("--device-id", required=True)
    p_rm.add_argument("--reason", default="REMOVED_BY_OWNER")
    p_rm.add_argument("--force-revoke", action="store_true")
    _add_owner_secret_args(p_rm)
    p_rm.add_argument("--remote-access", action="store_true")
    p_rm.set_defaults(func=cmd_remove_device)

    p_ba = sub.add_parser(
        "broker-auth",
        help="Trigger explicit owner Angel One broker authentication on local loopback admin port",
    )
    p_ba.add_argument("--admin-url", default=None)
    _add_owner_secret_args(p_ba)
    p_ba.set_defaults(func=cmd_broker_auth)

    p_sb = sub.add_parser(
        "stage-b",
        help="Run Stage B provisioning preflight, status, verification checks, or report generation (broker-auth & order execution excluded)",
    )
    p_sb.add_argument(
        "--action",
        default="status",
        choices=[
            "preflight",
            "status",
            "home-preflight",
            "tailscale-check",
            "tls-check",
            "verify-device",
            "remote-check",
            "security-check",
            "revocation-selftest",
            "revocation-check",
            "render-launchd",
            "launchd-lint",
            "launchd-status",
            "register-device-guide",
            "register-device",
            "confirm-step",
            "report",
        ],
    )
    p_sb.add_argument("--step-id", choices=sorted(MANUAL_CONFIRMATION_ALLOWED_STEPS), default=None)
    p_sb.add_argument("--confirm", action="store_true")
    p_sb.add_argument("--note", default="")
    p_sb.add_argument("--no-save", action="store_true")
    p_sb.add_argument("--record-live-drill", action="store_true")
    p_sb.add_argument("--python-exe", default=None)
    p_sb.add_argument("--progress-path", default=None)
    p_sb.add_argument("--out", default=None)
    p_sb.add_argument("--stdout", action="store_true")
    p_sb.add_argument("--device-id", default=None)
    p_sb.add_argument("--device-name", default="")
    p_sb.add_argument("--public-key-file", default=None)
    p_sb.add_argument("--public-key-pem", default=None)
    p_sb.add_argument("--allowed-ip", action="append", default=[])
    p_sb.add_argument("--pending-only", action="store_true")
    p_sb.add_argument("--allow-re-enroll", action="store_true")
    p_sb.add_argument("--remote-access", action="store_true")
    _add_owner_secret_args(p_sb)
    p_sb.set_defaults(func=cmd_stage_b)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
