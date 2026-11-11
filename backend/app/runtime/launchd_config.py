"""
TradeLens AI - macOS launchd Configuration Templates & Safe Lifecycle Generator.

Generates validated LaunchAgent property lists (`<!DOCTYPE plist ...>`) for the Home Mac
without blindly mutating user `~/Library/LaunchAgents` unless explicitly invoked.
Uses `KeepAlive = {"SuccessfulExit": False}` so intentional owner shutdown
(exit code 0) remains stopped rather than respawning infinitely.
Avoids top-level `plistlib` / `pyexpat` import so Homebrew Python 3.12 on macOS 26
never fails on `libexpat` symbol mismatches.
"""

from __future__ import annotations

import os
from html import escape as _xml_escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


HOME_ENGINE_LABEL = "com.tradelens.home-engine"
FRONTEND_SERVER_LABEL = "com.tradelens.frontend-dashboard"


def _render_plist_value(val: Any, indent: int) -> List[str]:
    pad = "  " * indent
    lines: List[str] = []
    if isinstance(val, bool):
        lines.append(f"{pad}<{'true' if val else 'false'}/>")
    elif isinstance(val, int):
        lines.append(f"{pad}<integer>{val}</integer>")
    elif isinstance(val, str):
        lines.append(f"{pad}<string>{_xml_escape(val)}</string>")
    elif isinstance(val, list):
        lines.append(f"{pad}<array>")
        for item in val:
            lines.extend(_render_plist_value(item, indent + 1))
        lines.append(f"{pad}</array>")
    elif isinstance(val, dict):
        lines.append(f"{pad}<dict>")
        for k, v in val.items():
            lines.append(f"{pad}  <key>{_xml_escape(str(k))}</key>")
            lines.extend(_render_plist_value(v, indent + 1))
        lines.append(f"{pad}</dict>")
    else:
        raise TypeError(f"Unsupported plist value type: {type(val).__name__}")
    return lines


def dumps_plist_xml(plist_dict: Dict[str, Any]) -> str:
    header = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
    ]
    body = _render_plist_value(plist_dict, indent=0)
    footer = ["</plist>", ""]
    return "\n".join(header + body + footer)


def build_home_engine_plist(
    repo_root: Path | str,
    python_executable: str = "/opt/homebrew/bin/python3.12",
    bind_host: str = "127.0.0.1",
    backend_port: int = 8000,
    frontend_port: int = 8080,
    state_dir: Optional[str] = None,
    log_dir: Optional[str] = None,
    enable_caffeinate: bool = True,
    vpn_cidrs: Optional[Sequence[str]] = None,
    allowed_peer_ips: Optional[Sequence[str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_origins: Optional[Sequence[str]] = None,
    tls_cert_file: Optional[str] = None,
    tls_key_file: Optional[str] = None,
    remote_access_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    root = Path(repo_root).resolve()
    wrapper_script = str(root / "deploy" / "scripts" / "run_launchd_service.sh")
    resolved_state = state_dir or str(root / "runtime_state")
    resolved_logs = log_dir or str(root / "runtime_logs")
    vpn_list = [str(c).strip() for c in (vpn_cidrs or ()) if str(c).strip()]
    peer_list = [str(p).strip() for p in (allowed_peer_ips or ()) if str(p).strip()]
    host_list = [str(h).strip() for h in (allowed_hosts or ()) if str(h).strip()]
    origin_list = [str(o).strip() for o in (allowed_origins or ()) if str(o).strip()]
    is_remote = (
        bool(remote_access_enabled)
        if remote_access_enabled is not None
        else (bool(vpn_list) or bool(peer_list) or bind_host.strip() not in {"127.0.0.1", "::1", "localhost"})
    )

    prog_args: List[str] = [
        wrapper_script,
        python_executable,
        "-m",
        "backend.app.runtime.cli",
        "supervise",
        "--bind-host",
        bind_host,
        "--backend-port",
        str(backend_port),
        "--frontend-port",
        str(frontend_port),
        "--state-dir",
        resolved_state,
    ]
    for cidr in vpn_list:
        prog_args.extend(["--vpn-cidr", cidr])
    for peer in peer_list:
        prog_args.extend(["--allowed-peer-ip", peer])
    for host in host_list:
        prog_args.extend(["--allowed-host", host])
    for origin in origin_list:
        prog_args.extend(["--allowed-origin", origin])
    if tls_cert_file and tls_key_file:
        prog_args.extend(["--tls-cert", str(tls_cert_file), "--tls-key", str(tls_key_file)])
    if is_remote:
        prog_args.append("--remote-access")
    if enable_caffeinate:
        prog_args.append("--caffeinate")

    env_vars: Dict[str, str] = {
        "PYTHONPATH": f"{root}:{root / 'backend'}",
        "PYTHONUNBUFFERED": "1",
        "TRADELENS_BIND_HOST": bind_host,
        "TRADELENS_RUNTIME_STATE_DIR": resolved_state,
    }
    if vpn_list:
        env_vars["TRADELENS_ALLOWED_VPN_CIDRS"] = ",".join(vpn_list)
    if peer_list:
        env_vars["TRADELENS_ALLOWED_PEER_IPS"] = ",".join(peer_list)
    if host_list:
        env_vars["TRADELENS_ALLOWED_HOSTS"] = ",".join(host_list)
    if origin_list:
        env_vars["TRADELENS_ALLOWED_ORIGINS"] = ",".join(origin_list)
    if tls_cert_file and tls_key_file:
        env_vars["TRADELENS_TLS_CERT_FILE"] = str(tls_cert_file)
        env_vars["TRADELENS_TLS_KEY_FILE"] = str(tls_key_file)
    if is_remote:
        env_vars["TRADELENS_REMOTE_ACCESS_ENABLED"] = "1"

    return {
        "Label": HOME_ENGINE_LABEL,
        "ProgramArguments": prog_args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": True,
        "KeepAlive": {
            "SuccessfulExit": False,
        },
        "ThrottleInterval": 30,
        "ExitTimeOut": 15,
        "ProcessType": "Interactive",
        "StandardOutPath": f"{resolved_logs}/launchd.home-engine.stdout.log",
        "StandardErrorPath": f"{resolved_logs}/launchd.home-engine.stderr.log",
    }


def build_frontend_plist(
    repo_root: Path | str,
    python_executable: str = "/opt/homebrew/bin/python3.12",
    bind_host: str = "127.0.0.1",
    frontend_port: int = 8080,
    backend_port: int = 8000,
    log_dir: Optional[str] = None,
    vpn_cidrs: Optional[Sequence[str]] = None,
    allowed_peer_ips: Optional[Sequence[str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_origins: Optional[Sequence[str]] = None,
    tls_cert_file: Optional[str] = None,
    tls_key_file: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(repo_root).resolve()
    wrapper_script = str(root / "deploy" / "scripts" / "run_launchd_service.sh")
    resolved_logs = log_dir or str(root / "runtime_logs")
    vpn_list = [str(c).strip() for c in (vpn_cidrs or ()) if str(c).strip()]
    peer_list = [str(p).strip() for p in (allowed_peer_ips or ()) if str(p).strip()]
    host_list = [str(h).strip() for h in (allowed_hosts or ()) if str(h).strip()]
    origin_list = [str(o).strip() for o in (allowed_origins or ()) if str(o).strip()]

    prog_args: List[str] = [
        wrapper_script,
        python_executable,
        "-m",
        "backend.app.runtime.cli",
        "serve-frontend",
        "--bind-host",
        bind_host,
        "--port",
        str(frontend_port),
        "--backend-port",
        str(backend_port),
    ]
    for cidr in vpn_list:
        prog_args.extend(["--vpn-cidr", cidr])
    for peer in peer_list:
        prog_args.extend(["--allowed-peer-ip", peer])
    for host in host_list:
        prog_args.extend(["--allowed-host", host])
    for origin in origin_list:
        prog_args.extend(["--allowed-origin", origin])
    if tls_cert_file and tls_key_file:
        prog_args.extend(["--tls-cert", str(tls_cert_file), "--tls-key", str(tls_key_file)])

    env_vars: Dict[str, str] = {
        "PYTHONPATH": f"{root}:{root / 'backend'}",
        "PYTHONUNBUFFERED": "1",
    }
    if peer_list:
        env_vars["TRADELENS_ALLOWED_PEER_IPS"] = ",".join(peer_list)
    if tls_cert_file and tls_key_file:
        env_vars["TRADELENS_TLS_CERT_FILE"] = str(tls_cert_file)
        env_vars["TRADELENS_TLS_KEY_FILE"] = str(tls_key_file)

    return {
        "Label": FRONTEND_SERVER_LABEL,
        "ProgramArguments": prog_args,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": env_vars,
        "RunAtLoad": False,
        "KeepAlive": {
            "SuccessfulExit": False,
        },
        "ThrottleInterval": 15,
        "StandardOutPath": f"{resolved_logs}/launchd.frontend.stdout.log",
        "StandardErrorPath": f"{resolved_logs}/launchd.frontend.stderr.log",
    }


def render_launchd_plists(
    output_dir: Path | str,
    repo_root: Path | str,
    **kwargs: Any,
) -> Dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    engine_plist = build_home_engine_plist(repo_root=repo_root, **kwargs)
    fe_kwargs = {
        k: kwargs[k]
        for k in (
            "python_executable",
            "bind_host",
            "frontend_port",
            "backend_port",
            "log_dir",
            "vpn_cidrs",
            "allowed_peer_ips",
            "allowed_hosts",
            "allowed_origins",
            "tls_cert_file",
            "tls_key_file",
        )
        if k in kwargs
    }
    frontend_plist = build_frontend_plist(repo_root=repo_root, **fe_kwargs)

    engine_path = out / f"{HOME_ENGINE_LABEL}.plist"
    frontend_path = out / f"{FRONTEND_SERVER_LABEL}.plist"

    engine_path.write_text(dumps_plist_xml(engine_plist), encoding="utf-8")
    frontend_path.write_text(dumps_plist_xml(frontend_plist), encoding="utf-8")

    return {
        HOME_ENGINE_LABEL: engine_path,
        FRONTEND_SERVER_LABEL: frontend_path,
    }


def get_launchd_lifecycle_commands(
    plist_path: Path | str,
    label: str = HOME_ENGINE_LABEL,
) -> Dict[str, List[str]]:
    uid = os.getuid()
    return {
        "validate": ["plutil", "-lint", str(plist_path)],
        "bootstrap": ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        "enable": ["launchctl", "enable", f"gui/{uid}/{label}"],
        "status": ["launchctl", "print", f"gui/{uid}/{label}"],
        "bootout": ["launchctl", "bootout", f"gui/{uid}/{label}"],
    }
