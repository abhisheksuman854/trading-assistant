"""
Security, private-network binding guards, secret redaction, asymmetric device identity,
challenge-response authentication, Host/Origin/CSP protections, and session revocation
for Home Mac <-> Office Mac remote availability.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from backend.app.data.historical_acquisition import redact_sensitive_text


# Tailscale IPv4 CGNAT (100.64.0.0/10) and IPv6 ULA prefix (fd7a:115c:a1e0::/48)
TAILSCALE_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_ULA_NET = ipaddress.ip_network("fd7a:115c:a1e0::/48")
LOOPBACK_V4_NET = ipaddress.ip_network("127.0.0.0/8")
LOOPBACK_V6_NET = ipaddress.ip_network("::1/128")

# Explicit device lifecycle states:
# pending approval -> approved -> active -> revoked
DEVICE_STATUS_PENDING = "PENDING_APPROVAL"
DEVICE_STATUS_APPROVED = "APPROVED"
DEVICE_STATUS_ACTIVE = "ACTIVE"
DEVICE_STATUS_REVOKED = "REVOKED"
VALID_DEVICE_STATUSES: Set[str] = {
    DEVICE_STATUS_PENDING,
    DEVICE_STATUS_APPROVED,
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_REVOKED,
}

# Strictly explicit allowlist of operations & HTTP methods permitted to an authenticated
# remote office workstation. No arbitrary shell, no filesystem browsing, no credential export,
# no broker connect, no ledger mutation, no host shutdown.
ALLOWED_REMOTE_ROUTE_METHODS: Dict[str, Set[str]] = {
    "/api/health": {"GET"},
    "/api/runtime/status": {"GET"},
    "/api/scan": {"GET"},
    "/api/indices": {"GET"},
    "/api/positions": {"GET"},
    "/api/positions/monitor": {"POST"},
    "/api/verify-tip": {"POST"},
    "/api/universe": {"GET"},
    "/api/ledger": {"GET"},
    "/api/remote/heartbeat": {"POST"},
    "/api/remote/logout": {"POST"},
    "/api/setup/state": {"GET"},
}

ALLOWED_REMOTE_ENDPOINTS: Set[str] = set(ALLOWED_REMOTE_ROUTE_METHODS.keys())
ALLOWED_REMOTE_PREFIXES: Tuple[str, ...] = ("/api/stock/",)

# Strictly Home Mac owner-only endpoints (must NEVER be exposed remotely or over proxy)
HOME_OWNER_ONLY_ENDPOINTS: Set[str] = {
    "/api/runtime/shutdown",
    "/api/broker/authenticate",
    "/api/remote/devices",
    "/api/remote/devices/register",
    "/api/remote/devices/approve",
    "/api/remote/devices/revoke",
    "/api/remote/devices/remove",
    "/api/setup/run-task",
    "/api/setup/step/confirm",
    "/api/ledger/trade",
    "/api/ledger/trade/delete",
    "/api/ledger/clear",
    "/api/ledger/import-chat",
    "/api/ledger/settings",
}

ORDER_EXECUTION_BLOCKED_PREFIXES: Tuple[str, ...] = (
    "/api/orders",
    "/api/order",
    "/api/trade",
    "/api/execute",
    "/api/broker/order",
    "/api/broker/execute",
)


def is_forbidden_order_execution_path(path: str) -> bool:
    """Block all order/trade execution paths unconditionally (order execution is disabled)."""
    norm = (path or "").strip().lower()
    if not norm:
        return False
    for prefix in ORDER_EXECUTION_BLOCKED_PREFIXES:
        if norm == prefix or norm.startswith(prefix + "/"):
            return True
    return False

PBKDF2_DEFAULT_ITERATIONS = 600_000
PBKDF2_LEGACY_ITERATIONS = 120_000
PBKDF2_MIN_VERIFY_ITERATIONS = 100_000
MIN_OWNER_SECRET_LENGTH = 12
MIN_STATE_HMAC_SECRET_LENGTH = 16
RECOMMENDED_STATE_HMAC_SECRET_LENGTH = 32

ALLOWED_REMOTE_METHODS: Set[str] = {"GET", "POST", "OPTIONS"}

PROXY_HEADER_NAMES: Tuple[str, ...] = (
    "x-forwarded-for",
    "forwarded",
    "tailscale-user-login",
    "tailscale-user-name",
    "x-real-ip",
    "via",
)

FORBIDDEN_QUERY_TOKEN_PARAMS: Set[str] = {
    "token",
    "access_token",
    "session_token",
    "bearer",
    "auth",
    "authorization",
    "owner_secret",
    "device_secret",
    "private_key",
    "signature",
    "api_key",
    "apikey",
    "jwt",
    "secret",
}

# Sensitive env / secret key names never allowed in logs, responses, or office browser storage.
SENSITIVE_ENV_KEYS: Tuple[str, ...] = (
    "ANGEL_API_KEY",
    "ANGEL_CLIENT_CODE",
    "ANGEL_PASSWORD",
    "ANGEL_TOTP_SECRET",
    "TRADELENS_OWNER_SECRET",
    "TRADELENS_DEVICE_SECRET",
    "TRADELENS_STATE_HMAC_SECRET",
)

_ENV_ASSIGN_RE = re.compile(
    r"(?i)\b("
    r"ANGEL_API_KEY|ANGEL_CLIENT_CODE|ANGEL_PASSWORD|ANGEL_TOTP_SECRET|"
    r"TRADELENS_OWNER_SECRET|TRADELENS_DEVICE_SECRET|TRADELENS_STATE_HMAC_SECRET|"
    r"owner_secret|device_secret|private_key|private_key_pem|jwtToken|refreshToken|feedToken|"
    r"access_token|session_token|totp_secret|api_key|password"
    r")\s*([=:])\s*([^\s,;\"'\}]+|\"[^\"]*\"|'[^']*')"
)

_JSON_SECRET_RE = re.compile(
    r'(?i)("?(?:ANGEL_API_KEY|ANGEL_CLIENT_CODE|ANGEL_PASSWORD|ANGEL_TOTP_SECRET|'
    r'owner_secret|device_secret|private_key|private_key_pem|jwtToken|refreshToken|feedToken|'
    r'access_token|session_token|totp|password|apiKey|api_key|clientcode|authorization)"?\s*:\s*)"([^"]*)"'
)

_JWT_LIKE_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_PRIVATE_KEY_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_SAFE_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_SAFE_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTENV_EXPORT_RE = re.compile(r"^export[ \t]+(.*)$")


def _strip_horizontal_ws(text: str) -> str:
    return text.strip(" \t")


def parse_dotenv_content(content: str, *, strict_syntax: bool = True) -> Dict[str, str]:
    """
    Parse `.env` content identically to `deploy/scripts/run_launchd_service.sh`.

    Handles UTF-8 BOM, CRLF line endings, indented blank/comment lines, optional
    `export ` prefix, strict `[A-Za-z_][A-Za-z0-9_]*` identifiers, single-quoted
    literals (`'...'`), double-quoted values (`"..."` with `\\"` and `\\\\`),
    unquoted values with optional whitespace-prefixed `# comment` suffixes, and
    fail-closed syntax rejection without leaking secret values in exceptions.
    """
    parsed: Dict[str, str] = {}
    clean_content = content[1:] if content.startswith("\ufeff") else content
    for line_no, raw_line in enumerate(clean_content.splitlines(), start=1):
        line = _strip_horizontal_ws(raw_line.rstrip("\r"))
        if not line or line.startswith("#"):
            continue
        exp_match = _DOTENV_EXPORT_RE.match(line)
        if exp_match:
            line = _strip_horizontal_ws(exp_match.group(1))
        if "=" not in line:
            if strict_syntax:
                raise ValueError(f"Malformed .env syntax at line {line_no}: missing '=' assignment.")
            continue
        raw_key, raw_val = line.split("=", 1)
        key = _strip_horizontal_ws(raw_key)
        if not _DOTENV_KEY_RE.fullmatch(key):
            if strict_syntax:
                raise ValueError(f"Malformed .env syntax at line {line_no}: invalid variable identifier.")
            continue
        val_text = _strip_horizontal_ws(raw_val)
        if not val_text:
            parsed[key] = ""
            continue
        if val_text.startswith("'"):
            close_idx = val_text.find("'", 1)
            if close_idx == -1:
                if strict_syntax:
                    raise ValueError(f"Malformed .env syntax at line {line_no}: unclosed single quote.")
                continue
            trailing = _strip_horizontal_ws(val_text[close_idx + 1 :])
            if trailing and not trailing.startswith("#"):
                if strict_syntax:
                    raise ValueError(
                        f"Malformed .env syntax at line {line_no}: unexpected trailing tokens after quote."
                    )
                continue
            parsed[key] = val_text[1:close_idx]
            continue
        if val_text.startswith('"'):
            out_chars: List[str] = []
            idx = 1
            closed = False
            n = len(val_text)
            while idx < n:
                ch = val_text[idx]
                if ch == "\\":
                    if idx + 1 >= n:
                        break
                    nxt = val_text[idx + 1]
                    if nxt in {'"', "\\"}:
                        out_chars.append(nxt)
                    else:
                        out_chars.append("\\")
                        out_chars.append(nxt)
                    idx += 2
                    continue
                if ch == '"':
                    trailing = _strip_horizontal_ws(val_text[idx + 1 :])
                    if trailing and not trailing.startswith("#"):
                        if strict_syntax:
                            raise ValueError(
                                f"Malformed .env syntax at line {line_no}: unexpected trailing tokens after quote."
                            )
                        closed = False
                        break
                    closed = True
                    break
                out_chars.append(ch)
                idx += 1
            if not closed:
                if strict_syntax:
                    raise ValueError(f"Malformed .env syntax at line {line_no}: unclosed double quote.")
                continue
            parsed[key] = "".join(out_chars)
            continue

        if '"' in val_text or "'" in val_text:
            if strict_syntax:
                raise ValueError(f"Malformed .env syntax at line {line_no}: unexpected quote inside unquoted value.")
            continue
        comment_idx: Optional[int] = None
        for idx, ch in enumerate(val_text):
            if ch == "#" and idx > 0 and val_text[idx - 1] in (" ", "\t"):
                comment_idx = idx
                break
        effective = val_text[:comment_idx] if comment_idx is not None else val_text
        parsed[key] = _strip_horizontal_ws(effective)
    return parsed


class RefusePublicBindError(ValueError):
    """Raised when attempting to bind a listener onto 0.0.0.0, ::, or a public IP."""


class DeviceCryptoError(ValueError):
    """Raised when device key generation, parsing, or signature verification fails."""


def _openssl_bin() -> str:
    return shutil.which("openssl") or "/usr/bin/openssl"


def _security_bin() -> str:
    return shutil.which("security") or "/usr/bin/security"


def _parse_ip_clean(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    cleaned = addr.strip()
    if "%" in cleaned:
        cleaned = cleaned.split("%", 1)[0]
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return ipaddress.ip_address(cleaned)


def _extract_hostname_without_port(raw_host: str) -> Tuple[bool, str]:
    raw = (raw_host or "").strip()
    if not raw or any(ch.isspace() for ch in raw) or "@" in raw or "/" in raw or "\\" in raw:
        return False, ""
    if raw.startswith("["):
        if "]" not in raw:
            return False, ""
        inner, rest = raw[1:].split("]", 1)
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                return False, ""
            port = int(rest[1:])
            if port < 1 or port > 65535:
                return False, ""
        return True, inner
    if raw.count(":") == 1:
        host_part, port_part = raw.split(":", 1)
        if not port_part.isdigit():
            return False, ""
        port = int(port_part)
        if port < 1 or port > 65535:
            return False, ""
        return True, host_part
    if ":" in raw:
        # Unbracketed IPv6 literal without port
        try:
            _parse_ip_clean(raw)
            return True, raw
        except ValueError:
            return False, ""
    return True, raw


def _is_tailscale_or_custom_vpn_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    extra_cidrs: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    if ip_obj in TAILSCALE_CGNAT_NET or ip_obj in TAILSCALE_ULA_NET:
        return True, "TAILSCALE_MESH_IP"
    if extra_cidrs:
        for cidr in extra_cidrs:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if ip_obj in net:
                    return True, f"CONFIGURED_VPN_CIDR:{cidr}"
            except ValueError:
                continue
    return False, "NOT_IN_APPROVED_VPN_RANGE"


def is_allowed_bind_host(
    host: str,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    """
    Ensure HTTP listeners only bind to loopback (127.0.0.1 / ::1) or an explicit
    private VPN tunnel interface (Tailscale 100.64.0.0/10 / fd7a:115c:a1e0::/48 or
    configured WireGuard subnet). Rejects 0.0.0.0, ::, hostnames resolving ambiguously,
    and public internet IPs.
    """
    raw = (host or "").strip()
    if not raw or raw in {"0.0.0.0", "::", "[::]", "*"}:
        return False, "PUBLIC_WILDCARD_BIND_FORBIDDEN"
    if raw.lower() == "localhost":
        return True, "LOOPBACK"
    try:
        ip_obj = _parse_ip_clean(raw)
    except ValueError:
        return False, "NON_LITERAL_OR_INVALID_BIND_HOST"

    if ip_obj.is_unspecified:
        return False, "UNSPECIFIED_BIND_FORBIDDEN"
    if ip_obj.is_loopback:
        return True, "LOOPBACK"

    ok_vpn, vpn_reason = _is_tailscale_or_custom_vpn_ip(ip_obj, extra_cidrs=allowed_vpn_cidrs)
    if ok_vpn:
        return True, vpn_reason

    if ip_obj.is_private:
        return False, "UNAPPROVED_PRIVATE_LAN_BIND_FORBIDDEN"

    return False, "PUBLIC_IP_BIND_FORBIDDEN"


def validate_bind_host(
    host: str,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
) -> str:
    allowed, reason = is_allowed_bind_host(host, allowed_vpn_cidrs=allowed_vpn_cidrs)
    if not allowed:
        raise RefusePublicBindError(
            f"Refusing to bind HTTP server to '{host}': {reason}. "
            "Use 127.0.0.1 or a private Tailscale/WireGuard tunnel IP."
        )
    return host.strip()


def normalize_allowed_peer_ips(allowed_peer_ips: Optional[Sequence[str]]) -> List[str]:
    """
    Validate and normalize an explicit remote peer IP allowlist.
    Only literal IP addresses (or /32 IPv4, /128 IPv6 host specs) are accepted so
    a broad range like 100.64.0.0/10 cannot silently weaken peer pinning.
    """
    if not allowed_peer_ips:
        return []
    normalized: List[str] = []
    for raw in allowed_peer_ips:
        clean = str(raw or "").strip()
        if not clean:
            continue
        if "/" in clean:
            net = ipaddress.ip_network(clean, strict=True)
            max_prefix = 32 if net.version == 4 else 128
            if net.prefixlen != max_prefix:
                raise ValueError(
                    f"Peer IP allowlist entry '{clean}' must be an exact host IP (or /{max_prefix}), "
                    "not a broad subnet CIDR."
                )
            ip_str = str(net.network_address)
        else:
            ip_str = str(_parse_ip_clean(clean))
        if ip_str not in normalized:
            normalized.append(ip_str)
    return normalized


def _ip_matches_peer_allowlist(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allowed_peer_ips: Sequence[str],
) -> bool:
    for entry in allowed_peer_ips:
        clean = str(entry or "").strip()
        if not clean:
            continue
        try:
            if ip_obj == _parse_ip_clean(clean):
                return True
        except ValueError:
            continue
    return False


def is_allowed_remote_client_ip(
    client_ip: str,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
    allowed_peer_ips: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    """
    Validate transport peer address independently of application auth tokens.
    Only loopback or approved private WireGuard/Tailscale overlay CIDRs are accepted.
    When `allowed_peer_ips` is configured, non-loopback peers must also match an
    explicitly approved Tailscale/VPN peer IP (never treating 100.64.0.0/10 alone as
    proof of Office Mac identity).
    """
    try:
        ip_obj = _parse_ip_clean(client_ip)
    except ValueError:
        return False, "INVALID_CLIENT_IP"

    if ip_obj.is_loopback:
        return True, "LOOPBACK"

    ok_vpn, vpn_reason = _is_tailscale_or_custom_vpn_ip(ip_obj, extra_cidrs=allowed_vpn_cidrs)
    if not ok_vpn:
        return False, "UNTRUSTED_NETWORK"

    clean_peers = [str(p).strip() for p in (allowed_peer_ips or []) if str(p or "").strip()]
    if clean_peers and not _ip_matches_peer_allowlist(ip_obj, clean_peers):
        return False, "UNAPPROVED_TAILSCALE_PEER"

    return True, vpn_reason


def is_allowed_host_header(
    host_header: Optional[str],
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
) -> Tuple[bool, str]:
    """
    Validate HTTP Host header against DNS rebinding and unexpected hostnames.
    Accepts loopback, approved VPN literal IPs, or explicitly configured hostnames
    (e.g. Tailscale MagicDNS hostname).
    """
    ok_fmt, hostname = _extract_hostname_without_port(host_header or "")
    if not ok_fmt or not hostname:
        return False, "INVALID_HOST_HEADER"

    low_host = hostname.lower().rstrip(".")
    if low_host in {"localhost", "127.0.0.1", "::1"}:
        return True, "LOOPBACK_HOST"

    if allowed_hosts:
        normalized = {h.strip().lower().rstrip(".") for h in allowed_hosts if h and h.strip()}
        if low_host in normalized:
            return True, "CONFIGURED_ALLOWED_HOST"

    try:
        ip_obj = _parse_ip_clean(hostname)
        if ip_obj.is_loopback:
            return True, "LOOPBACK_IP_HOST"
        ok_vpn, vpn_reason = _is_tailscale_or_custom_vpn_ip(ip_obj, extra_cidrs=allowed_vpn_cidrs)
        if ok_vpn:
            return True, vpn_reason
        return False, "UNAPPROVED_IP_HOST_HEADER"
    except ValueError:
        return False, "UNAPPROVED_HOSTNAME_HOST_HEADER"


def is_allowed_origin(
    origin_header: Optional[str],
    allowed_origins: Optional[Sequence[str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
) -> Tuple[bool, Optional[str], str]:
    """
    Strictly validate Origin headers (never returns Access-Control-Allow-Origin: *).
    Returns (allowed, normalized_origin_to_echo, reason_code).
    """
    raw = (origin_header or "").strip()
    if not raw:
        return True, None, "NO_ORIGIN_HEADER"

    if raw.lower() == "null":
        return False, None, "NULL_ORIGIN_FORBIDDEN"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        return False, None, "UNAPPROVED_ORIGIN_SCHEME"
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False, None, "MALFORMED_ORIGIN_HEADER"
    if not parsed.hostname:
        return False, None, "MISSING_ORIGIN_HOST"

    canonical_origin = f"{parsed.scheme}://{parsed.netloc}"

    if allowed_origins:
        explicit = {o.strip().rstrip("/") for o in allowed_origins if o and o.strip()}
        if canonical_origin.rstrip("/") in explicit:
            return True, canonical_origin.rstrip("/"), "EXPLICIT_ALLOWED_ORIGIN"
        return False, None, "ORIGIN_NOT_IN_ALLOWLIST"

    host_ok, _ = is_allowed_host_header(
        parsed.netloc,
        allowed_hosts=allowed_hosts,
        allowed_vpn_cidrs=allowed_vpn_cidrs,
    )
    if not host_ok:
        return False, None, "ORIGIN_HOST_FORBIDDEN"

    return True, canonical_origin.rstrip("/"), "ALLOWED_PRIVATE_ORIGIN"


def has_forbidden_url_secret(query: str) -> Tuple[bool, Optional[str]]:
    """Reject URL query parameters attempting to transmit tokens or secrets."""
    if not query:
        return False, None
    parsed = parse_qs(query, keep_blank_values=True)
    for key in parsed:
        if key.strip().lower() in FORBIDDEN_QUERY_TOKEN_PARAMS:
            return True, key.strip().lower()
    raw_low = unquote(query).lower()
    if "bearer " in raw_low:
        return True, "bearer"
    return False, None


def build_csp_header(
    script_nonce: Optional[str] = None,
    connect_origins: Optional[Sequence[str]] = None,
) -> str:
    """
    Construct a strict Content-Security-Policy with zero third-party script origins,
    no 'unsafe-inline'/'unsafe-eval' in script-src, and nonce-gated inline execution.
    """
    script_src = f"'nonce-{script_nonce}'" if script_nonce else "'self'"
    connect_parts = ["'self'"]
    for origin in connect_origins or ():
        clean = (origin or "").strip().rstrip("/")
        if clean and clean not in connect_parts:
            connect_parts.append(clean)
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        f"connect-src {' '.join(connect_parts)}; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )


def build_security_headers(
    allowed_origin: Optional[str] = None,
    script_nonce: Optional[str] = None,
    connect_origins: Optional[Sequence[str]] = None,
    is_html: bool = False,
) -> List[Tuple[str, str]]:
    """Return defensive HTTP security headers without wildcard CORS."""
    headers: List[Tuple[str, str]] = [
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
        ("Vary", "Origin"),
    ]
    if is_html:
        headers.append(
            (
                "Content-Security-Policy",
                build_csp_header(script_nonce=script_nonce, connect_origins=connect_origins),
            )
        )
    else:
        headers.append(("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"))
    if allowed_origin:
        headers.append(("Access-Control-Allow-Origin", allowed_origin))
        headers.append(("Access-Control-Allow-Methods", "GET, POST, OPTIONS"))
        headers.append(
            (
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Requested-With, X-TradeLens-Owner-Secret",
            )
        )
    return headers


def render_hardened_html(
    raw_html_bytes: bytes,
    connect_origins: Optional[Sequence[str]] = None,
) -> Tuple[bytes, List[Tuple[str, str]]]:
    """Inject a per-response CSP nonce into frontend HTML and return hardened headers."""
    nonce = secrets.token_urlsafe(16)
    html_text = raw_html_bytes.decode("utf-8", errors="strict")
    rendered = html_text.replace("__CSP_NONCE__", nonce).encode("utf-8")
    headers = build_security_headers(
        script_nonce=nonce,
        connect_origins=connect_origins,
        is_html=True,
    )
    return rendered, headers


def collect_known_secrets(
    environ: Optional[Dict[str, str]] = None,
    extra_secrets: Optional[Iterable[Optional[str]]] = None,
) -> List[str]:
    env = environ if environ is not None else os.environ
    collected: List[str] = []
    seen: Set[str] = set()
    for key in SENSITIVE_ENV_KEYS:
        val = env.get(key)
        if val and val.strip() and val.strip() not in seen:
            seen.add(val.strip())
            collected.append(val.strip())
    if extra_secrets:
        for item in extra_secrets:
            if item and isinstance(item, str) and len(item.strip()) >= 4 and item.strip() not in seen:
                seen.add(item.strip())
                collected.append(item.strip())
    return collected


def redact_runtime_text(
    text: Any,
    extra_secrets: Optional[Iterable[Optional[str]]] = None,
    environ: Optional[Dict[str, str]] = None,
) -> str:
    """Redact broker credentials, bearer tokens, JWTs, private keys, and secrets."""
    raw = "" if text is None else str(text)
    known = collect_known_secrets(environ=environ, extra_secrets=extra_secrets)
    scrubbed = redact_sensitive_text(raw, extra_secrets=known)
    scrubbed = _PRIVATE_KEY_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", scrubbed)
    scrubbed = _JSON_SECRET_RE.sub(r'\1"[REDACTED]"', scrubbed)
    scrubbed = _ENV_ASSIGN_RE.sub(r"\1\2[REDACTED]", scrubbed)
    scrubbed = _JWT_LIKE_RE.sub("[REDACTED_JWT]", scrubbed)
    return scrubbed


def redact_payload(
    value: Any,
    extra_secrets: Optional[Iterable[Optional[str]]] = None,
    environ: Optional[Dict[str, str]] = None,
) -> Any:
    """Recursively redact sensitive dictionary keys and string secrets."""
    sensitive_keys = {
        "angel_api_key",
        "angel_client_code",
        "angel_password",
        "angel_totp_secret",
        "api_key",
        "apikey",
        "password",
        "totp",
        "totp_secret",
        "jwttoken",
        "jwt_token",
        "refreshtoken",
        "refresh_token",
        "feedtoken",
        "feed_token",
        "owner_secret",
        "device_secret",
        "private_key",
        "private_key_pem",
        "session_token",
        "access_token",
        "authorization",
    }
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).strip().lower() in sensitive_keys:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_payload(v, extra_secrets=extra_secrets, environ=environ)
        return out
    if isinstance(value, list):
        return [redact_payload(item, extra_secrets=extra_secrets, environ=environ) for item in value]
    if isinstance(value, str):
        return redact_runtime_text(value, extra_secrets=extra_secrets, environ=environ)
    return value


class RedactingLogFilter(logging.Filter):
    """Logging filter that guarantees all emitted log messages are secret-redacted."""

    def __init__(self, extra_secrets_provider: Optional[Any] = None) -> None:
        super().__init__()
        self._extra_secrets_provider = extra_secrets_provider

    def filter(self, record: logging.LogRecord) -> bool:
        extra = self._extra_secrets_provider() if callable(self._extra_secrets_provider) else None
        msg = record.getMessage()
        record.msg = redact_runtime_text(msg, extra_secrets=extra)
        record.args = ()
        return True


class DeviceKeyCrypto:
    """
    Standard ECDSA P-256 (prime256v1) + SHA-256 asymmetric device identity crypto
    backed by system OpenSSL (/usr/bin/openssl). Avoids any custom cryptographic primitives.
    """

    @staticmethod
    def _run_openssl(args: Sequence[str], stdin_bytes: Optional[bytes] = None) -> bytes:
        proc = subprocess.run(
            [_openssl_bin(), *args],
            input=stdin_bytes,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise DeviceCryptoError(
                redact_runtime_text(proc.stderr.decode("utf-8", errors="replace") or "OpenSSL command failed")
            )
        return proc.stdout

    @classmethod
    def generate_keypair(cls) -> Tuple[str, str]:
        """Generate a unique ECDSA P-256 (prime256v1) (private_key_pem, public_key_pem) pair."""
        with tempfile.TemporaryDirectory(prefix="tl_key_") as tmp:
            os.chmod(tmp, 0o700)
            priv_path = Path(tmp) / "priv.pem"
            pub_path = Path(tmp) / "pub.pem"
            cls._run_openssl(
                ["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(priv_path)]
            )
            os.chmod(priv_path, 0o600)
            cls._run_openssl(["ec", "-in", str(priv_path), "-pubout", "-out", str(pub_path)])
            priv_pem = priv_path.read_text(encoding="utf-8")
            pub_pem = pub_path.read_text(encoding="utf-8")
        return priv_pem, pub_pem

    @classmethod
    def extract_public_key_pem(cls, private_key_pem: str) -> str:
        if not private_key_pem or "PRIVATE KEY" not in private_key_pem:
            raise DeviceCryptoError("Valid PEM private key required.")
        with tempfile.TemporaryDirectory(prefix="tl_pub_") as tmp:
            os.chmod(tmp, 0o700)
            priv_path = Path(tmp) / "priv.pem"
            priv_path.write_text(private_key_pem, encoding="utf-8")
            os.chmod(priv_path, 0o600)
            out = cls._run_openssl(["ec", "-in", str(priv_path), "-pubout"])
        return out.decode("utf-8").strip() + "\n"

    @classmethod
    def normalize_public_key_pem(cls, public_key_pem: str) -> Tuple[str, str]:
        """
        Validate that a supplied string is strictly a public key (never a private key)
        and return (canonical_spki_pem, sha256_fingerprint_hex).
        """
        raw = (public_key_pem or "").strip()
        if not raw:
            raise DeviceCryptoError("Public key PEM cannot be empty.")
        if "PRIVATE KEY" in raw.upper():
            raise DeviceCryptoError("Refusing private key material on Home Mac registry; submit public key only.")
        with tempfile.TemporaryDirectory(prefix="tl_chkpub_") as tmp:
            os.chmod(tmp, 0o700)
            pub_path = Path(tmp) / "pub.pem"
            pub_path.write_text(raw + "\n", encoding="utf-8")
            os.chmod(pub_path, 0o600)
            der_bytes = cls._run_openssl(["pkey", "-pubin", "-in", str(pub_path), "-inform", "PEM", "-outform", "DER"])
            pem_bytes = cls._run_openssl(["pkey", "-pubin", "-in", str(pub_path), "-inform", "PEM", "-outform", "PEM"])
        fp = hashlib.sha256(der_bytes).hexdigest()
        return pem_bytes.decode("utf-8").strip() + "\n", fp

    @classmethod
    def compute_public_key_fingerprint(cls, public_key_pem: str) -> str:
        _, fp = cls.normalize_public_key_pem(public_key_pem)
        return fp

    @classmethod
    def sign_challenge(cls, private_key_pem: str, challenge_payload: str) -> str:
        """Sign challenge bytes with ECDSA SHA-256 and return base64 signature."""
        if not private_key_pem or "PRIVATE KEY" not in private_key_pem:
            raise DeviceCryptoError("Valid private key required to sign challenge.")
        with tempfile.TemporaryDirectory(prefix="tl_sign_") as tmp:
            os.chmod(tmp, 0o700)
            priv_path = Path(tmp) / "priv.pem"
            msg_path = Path(tmp) / "msg.bin"
            priv_path.write_text(private_key_pem, encoding="utf-8")
            os.chmod(priv_path, 0o600)
            msg_path.write_bytes(challenge_payload.encode("utf-8"))
            sig_bytes = cls._run_openssl(["dgst", "-sha256", "-sign", str(priv_path), str(msg_path)])
        return base64.b64encode(sig_bytes).decode("ascii")

    @classmethod
    def verify_signature(cls, public_key_pem: str, challenge_payload: str, signature_b64: str) -> bool:
        """Verify base64-encoded ECDSA SHA-256 signature against registered public key PEM."""
        if not public_key_pem or not challenge_payload or not signature_b64:
            return False
        try:
            sig_bytes = base64.b64decode(signature_b64.strip(), validate=True)
        except Exception:
            return False
        if not sig_bytes:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="tl_ver_") as tmp:
                os.chmod(tmp, 0o700)
                pub_path = Path(tmp) / "pub.pem"
                msg_path = Path(tmp) / "msg.bin"
                sig_path = Path(tmp) / "sig.bin"
                pub_path.write_text(public_key_pem, encoding="utf-8")
                os.chmod(pub_path, 0o600)
                msg_path.write_bytes(challenge_payload.encode("utf-8"))
                sig_path.write_bytes(sig_bytes)
                proc = subprocess.run(
                    [
                        _openssl_bin(),
                        "dgst",
                        "-sha256",
                        "-verify",
                        str(pub_path),
                        "-signature",
                        str(sig_path),
                        str(msg_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return proc.returncode == 0 and "Verified OK" in (proc.stdout or "")
        except Exception:
            return False


class DevicePrivateKeyStore:
    """Abstract client-side private key store interface (never used on Home Mac server)."""

    def store_private_key(self, device_id: str, private_key_pem: str) -> None:
        raise NotImplementedError

    def load_private_key(self, device_id: str) -> Optional[str]:
        raise NotImplementedError

    def delete_private_key(self, device_id: str) -> bool:
        raise NotImplementedError


class InMemoryKeyStore(DevicePrivateKeyStore):
    """Volatile RAM-only private key store for tests or ephemeral remote sessions."""

    def __init__(self) -> None:
        self._keys: Dict[str, str] = {}

    def store_private_key(self, device_id: str, private_key_pem: str) -> None:
        self._keys[device_id] = private_key_pem

    def load_private_key(self, device_id: str) -> Optional[str]:
        return self._keys.get(device_id)

    def delete_private_key(self, device_id: str) -> bool:
        return self._keys.pop(device_id, None) is not None


class MacOSKeychainKeyStore(DevicePrivateKeyStore):
    """
    Office Mac private key storage backed by macOS Keychain via `/usr/bin/security`.

    Security boundaries & limitations (explicitly documented):
    - Uses generic-password Keychain items (`add-generic-password`), NOT hardware
      Secure Enclave keys (`SecKeyCreateRandomKey` + `SecAccessControl` would require
      native Swift/Security.framework code).
    - When `require_prompt_acl=True` (default), `-T ""` strips automatic creator-app
      ACL trust so macOS Keychain requires explicit user authorization rather than
      silently releasing the item to any unsigned creator binary.
    - Same-user process risk: once Keychain releases the base64 PEM via `-w`, the
      private key passes through Python RAM and `/usr/bin/openssl` stdin/temp-file during
      local challenge signing. Never run untrusted tools under the same macOS UID.
    """

    def __init__(
        self,
        service_name: str = "com.tradelens.ai.remote-device-key",
        cmd_runner: Optional[Callable[[List[str]], Tuple[int, str, str]]] = None,
        require_prompt_acl: bool = True,
        allowed_app_paths: Sequence[str] = (),
    ) -> None:
        self.service_name = service_name
        self._cmd_runner = cmd_runner or self._default_runner
        self.require_prompt_acl = bool(require_prompt_acl)
        self.allowed_app_paths = tuple(str(p) for p in allowed_app_paths if str(p).strip())

    @staticmethod
    def _default_runner(cmd: List[str]) -> Tuple[int, str, str]:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr

    @staticmethod
    def _validate_device_id(device_id: str) -> str:
        clean_id = (device_id or "").strip()
        if not _SAFE_DEVICE_ID_RE.fullmatch(clean_id):
            raise ValueError("device_id must be 3-64 safe characters [A-Za-z0-9._-].")
        return clean_id

    def _build_add_command(self, clean_id: str, encoded_b64: str) -> List[str]:
        cmd = [
            _security_bin(),
            "add-generic-password",
            "-U",
            "-a",
            clean_id,
            "-s",
            self.service_name,
            "-w",
            encoded_b64,
        ]
        if self.allowed_app_paths:
            for app_path in self.allowed_app_paths:
                cmd.extend(["-T", app_path])
        elif self.require_prompt_acl:
            # Explicit empty -T removes default creator-app silent Keychain access.
            cmd.extend(["-T", ""])
        return cmd

    def store_private_key(self, device_id: str, private_key_pem: str) -> None:
        clean_id = self._validate_device_id(device_id)
        pem_text = (private_key_pem or "").strip()
        if "PRIVATE KEY" not in pem_text:
            raise DeviceCryptoError("Refusing to store empty or non-PEM private key in Keychain.")
        encoded = base64.b64encode(pem_text.encode("utf-8")).decode("ascii")
        cmd = self._build_add_command(clean_id, encoded)
        rc, _, err = self._cmd_runner(cmd)
        if rc != 0:
            # Some macOS versions reject `-U` ACL updates on an existing item; delete cleanly and re-add once.
            err_low = (err or "").lower()
            if rc == 45 or "already exists" in err_low or "duplicate" in err_low or "errsecduplicateitem" in err_low:
                self.delete_private_key(clean_id)
                rc, _, err = self._cmd_runner(cmd)
        if rc != 0:
            raise RuntimeError(f"Failed storing private key in macOS Keychain: {redact_runtime_text(err)}")

    def has_private_key(self, device_id: str) -> bool:
        """Check whether a generic-password entry exists without extracting `-w` secret bytes."""
        clean_id = self._validate_device_id(device_id)
        rc, _, _ = self._cmd_runner(
            [
                _security_bin(),
                "find-generic-password",
                "-a",
                clean_id,
                "-s",
                self.service_name,
            ]
        )
        return rc == 0

    def load_private_key(self, device_id: str) -> Optional[str]:
        clean_id = self._validate_device_id(device_id)
        rc, out, err = self._cmd_runner(
            [
                _security_bin(),
                "find-generic-password",
                "-a",
                clean_id,
                "-s",
                self.service_name,
                "-w",
            ]
        )
        err_low = (err or "").lower()
        if rc != 0:
            if rc == 44 or "could not be found" in err_low or "not found" in err_low:
                return None
            raise RuntimeError(
                f"Failed loading private key from macOS Keychain for '{clean_id}': {redact_runtime_text(err)}"
            )
        raw = (out or "").strip()
        if not raw:
            return None
        try:
            decoded = base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
        except Exception as exc:
            if "PRIVATE KEY" in raw:
                return raw
            raise DeviceCryptoError(
                f"Corrupted private key data returned by macOS Keychain for '{clean_id}'."
            ) from exc
        if "PRIVATE KEY" not in decoded:
            raise DeviceCryptoError(
                f"Decoded Keychain payload for '{clean_id}' does not contain a valid PEM private key."
            )
        return decoded

    def delete_private_key(self, device_id: str) -> bool:
        clean_id = self._validate_device_id(device_id)
        rc, _, _ = self._cmd_runner(
            [
                _security_bin(),
                "delete-generic-password",
                "-a",
                clean_id,
                "-s",
                self.service_name,
            ]
        )
        return rc == 0


def _pbkdf2_hash(
    secret_value: str,
    salt_hex: Optional[str] = None,
    iterations: int = PBKDF2_DEFAULT_ITERATIONS,
) -> Tuple[str, str]:
    eff_iters = max(PBKDF2_MIN_VERIFY_ITERATIONS, int(iterations))
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret_value.encode("utf-8"), salt, eff_iters)
    return salt.hex(), digest.hex()


def _verify_pbkdf2(
    secret_value: str,
    salt_hex: str,
    expected_hash_hex: str,
    iterations: int = PBKDF2_DEFAULT_ITERATIONS,
) -> bool:
    if not secret_value or not salt_hex or not expected_hash_hex:
        return False
    if int(iterations) < PBKDF2_MIN_VERIFY_ITERATIONS:
        return False
    _, candidate = _pbkdf2_hash(secret_value, salt_hex=salt_hex, iterations=int(iterations))
    return hmac.compare_digest(candidate, expected_hash_hex)


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


class AuthRateLimiter:
    """Sliding-window brute-force limiter & temporary lockout per (device_id, ip) and per ip."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 300,
        lockout_seconds: int = 300,
        max_ip_failures: int = 12,
    ) -> None:
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = max(10, int(window_seconds))
        self.lockout_seconds = max(10, int(lockout_seconds))
        self.max_ip_failures = max(self.max_failures, int(max_ip_failures))
        self._failures: Dict[str, List[datetime]] = {}
        self._locked_until: Dict[str, datetime] = {}

    def _prune(self, key: str, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        items = [t for t in self._failures.get(key, []) if t >= cutoff]
        if items:
            self._failures[key] = items
        else:
            self._failures.pop(key, None)
        locked = self._locked_until.get(key)
        if locked and now >= locked:
            self._locked_until.pop(key, None)

    def check_allowed(self, device_id: str, client_ip: str, now: datetime) -> Tuple[bool, int]:
        keys = (f"devip:{device_id}|{client_ip}", f"ip:{client_ip}")
        max_retry = 0
        for key in keys:
            self._prune(key, now)
            until = self._locked_until.get(key)
            if until and now < until:
                rem = max(1, int((until - now).total_seconds()))
                max_retry = max(max_retry, rem)
        return max_retry == 0, max_retry

    def record_failure(self, device_id: str, client_ip: str, now: datetime) -> int:
        pairs = (
            (f"devip:{device_id}|{client_ip}", self.max_failures),
            (f"ip:{client_ip}", self.max_ip_failures),
        )
        lock_seconds = 0
        for key, threshold in pairs:
            self._prune(key, now)
            arr = self._failures.setdefault(key, [])
            arr.append(now)
            if len(arr) >= threshold:
                self._locked_until[key] = now + timedelta(seconds=self.lockout_seconds)
                lock_seconds = max(lock_seconds, self.lockout_seconds)
        return lock_seconds

    def reset_success(self, device_id: str, client_ip: str) -> None:
        self._failures.pop(f"devip:{device_id}|{client_ip}", None)
        self._locked_until.pop(f"devip:{device_id}|{client_ip}", None)
        self._failures.pop(f"ip:{client_ip}", None)
        self._locked_until.pop(f"ip:{client_ip}", None)


@dataclass
class TrustedDeviceRecord:
    device_id: str
    device_name: str
    role: str
    public_key_pem: str
    public_key_fingerprint: str
    status: str  # PENDING_APPROVAL | APPROVED | ACTIVE | REVOKED
    registered_at: str
    approved_at: Optional[str] = None
    last_auth_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revocation_reason: Optional[str] = None
    re_enrollment_count: int = 0
    allowed_ips: List[str] = field(default_factory=list)


@dataclass
class AuthChallengeRecord:
    challenge_id: str
    device_id: str
    public_key_fingerprint: str
    nonce: str
    client_ip: str
    challenge_string: str
    issued_at: str
    expires_at: str
    used: bool = False
    used_at: Optional[str] = None


@dataclass
class RemoteSessionRecord:
    session_id: str
    token_hash_hex: str
    device_id: str
    public_key_fingerprint: str
    challenge_id: str
    issued_at: str
    expires_at: str
    last_seen_at: str
    client_ip: str
    revoked: bool = False
    revoked_at: Optional[str] = None
    revoked_reason: Optional[str] = None


@dataclass
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    message: str
    status_code: int = 200
    device_id: Optional[str] = None
    session_id: Optional[str] = None
    client_ip: str = ""
    is_loopback: bool = False
    is_owner_verified: bool = False
    allowed_origin: Optional[str] = None


class RemoteAccessManager:
    """
    Home Mac security boundary separating private network transport from
    cryptographic device identity, key-only challenge-response remote auth,
    independent Home Mac owner controls, Host/Origin validation,
    rate-limited login protection, and HMAC-protected persisted session revocation.
    """

    def __init__(
        self,
        state_path: Path | str,
        owner_secret: Optional[str] = None,
        allowed_vpn_cidrs: Optional[Sequence[str]] = None,
        allowed_hosts: Optional[Sequence[str]] = None,
        allowed_origins: Optional[Sequence[str]] = None,
        session_ttl_seconds: int = 900,
        max_session_ttl_seconds: int = 3600,
        challenge_ttl_seconds: int = 60,
        max_active_challenges_per_device: int = 5,
        max_auth_failures: int = 5,
        auth_failure_window_seconds: int = 300,
        lockout_seconds: int = 300,
        require_auth_on_loopback: bool = False,
        trust_loopback_proxy: bool = True,
        tls_enabled: bool = False,
        state_hmac_secret: Optional[str] = None,
        remote_mode_enabled: Optional[bool] = None,
        require_state_hmac: Optional[bool] = None,
        clock: Optional[Callable[[], datetime]] = None,
        allowed_peer_ips: Optional[Sequence[str]] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.allowed_vpn_cidrs = list(allowed_vpn_cidrs or [])
        self.allowed_peer_ips = normalize_allowed_peer_ips(allowed_peer_ips)
        self.allowed_hosts = list(allowed_hosts or [])
        self.allowed_origins = list(allowed_origins or [])
        self.remote_mode_enabled = (
            bool(remote_mode_enabled)
            if remote_mode_enabled is not None
            else bool(self.allowed_vpn_cidrs)
        )
        self.require_state_hmac = (
            bool(require_state_hmac)
            if require_state_hmac is not None
            else self.remote_mode_enabled
        )
        clean_hmac = state_hmac_secret.strip() if isinstance(state_hmac_secret, str) else ""
        if self.require_state_hmac and len(clean_hmac) < MIN_STATE_HMAC_SECRET_LENGTH:
            raise PermissionError(
                f"TRADELENS_STATE_HMAC_SECRET (minimum {MIN_STATE_HMAC_SECRET_LENGTH} chars; "
                f"{RECOMMENDED_STATE_HMAC_SECRET_LENGTH}+ chars recommended) is mandatory when remote mode is enabled."
            )
        if state_hmac_secret is not None and len(clean_hmac) < MIN_STATE_HMAC_SECRET_LENGTH:
            raise ValueError(
                f"state_hmac_secret must be at least {MIN_STATE_HMAC_SECRET_LENGTH} non-whitespace characters."
            )
        self._state_hmac_secret: Optional[str] = clean_hmac or None

        self.session_ttl_seconds = max(60, min(session_ttl_seconds, max_session_ttl_seconds))
        self.max_session_ttl_seconds = max_session_ttl_seconds
        self.challenge_ttl_seconds = max(15, min(challenge_ttl_seconds, 300))
        self.max_active_challenges_per_device = max(1, int(max_active_challenges_per_device))
        self.require_auth_on_loopback = require_auth_on_loopback
        self.trust_loopback_proxy = trust_loopback_proxy
        self.tls_enabled = bool(tls_enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ephemeral_tokens: Set[str] = set()
        self._challenges: Dict[str, AuthChallengeRecord] = {}
        self.rate_limiter = AuthRateLimiter(
            max_failures=max_auth_failures,
            window_seconds=auth_failure_window_seconds,
            lockout_seconds=lockout_seconds,
        )

        self._mem_lock = threading.RLock()
        self._flock_depth = 0
        self._state_lock_path = self.state_path.with_suffix(".lock")
        self._last_state_sig: Optional[Tuple[int, int, int]] = None
        self._state_ever_existed: bool = False

        self.owner_salt_hex: Optional[str] = None
        self.owner_hash_hex: Optional[str] = None
        self.owner_kdf_iterations: int = PBKDF2_DEFAULT_ITERATIONS
        self.devices: Dict[str, TrustedDeviceRecord] = {}
        self.sessions: Dict[str, RemoteSessionRecord] = {}
        self.load_state()
        if owner_secret:
            self.configure_owner_secret(owner_secret)

    def _get_state_sig(self) -> Optional[Tuple[int, int, int]]:
        try:
            st = self.state_path.stat()
        except FileNotFoundError:
            return None
        return (int(st.st_mtime_ns), int(st.st_ino), int(st.st_size))

    @contextmanager
    def _state_file_lock(self) -> Iterator[None]:
        with self._mem_lock:
            if self._flock_depth > 0:
                self._flock_depth += 1
                try:
                    yield
                finally:
                    self._flock_depth -= 1
                return
            self._state_lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._state_lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._flock_depth = 1
                try:
                    yield
                finally:
                    self._flock_depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _sync_state_from_disk_if_changed(self) -> None:
        with self._mem_lock:
            sig = self._get_state_sig()
            if sig is None:
                if self._state_ever_existed:
                    # State file vanished unexpectedly: fail closed by clearing in-memory trusts/sessions.
                    self.devices.clear()
                    self.sessions.clear()
                    self._challenges.clear()
                    self._last_state_sig = None
                return
            if sig != self._last_state_sig:
                self.load_state()
                stale_cids = [
                    cid
                    for cid, ch in self._challenges.items()
                    if ch.device_id not in self.devices
                    or self.devices[ch.device_id].status != DEVICE_STATUS_ACTIVE
                    or self.devices[ch.device_id].public_key_fingerprint != ch.public_key_fingerprint
                ]
                for cid in stale_cids:
                    self._challenges.pop(cid, None)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    def active_ephemeral_secrets(self) -> List[str]:
        return list(self._ephemeral_tokens)

    def configure_owner_secret(self, owner_secret: str) -> None:
        clean = (owner_secret or "").strip()
        if len(clean) < MIN_OWNER_SECRET_LENGTH or len(set(clean)) < 5:
            raise ValueError(
                f"Owner secret must be at least {MIN_OWNER_SECRET_LENGTH} non-trivial characters."
            )
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            salt_hex, hash_hex = _pbkdf2_hash(owner_secret, iterations=PBKDF2_DEFAULT_ITERATIONS)
            self.owner_salt_hex = salt_hex
            self.owner_hash_hex = hash_hex
            self.owner_kdf_iterations = PBKDF2_DEFAULT_ITERATIONS
            self.save_state()

    def verify_owner_secret(self, owner_secret: Optional[str]) -> bool:
        with self._mem_lock:
            self._sync_state_from_disk_if_changed()
            if not owner_secret or not self.owner_salt_hex or not self.owner_hash_hex:
                return False
            ok = _verify_pbkdf2(
                owner_secret,
                self.owner_salt_hex,
                self.owner_hash_hex,
                iterations=self.owner_kdf_iterations,
            )
            if ok and self.owner_kdf_iterations < PBKDF2_DEFAULT_ITERATIONS:
                # Seamlessly migrate verified legacy hashes (e.g., 120k) to 600k iterations with fresh salt.
                with self._state_file_lock():
                    self._sync_state_from_disk_if_changed()
                    new_salt, new_hash = _pbkdf2_hash(owner_secret, iterations=PBKDF2_DEFAULT_ITERATIONS)
                    self.owner_salt_hex = new_salt
                    self.owner_hash_hex = new_hash
                    self.owner_kdf_iterations = PBKDF2_DEFAULT_ITERATIONS
                    self.save_state()
            return ok

    def _compute_state_hmac(self, core_payload: Dict[str, Any]) -> Optional[str]:
        if not self._state_hmac_secret:
            return None
        canonical = json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._state_hmac_secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

    def load_state(self) -> None:
        with self._mem_lock:
            if not self.state_path.exists():
                self._last_state_sig = None
                return
            if self.require_state_hmac or self._state_hmac_secret:
                mode = stat.S_IMODE(self.state_path.stat().st_mode)
                if (mode & 0o077) != 0:
                    raise PermissionError(
                        f"Refusing to load '{self.state_path.name}' with permissive mode {oct(mode)}; "
                        "must restrict permissions to 0600 (owner read/write only)."
                    )
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("Corrupted remote auth state store: top-level JSON must be an object.")

            core = {
                "version": data.get("version", 2),
                "owner": data.get("owner", {}),
                "devices": data.get("devices", {}),
                "sessions": data.get("sessions", {}),
            }
            has_state_mac = bool(str(data.get("state_hmac", "")).strip())
            if self.require_state_hmac or self._state_hmac_secret:
                if not self._state_hmac_secret:
                    raise PermissionError("TRADELENS_STATE_HMAC_SECRET missing; cannot verify persisted remote state.")
                expected_mac = self._compute_state_hmac(core) or ""
                actual_mac = str(data.get("state_hmac", ""))
                if not actual_mac or not hmac.compare_digest(actual_mac, expected_mac):
                    raise PermissionError("Remote auth state HMAC integrity verification failed (missing or invalid state_hmac).")
            elif has_state_mac:
                raise PermissionError(
                    "Persisted remote auth state is HMAC-signed; provide TRADELENS_STATE_HMAC_SECRET to load it."
                )

            owner = core.get("owner", {})
            if not isinstance(owner, dict):
                raise ValueError("Malformed owner metadata in remote auth state store.")
            self.owner_salt_hex = owner.get("salt_hex")
            self.owner_hash_hex = owner.get("hash_hex")
            raw_iters = owner.get("iterations")
            if raw_iters is not None:
                self.owner_kdf_iterations = int(raw_iters)
            elif self.owner_hash_hex:
                self.owner_kdf_iterations = PBKDF2_LEGACY_ITERATIONS
            else:
                self.owner_kdf_iterations = PBKDF2_DEFAULT_ITERATIONS

            loaded_devices: Dict[str, TrustedDeviceRecord] = {}
            for dev_id, rec in (core.get("devices") or {}).items():
                if not isinstance(rec, dict):
                    raise ValueError(f"Malformed device record for {dev_id}")
                if "PRIVATE KEY" in str(rec.get("public_key_pem", "")).upper():
                    raise PermissionError(f"Private key material found inside server registry for {dev_id}")
                status = rec.get("status", DEVICE_STATUS_REVOKED)
                if status not in VALID_DEVICE_STATUSES:
                    status = DEVICE_STATUS_REVOKED
                rec_copy = dict(rec)
                rec_copy["status"] = status
                rec_copy["allowed_ips"] = normalize_allowed_peer_ips(rec_copy.get("allowed_ips") or [])
                loaded_devices[dev_id] = TrustedDeviceRecord(**rec_copy)
            self.devices = loaded_devices

            loaded_sessions: Dict[str, RemoteSessionRecord] = {}
            for sess_id, rec in (core.get("sessions") or {}).items():
                if not isinstance(rec, dict):
                    raise ValueError(f"Malformed session record for {sess_id}")
                loaded_sessions[sess_id] = RemoteSessionRecord(**rec)
            self.sessions = loaded_sessions
            self._last_state_sig = self._get_state_sig()
            self._state_ever_existed = True

    def save_state(self) -> None:
        with self._mem_lock:
            if self.require_state_hmac and not self._state_hmac_secret:
                raise PermissionError("Refusing to persist remote auth state without TRADELENS_STATE_HMAC_SECRET.")
            owner_meta: Dict[str, Any] = {
                "salt_hex": self.owner_salt_hex,
                "hash_hex": self.owner_hash_hex,
            }
            if self.owner_hash_hex:
                owner_meta["kdf"] = "pbkdf2_hmac_sha256"
                owner_meta["iterations"] = int(self.owner_kdf_iterations)
            core_payload = {
                "version": 2,
                "owner": owner_meta,
                "devices": {dev_id: asdict(rec) for dev_id, rec in self.devices.items()},
                "sessions": {sess_id: asdict(rec) for sess_id, rec in self.sessions.items()},
            }
            payload: Dict[str, Any] = dict(core_payload)
            mac = self._compute_state_hmac(core_payload)
            if mac is not None:
                payload["state_hmac"] = mac
            _atomic_write_json(self.state_path, payload)
            self._last_state_sig = self._get_state_sig()
            self._state_ever_existed = True

    def _revoke_device_sessions_and_challenges(self, clean_id: str, reason: str, now_iso: str) -> int:
        revoked_sessions = 0
        for sess in self.sessions.values():
            if sess.device_id == clean_id and not sess.revoked:
                sess.revoked = True
                sess.revoked_at = now_iso
                sess.revoked_reason = reason
                revoked_sessions += 1
        stale_challenges = [cid for cid, ch in self._challenges.items() if ch.device_id == clean_id]
        for cid in stale_challenges:
            self._challenges.pop(cid, None)
        return revoked_sessions

    def request_device_registration(
        self,
        device_id: str,
        device_name: str,
        public_key_pem: str,
        role: str = "remote_workstation",
        allowed_ips: Optional[Sequence[str]] = None,
    ) -> TrustedDeviceRecord:
        """
        Submit or re-submit a device public key for Home Mac owner approval.
        Always enters PENDING_APPROVAL and clears prior approvals/sessions so
        re-enrolment of a revoked device requires fresh explicit owner approval.
        """
        clean_id = (device_id or "").strip()
        if not _SAFE_DEVICE_ID_RE.fullmatch(clean_id):
            raise ValueError("device_id must be 3-64 safe characters [A-Za-z0-9._-].")
        canonical_pem, fingerprint = DeviceKeyCrypto.normalize_public_key_pem(public_key_pem)
        norm_allowed_ips = normalize_allowed_peer_ips(allowed_ips)

        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            now_iso = self._now().isoformat()
            existing = self.devices.get(clean_id)
            re_enroll_count = existing.re_enrollment_count + 1 if existing else 0
            if existing:
                self._revoke_device_sessions_and_challenges(
                    clean_id,
                    reason="RE_ENROLLMENT_PENDING_OWNER_APPROVAL",
                    now_iso=now_iso,
                )

            record = TrustedDeviceRecord(
                device_id=clean_id,
                device_name=(device_name or clean_id).strip(),
                role=role,
                public_key_pem=canonical_pem,
                public_key_fingerprint=fingerprint,
                status=DEVICE_STATUS_PENDING,
                registered_at=now_iso,
                approved_at=None,
                last_auth_at=None,
                last_seen_at=None,
                revoked_at=existing.revoked_at if existing else None,
                revocation_reason=existing.revocation_reason if existing else None,
                re_enrollment_count=re_enroll_count,
                allowed_ips=norm_allowed_ips,
            )
            self.devices[clean_id] = record
            self.save_state()
            return record

    def approve_device(
        self,
        device_id: str,
        owner_secret: str,
        activate: bool = True,
        allowed_ips: Optional[Sequence[str]] = None,
    ) -> TrustedDeviceRecord:
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while approving device.")
        clean_id = (device_id or "").strip()
        norm_allowed_ips = normalize_allowed_peer_ips(allowed_ips) if allowed_ips is not None else None
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            dev = self.devices.get(clean_id)
            if not dev:
                raise KeyError(f"Unknown device_id: {clean_id}")
            if dev.status == DEVICE_STATUS_REVOKED:
                raise PermissionError(
                    f"Device '{clean_id}' is REVOKED; submit a fresh enrollment request with public key before owner approval."
                )
            now_iso = self._now().isoformat()
            dev.approved_at = now_iso
            dev.status = DEVICE_STATUS_ACTIVE if activate else DEVICE_STATUS_APPROVED
            if norm_allowed_ips is not None:
                if norm_allowed_ips != dev.allowed_ips:
                    self._revoke_device_sessions_and_challenges(
                        clean_id,
                        reason="DEVICE_ALLOWED_IP_UPDATED",
                        now_iso=now_iso,
                    )
                    dev.last_auth_at = None
                dev.allowed_ips = norm_allowed_ips
            self.save_state()
            return dev

    def update_device_allowed_ips(
        self,
        device_id: str,
        allowed_ips: Sequence[str],
        owner_secret: str,
        reason: str = "DEVICE_ALLOWED_IP_UPDATED",
    ) -> Dict[str, Any]:
        """
        Owner-authenticated exact peer IP update for a non-revoked registered device.
        Rejects broad CIDRs or empty lists, invalidates all existing sessions/challenges
        for the device so re-authentication from the newly verified IP is mandatory,
        and never auto-accepts an unapproved IP by matching device_id.
        """
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while updating device allowed IPs.")
        clean_id = (device_id or "").strip()
        norm_ips = normalize_allowed_peer_ips(allowed_ips)
        if not norm_ips:
            raise ValueError("At least one exact remote peer IP address is required when updating device IP pinning.")
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            dev = self.devices.get(clean_id)
            if not dev:
                raise KeyError(f"Unknown device_id: {clean_id}")
            if dev.status == DEVICE_STATUS_REVOKED:
                raise PermissionError(
                    f"Device '{clean_id}' is REVOKED; cannot update allowed IPs without explicit fresh registration and owner approval."
                )
            now_iso = self._now().isoformat()
            revoked_count = self._revoke_device_sessions_and_challenges(
                clean_id,
                reason=reason,
                now_iso=now_iso,
            )
            dev.allowed_ips = norm_ips
            dev.last_auth_at = None
            self.save_state()
            return {
                "status": "ALLOWED_IPS_UPDATED",
                "device_id": dev.device_id,
                "device_status": dev.status,
                "allowed_ips": list(dev.allowed_ips),
                "revoked_sessions_count": revoked_count,
            }

    def activate_device(self, device_id: str, owner_secret: str) -> TrustedDeviceRecord:
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while activating device.")
        clean_id = (device_id or "").strip()
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            dev = self.devices.get(clean_id)
            if not dev:
                raise KeyError(f"Unknown device_id: {clean_id}")
            if dev.status != DEVICE_STATUS_APPROVED or not dev.approved_at:
                raise PermissionError(f"Device '{clean_id}' must be in APPROVED state before activation (got {dev.status}).")
            dev.status = DEVICE_STATUS_ACTIVE
            self.save_state()
            return dev

    def register_device(
        self,
        device_id: str,
        device_name: str,
        public_key_pem: str,
        owner_secret: str,
        role: str = "remote_workstation",
        approve: bool = True,
        allow_re_enroll: bool = False,
        allowed_ips: Optional[Sequence[str]] = None,
    ) -> TrustedDeviceRecord:
        """
        Home Mac owner provisioning entry point using the remote workstation's PUBLIC key ONLY.
        Private keys are never accepted or persisted by Home Mac.
        """
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while registering device.")
        clean_id = (device_id or "").strip()
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            existing = self.devices.get(clean_id)
            if existing and existing.status == DEVICE_STATUS_REVOKED and not allow_re_enroll:
                raise PermissionError(
                    f"Device '{clean_id}' was REVOKED; re-enrollment requires explicit request_device_registration "
                    "and fresh owner approval."
                )
            rec = self.request_device_registration(
                device_id=clean_id,
                device_name=device_name,
                public_key_pem=public_key_pem,
                role=role,
                allowed_ips=allowed_ips,
            )
            if approve:
                return self.approve_device(clean_id, owner_secret=owner_secret, activate=True)
            return rec

    def revoke_device(
        self,
        device_id: str,
        owner_secret: str,
        reason: str = "REVOKED_BY_OWNER",
    ) -> int:
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while revoking device.")
        clean_id = (device_id or "").strip()
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            dev = self.devices.get(clean_id)
            if not dev:
                raise KeyError(f"Unknown device_id: {clean_id}")
            now_iso = self._now().isoformat()
            dev.status = DEVICE_STATUS_REVOKED
            dev.revoked_at = now_iso
            dev.revocation_reason = reason

            revoked_sessions = self._revoke_device_sessions_and_challenges(
                clean_id,
                reason=reason,
                now_iso=now_iso,
            )
            self.save_state()
            return revoked_sessions

    def list_devices(self, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return safe non-secret device metadata from the persisted Home Mac registry."""
        self._sync_state_from_disk_if_changed()
        filt = status_filter.strip().upper() if isinstance(status_filter, str) and status_filter.strip() else None
        now = self._now()
        out: List[Dict[str, Any]] = []
        for dev_id in sorted(self.devices.keys()):
            dev = self.devices[dev_id]
            if filt and dev.status != filt:
                continue
            active_sessions = 0
            for sess in self.sessions.values():
                if sess.device_id != dev.device_id or sess.revoked:
                    continue
                if now < datetime.fromisoformat(sess.expires_at):
                    active_sessions += 1
            out.append(
                {
                    "device_id": dev.device_id,
                    "device_name": dev.device_name,
                    "role": dev.role,
                    "status": dev.status,
                    "public_key_fingerprint": dev.public_key_fingerprint,
                    "allowed_ips": list(dev.allowed_ips),
                    "registered_at": dev.registered_at,
                    "approved_at": dev.approved_at,
                    "last_auth_at": dev.last_auth_at,
                    "last_seen_at": dev.last_seen_at,
                    "revoked_at": dev.revoked_at,
                    "revocation_reason": dev.revocation_reason,
                    "re_enrollment_count": dev.re_enrollment_count,
                    "active_session_count": active_sessions,
                }
            )
        return out

    def remove_device(
        self,
        device_id: str,
        owner_secret: str,
        require_revoked: bool = True,
        reason: str = "REMOVED_BY_OWNER",
    ) -> Dict[str, Any]:
        """
        Permanently purge a registered device public key and its historical sessions from Home Mac.
        By default (`require_revoked=True`), a device must be REVOKED first so accidental deletion
        of an active device cannot occur without explicit revocation semantics.
        """
        if not self.verify_owner_secret(owner_secret):
            raise PermissionError("Owner authentication failed while removing device.")
        clean_id = (device_id or "").strip()
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            dev = self.devices.get(clean_id)
            if not dev:
                raise KeyError(f"Unknown device_id: {clean_id}")
            prev_status = dev.status
            if require_revoked and prev_status != DEVICE_STATUS_REVOKED:
                raise PermissionError(
                    f"Device '{clean_id}' must be REVOKED before permanent removal (status={prev_status}). "
                    "Revoke first or pass require_revoked=False."
                )

            now_iso = self._now().isoformat()
            revoked_active_count = self._revoke_device_sessions_and_challenges(
                clean_id,
                reason=reason,
                now_iso=now_iso,
            )
            purged_session_ids = [sid for sid, sess in self.sessions.items() if sess.device_id == clean_id]
            for sid in purged_session_ids:
                self.sessions.pop(sid, None)
            del self.devices[clean_id]
            self.save_state()
            return {
                "removed": True,
                "device_id": clean_id,
                "previous_status": prev_status,
                "revoked_sessions_count": revoked_active_count,
                "purged_sessions_count": len(purged_session_ids),
                "note": "Public key purged from Home Mac; run delete-device-key on remote Mac to clear Keychain private key.",
            }

    def _prune_challenges(self, now: datetime) -> None:
        stale = []
        for cid, ch in self._challenges.items():
            exp = datetime.fromisoformat(ch.expires_at)
            if ch.used or now >= exp:
                if (now - exp).total_seconds() > 300 or ch.used:
                    stale.append(cid)
        for cid in stale:
            self._challenges.pop(cid, None)

    def issue_auth_challenge(
        self,
        device_id: str,
        client_ip: str,
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], AuthorizationDecision]:
        """Issue a cryptographically random, single-use, expiring login challenge for an ACTIVE device."""
        self._sync_state_from_disk_if_changed()
        net_ok, net_reason = is_allowed_remote_client_ip(
            client_ip,
            self.allowed_vpn_cidrs,
            allowed_peer_ips=self.allowed_peer_ips,
        )
        if not net_ok:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code=net_reason,
                message="Connection rejected: client IP is outside approved private network / peer allowlist.",
                status_code=403,
                client_ip=client_ip,
            )

        now = self._now()
        clean_id = (device_id or "").strip()
        allowed_rate, retry_after = self.rate_limiter.check_allowed(clean_id or "_unknown_", client_ip, now)
        if not allowed_rate:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="AUTH_RATE_LIMITED",
                message=f"Too many failed authentication attempts. Retry after {retry_after}s.",
                status_code=429,
                device_id=clean_id or None,
                client_ip=client_ip,
            )

        dev = self.devices.get(clean_id)
        if not dev:
            self.rate_limiter.record_failure(clean_id or "_unknown_", client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="UNREGISTERED_DEVICE",
                message="Device is not registered on Home Mac.",
                status_code=403,
                client_ip=client_ip,
            )

        if dev.status == DEVICE_STATUS_REVOKED:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_REVOKED",
                message=f"Device '{clean_id}' has been revoked ({dev.revocation_reason or 'owner action'}).",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )
        if dev.status == DEVICE_STATUS_PENDING:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_PENDING_APPROVAL",
                message=f"Device '{clean_id}' awaits explicit Home Mac owner approval.",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )
        if dev.status != DEVICE_STATUS_ACTIVE:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_NOT_ACTIVE",
                message=f"Device '{clean_id}' is not active (status={dev.status}).",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )
        if dev.allowed_ips and not _ip_matches_peer_allowlist(_parse_ip_clean(client_ip), dev.allowed_ips):
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_IP_NOT_ALLOWED",
                message=f"Device '{clean_id}' is pinned to another approved peer IP.",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )

        self._prune_challenges(now)
        per_dev = [ch for ch in self._challenges.values() if ch.device_id == clean_id and not ch.used]
        if len(per_dev) >= self.max_active_challenges_per_device:
            per_dev.sort(key=lambda c: c.issued_at)
            for old in per_dev[: len(per_dev) - self.max_active_challenges_per_device + 1]:
                self._challenges.pop(old.challenge_id, None)

        effective_ttl = max(10, min(int(ttl_seconds or self.challenge_ttl_seconds), 300))
        challenge_id = f"chg_{secrets.token_hex(12)}"
        nonce = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=effective_ttl)
        challenge_string = (
            f"tradelens-auth-v1|{challenge_id}|{clean_id}|{dev.public_key_fingerprint}|"
            f"{nonce}|{client_ip}|{expires_at.isoformat()}"
        )
        record = AuthChallengeRecord(
            challenge_id=challenge_id,
            device_id=clean_id,
            public_key_fingerprint=dev.public_key_fingerprint,
            nonce=nonce,
            client_ip=client_ip,
            challenge_string=challenge_string,
            issued_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            used=False,
        )
        self._challenges[challenge_id] = record
        return (
            {
                "challenge_id": challenge_id,
                "device_id": clean_id,
                "public_key_fingerprint": dev.public_key_fingerprint,
                "nonce": nonce,
                "challenge_string": challenge_string,
                "issued_at": record.issued_at,
                "expires_at": record.expires_at,
                "algorithm": "ECDSA_P256_SHA256",
            },
            AuthorizationDecision(
                allowed=True,
                reason_code="CHALLENGE_ISSUED",
                message="Single-use expiring device challenge issued.",
                status_code=200,
                device_id=clean_id,
                client_ip=client_ip,
            ),
        )

    def authenticate_remote_session(
        self,
        device_id: str,
        challenge_id: str,
        signature_b64: str,
        client_ip: str,
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], AuthorizationDecision]:
        """
        Verify cryptographic device challenge-response ECDSA P-256 signature for an
        owner-approved ACTIVE device over private network transport and issue a
        short-lived session token bound to device_id, key fingerprint, and client IP.
        Notice: TRADELENS_OWNER_SECRET stays exclusively on Home Mac and is NEVER
        required for routine remote workstation logins.
        """
        self._sync_state_from_disk_if_changed()
        net_ok, net_reason = is_allowed_remote_client_ip(
            client_ip,
            self.allowed_vpn_cidrs,
            allowed_peer_ips=self.allowed_peer_ips,
        )
        if not net_ok:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code=net_reason,
                message="Connection rejected: client IP is outside approved private network / peer allowlist.",
                status_code=403,
                client_ip=client_ip,
            )

        now = self._now()
        clean_id = (device_id or "").strip()
        allowed_rate, retry_after = self.rate_limiter.check_allowed(clean_id or "_unknown_", client_ip, now)
        if not allowed_rate:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="AUTH_RATE_LIMITED",
                message=f"Authentication temporarily locked out after repeated failures. Retry after {retry_after}s.",
                status_code=429,
                device_id=clean_id or None,
                client_ip=client_ip,
            )

        dev = self.devices.get(clean_id)
        if not dev:
            self.rate_limiter.record_failure(clean_id or "_unknown_", client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="UNREGISTERED_DEVICE",
                message="Device is not registered as a trusted workstation on Home Mac.",
                status_code=403,
                client_ip=client_ip,
            )

        if dev.status == DEVICE_STATUS_REVOKED:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_REVOKED",
                message=f"Device '{clean_id}' has been revoked ({dev.revocation_reason or 'owner action'}).",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )

        if dev.status != DEVICE_STATUS_ACTIVE:
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_NOT_ACTIVE",
                message=f"Device '{clean_id}' is not approved/active (status={dev.status}).",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )

        if dev.allowed_ips and not _ip_matches_peer_allowlist(_parse_ip_clean(client_ip), dev.allowed_ips):
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="DEVICE_IP_NOT_ALLOWED",
                message=f"Device '{clean_id}' is pinned to another approved peer IP.",
                status_code=403,
                device_id=clean_id,
                client_ip=client_ip,
            )

        clean_cid = (challenge_id or "").strip()
        ch = self._challenges.get(clean_cid)
        if not ch:
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="INVALID_CHALLENGE",
                message="Challenge ID missing or unrecognized; request a fresh challenge first.",
                status_code=401,
                device_id=clean_id,
                client_ip=client_ip,
            )

        if ch.used:
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="CHALLENGE_REPLAYED",
                message="Single-use challenge has already been consumed.",
                status_code=401,
                device_id=clean_id,
                client_ip=client_ip,
            )

        # Consume challenge immediately before further validation so failed/spoofed attempts cannot retry it.
        ch.used = True
        ch.used_at = now.isoformat()

        if (
            ch.device_id != clean_id
            or ch.client_ip != client_ip
            or ch.public_key_fingerprint != dev.public_key_fingerprint
        ):
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="CHALLENGE_BINDING_MISMATCH",
                message="Challenge does not match device identity, key fingerprint, or client IP.",
                status_code=401,
                device_id=clean_id,
                client_ip=client_ip,
            )

        exp_ch = datetime.fromisoformat(ch.expires_at)
        if now >= exp_ch:
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="CHALLENGE_EXPIRED",
                message="Challenge expired; request a fresh challenge.",
                status_code=401,
                device_id=clean_id,
                client_ip=client_ip,
            )

        if not DeviceKeyCrypto.verify_signature(dev.public_key_pem, ch.challenge_string, signature_b64):
            self.rate_limiter.record_failure(clean_id, client_ip, now)
            return None, AuthorizationDecision(
                allowed=False,
                reason_code="INVALID_DEVICE_SIGNATURE",
                message="Cryptographic device signature verification failed.",
                status_code=401,
                device_id=clean_id,
                client_ip=client_ip,
            )

        self.rate_limiter.reset_success(clean_id, client_ip)
        effective_ttl = self.session_ttl_seconds
        if ttl_seconds is not None:
            effective_ttl = max(60, min(int(ttl_seconds), self.max_session_ttl_seconds))

        expires = now + timedelta(seconds=effective_ttl)
        raw_token = f"tlr_{secrets.token_urlsafe(32)}"
        session_id = f"sess_{secrets.token_hex(8)}"

        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            latest_dev = self.devices.get(clean_id)
            if not latest_dev or latest_dev.status != DEVICE_STATUS_ACTIVE or latest_dev.public_key_fingerprint != dev.public_key_fingerprint:
                return None, AuthorizationDecision(
                    allowed=False,
                    reason_code="DEVICE_REVOKED" if (latest_dev and latest_dev.status == DEVICE_STATUS_REVOKED) else "DEVICE_NOT_ACTIVE",
                    message=f"Device '{clean_id}' was modified/revoked before session commit.",
                    status_code=403,
                    device_id=clean_id,
                    client_ip=client_ip,
                )
            self._ephemeral_tokens.add(raw_token)
            sess = RemoteSessionRecord(
                session_id=session_id,
                token_hash_hex=_sha256_hex(raw_token),
                device_id=clean_id,
                public_key_fingerprint=latest_dev.public_key_fingerprint,
                challenge_id=ch.challenge_id,
                issued_at=now.isoformat(),
                expires_at=expires.isoformat(),
                last_seen_at=now.isoformat(),
                client_ip=client_ip,
                revoked=False,
            )
            latest_dev.last_auth_at = now.isoformat()
            latest_dev.last_seen_at = now.isoformat()
            self.sessions[session_id] = sess
            self._prune_expired_sessions(now)
            self.save_state()

        # Deliberately omit any broker credentials, private keys, or owner secrets.
        return (
            {
                "session_id": session_id,
                "access_token": raw_token,
                "token_type": "Bearer",
                "device_id": clean_id,
                "device_name": dev.device_name,
                "public_key_fingerprint": dev.public_key_fingerprint,
                "issued_at": sess.issued_at,
                "expires_at": sess.expires_at,
                "ttl_seconds": effective_ttl,
                "storage_policy": "MEMORY_ONLY_DO_NOT_PERSIST_IN_BROWSER_STORAGE",
                "transport_tls_enabled": self.tls_enabled,
                "note": "Remote session proves workstation auth only; live data depends on Home Mac broker & feed checks.",
            },
            AuthorizationDecision(
                allowed=True,
                reason_code="SESSION_ISSUED",
                message="Short-lived remote session established.",
                status_code=200,
                device_id=clean_id,
                session_id=session_id,
                client_ip=client_ip,
                is_owner_verified=False,
            ),
        )

    def _prune_expired_sessions(self, now: datetime) -> None:
        expired_ids = []
        for sess_id, sess in self.sessions.items():
            exp = datetime.fromisoformat(sess.expires_at)
            # Keep explicitly revoked/logged-out records longer so restart checks remain strict.
            if not sess.revoked and (now - exp).total_seconds() > 86400:
                expired_ids.append(sess_id)
        for sess_id in expired_ids:
            self.sessions.pop(sess_id, None)

    def revoke_session_by_token(self, token: str, reason: str = "LOGOUT") -> bool:
        if not token:
            return False
        token_hash = _sha256_hex(token)
        with self._state_file_lock():
            self._sync_state_from_disk_if_changed()
            now_iso = self._now().isoformat()
            for sess in self.sessions.values():
                if hmac.compare_digest(sess.token_hash_hex, token_hash):
                    sess.revoked = True
                    sess.revoked_at = now_iso
                    sess.revoked_reason = reason
                    self.save_state()
                    return True
            return False

    def _lookup_valid_session(
        self,
        token: str,
        client_ip: str = "",
    ) -> Tuple[Optional[RemoteSessionRecord], str, str]:
        self._sync_state_from_disk_if_changed()
        if not token:
            return None, "AUTH_REQUIRED", "Authentication token required."
        token_hash = _sha256_hex(token)
        matched: Optional[RemoteSessionRecord] = None
        for sess in self.sessions.values():
            if hmac.compare_digest(sess.token_hash_hex, token_hash):
                matched = sess
                break
        if matched is None:
            return None, "INVALID_SESSION_TOKEN", "Session token is unrecognized or invalidated."

        dev = self.devices.get(matched.device_id)
        if not dev or dev.status == DEVICE_STATUS_REVOKED:
            with self._state_file_lock():
                matched.revoked = True
                matched.revoked_reason = "DEVICE_REVOKED"
                self.save_state()
            return None, "DEVICE_REVOKED", "Trusted workstation device has been revoked."

        if dev.status != DEVICE_STATUS_ACTIVE:
            return None, "DEVICE_NOT_ACTIVE", f"Trusted workstation is not active ({dev.status})."

        if dev.allowed_ips and client_ip and not _ip_matches_peer_allowlist(_parse_ip_clean(client_ip), dev.allowed_ips):
            return None, "DEVICE_IP_NOT_ALLOWED", "Trusted workstation device is restricted to another approved peer IP."

        if matched.public_key_fingerprint != dev.public_key_fingerprint:
            with self._state_file_lock():
                matched.revoked = True
                matched.revoked_reason = "KEY_FINGERPRINT_CHANGED"
                self.save_state()
            return None, "DEVICE_KEY_CHANGED", "Device key rotated; re-authentication required."

        if matched.revoked:
            return (
                None,
                "SESSION_REVOKED",
                f"Remote session revoked ({matched.revoked_reason or 'logout/revocation'}).",
            )

        if client_ip and matched.client_ip != client_ip:
            return (
                None,
                "SESSION_IP_MISMATCH",
                "Session token is bound to another network address and cannot be replayed.",
            )

        now = self._now()
        exp = datetime.fromisoformat(matched.expires_at)
        if now >= exp:
            return None, "SESSION_EXPIRED", "Short-lived remote session expired; re-authentication required."

        matched.last_seen_at = now.isoformat()
        dev.last_seen_at = now.isoformat()
        return matched, "AUTHORIZED", "Authorized short-lived remote session."

    def is_endpoint_allowed_for_remote(self, path: str, method: str) -> Tuple[bool, str]:
        if method not in ALLOWED_REMOTE_METHODS:
            return False, "METHOD_NOT_ALLOWED"
        if path in HOME_OWNER_ONLY_ENDPOINTS:
            return False, "HOME_OWNER_ONLY_OPERATION"
        if is_forbidden_order_execution_path(path):
            in_allowlist = (path in ALLOWED_REMOTE_ROUTE_METHODS) or any(
                path.startswith(prefix) and len(path) > len(prefix) for prefix in ALLOWED_REMOTE_PREFIXES
            )
            if in_allowlist:
                return False, "ORDER_EXECUTION_DISABLED"
            return False, "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"
        allowed_methods = ALLOWED_REMOTE_ROUTE_METHODS.get(path)
        if allowed_methods is not None:
            if method in allowed_methods:
                return True, "ALLOWED_ENDPOINT"
            return False, "METHOD_NOT_ALLOWED"
        if method == "GET" and any(
            path.startswith(prefix) and len(path) > len(prefix) for prefix in ALLOWED_REMOTE_PREFIXES
        ):
            return True, "ALLOWED_ENDPOINT"
        return False, "ENDPOINT_NOT_AUTHORIZED_FOR_REMOTE"

    def authorize_request(
        self,
        path: str,
        method: str,
        client_ip: str,
        headers: Optional[Dict[str, str]] = None,
        owner_secret_header: Optional[str] = None,
        query: str = "",
        require_host_header: bool = False,
    ) -> AuthorizationDecision:
        self._sync_state_from_disk_if_changed()
        raw_path = path
        if "?" in path and not query:
            parsed_url = urlparse(path)
            raw_path = parsed_url.path
            query = parsed_url.query

        has_url_secret, bad_param = has_forbidden_url_secret(query)
        if has_url_secret:
            return AuthorizationDecision(
                allowed=False,
                reason_code="URL_TOKEN_FORBIDDEN",
                message=f"Credential/token query parameter '{bad_param}' is forbidden; use Authorization header.",
                status_code=400,
                client_ip=client_ip,
            )

        decoded_path = unquote(raw_path)
        if (
            "\x00" in raw_path
            or "\x00" in decoded_path
            or ".." in raw_path
            or ".." in decoded_path
            or "\\" in raw_path
            or "\\" in decoded_path
            or "//" in raw_path
        ):
            return AuthorizationDecision(
                allowed=False,
                reason_code="INVALID_PATH",
                message="Malformed or traversal request path rejected.",
                status_code=400,
                client_ip=client_ip,
            )

        net_ok, net_reason = is_allowed_remote_client_ip(
            client_ip,
            self.allowed_vpn_cidrs,
            allowed_peer_ips=self.allowed_peer_ips,
        )
        if not net_ok:
            return AuthorizationDecision(
                allowed=False,
                reason_code=net_reason,
                message="Rejected connection from unapproved network address.",
                status_code=403,
                client_ip=client_ip,
            )

        norm_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

        if "host" in norm_headers or require_host_header:
            host_ok, host_reason = is_allowed_host_header(
                norm_headers.get("host"),
                allowed_hosts=self.allowed_hosts,
                allowed_vpn_cidrs=self.allowed_vpn_cidrs,
            )
            if not host_ok:
                return AuthorizationDecision(
                    allowed=False,
                    reason_code=host_reason,
                    message="Rejected request with unapproved HTTP Host header.",
                    status_code=403,
                    client_ip=client_ip,
                )

        origin_ok, allowed_origin, origin_reason = is_allowed_origin(
            norm_headers.get("origin"),
            allowed_origins=self.allowed_origins,
            allowed_hosts=self.allowed_hosts,
            allowed_vpn_cidrs=self.allowed_vpn_cidrs,
        )
        if not origin_ok:
            return AuthorizationDecision(
                allowed=False,
                reason_code=origin_reason,
                message="Rejected request from unapproved browser Origin.",
                status_code=403,
                client_ip=client_ip,
            )

        is_proxied = any(h in norm_headers and norm_headers[h].strip() for h in PROXY_HEADER_NAMES)
        peer_ip_obj = _parse_ip_clean(client_ip)
        effective_ip = client_ip
        if is_proxied:
            if not peer_ip_obj.is_loopback or not self.trust_loopback_proxy:
                return AuthorizationDecision(
                    allowed=False,
                    reason_code="FORGED_PROXY_HEADERS",
                    message="Proxy headers from non-trusted peer rejected to prevent IP spoofing.",
                    status_code=403,
                    client_ip=client_ip,
                )
            fwd = norm_headers.get("x-forwarded-for") or norm_headers.get("x-real-ip") or ""
            if fwd:
                effective_ip = fwd.split(",")[0].strip()
                net_ok_fwd, net_reason_fwd = is_allowed_remote_client_ip(
                    effective_ip,
                    self.allowed_vpn_cidrs,
                    allowed_peer_ips=self.allowed_peer_ips,
                )
                if not net_ok_fwd:
                    return AuthorizationDecision(
                        allowed=False,
                        reason_code="FORGED_PROXY_HEADERS" if net_reason_fwd == "INVALID_CLIENT_IP" else net_reason_fwd,
                        message="Rejected proxied connection from unapproved network address.",
                        status_code=403,
                        client_ip=effective_ip,
                    )
                if _parse_ip_clean(effective_ip).is_loopback:
                    return AuthorizationDecision(
                        allowed=False,
                        reason_code="FORGED_PROXY_HEADERS",
                        message="Proxied request claiming loopback source IP rejected.",
                        status_code=403,
                        client_ip=client_ip,
                    )

        ip_obj = _parse_ip_clean(effective_ip)
        is_loopback = ip_obj.is_loopback and not is_proxied

        if method == "OPTIONS":
            return AuthorizationDecision(
                allowed=True,
                reason_code="CORS_PREFLIGHT",
                message="Preflight",
                status_code=200,
                client_ip=effective_ip,
                is_loopback=is_loopback,
                allowed_origin=allowed_origin,
            )

        # Public HTML shell (zero secrets or live broker data)
        if raw_path in {"/", "/index.html"} and method == "GET":
            return AuthorizationDecision(
                allowed=True,
                reason_code="STATIC_UI_SHELL",
                message="Static UI shell served.",
                status_code=200,
                client_ip=effective_ip,
                is_loopback=is_loopback,
                allowed_origin=allowed_origin,
            )

        # Cryptographic challenge & auth handshake endpoints
        if raw_path in {"/api/remote/challenge", "/api/remote/auth"} and method == "POST":
            return AuthorizationDecision(
                allowed=True,
                reason_code="AUTH_HANDSHAKE",
                message="Remote auth handshake route.",
                status_code=200,
                client_ip=effective_ip,
                is_loopback=is_loopback,
                allowed_origin=allowed_origin,
            )

        owner_hdr = owner_secret_header or norm_headers.get("x-tradelens-owner-secret")

        # Home-Mac owner-only routes strictly forbid non-loopback execution AND require owner verification.
        if raw_path in HOME_OWNER_ONLY_ENDPOINTS:
            if not is_loopback:
                return AuthorizationDecision(
                    allowed=False,
                    reason_code="HOME_OWNER_ONLY_OPERATION",
                    message="Operation restricted to Home Mac owner session.",
                    status_code=403,
                    client_ip=effective_ip,
                    is_loopback=False,
                    allowed_origin=allowed_origin,
                )
            if self.owner_hash_hex or self.remote_mode_enabled:
                if not self.verify_owner_secret(owner_hdr):
                    return AuthorizationDecision(
                        allowed=False,
                        reason_code="OWNER_AUTH_REQUIRED",
                        message="Valid Home Mac owner authentication required.",
                        status_code=401,
                        client_ip=effective_ip,
                        is_loopback=True,
                        allowed_origin=allowed_origin,
                    )
            return AuthorizationDecision(
                allowed=True,
                reason_code="HOME_OWNER_AUTHORIZED",
                message="Home Mac owner authorized.",
                status_code=200,
                client_ip=effective_ip,
                is_loopback=True,
                is_owner_verified=True,
                allowed_origin=allowed_origin,
            )

        # Remote peer requests over Tailscale/WireGuard ALWAYS require endpoint allowlist AND active session.
        if not is_loopback or self.require_auth_on_loopback:
            ep_ok, ep_reason = self.is_endpoint_allowed_for_remote(raw_path, method)
            if not ep_ok:
                status = 405 if ep_reason == "METHOD_NOT_ALLOWED" else 403
                return AuthorizationDecision(
                    allowed=False,
                    reason_code=ep_reason,
                    message=f"Endpoint '{raw_path}' ({method}) is not authorized for remote sessions.",
                    status_code=status,
                    client_ip=effective_ip,
                    is_loopback=is_loopback,
                    allowed_origin=allowed_origin,
                )

            auth_header = norm_headers.get("authorization", "")
            token = ""
            if auth_header.lower().startswith("bearer "):
                token = auth_header[7:].strip()

            sess, code, msg = self._lookup_valid_session(token, client_ip=effective_ip)
            if not sess:
                status = 403 if code in {"DEVICE_REVOKED", "DEVICE_NOT_ACTIVE", "DEVICE_IP_NOT_ALLOWED", "SESSION_IP_MISMATCH"} else 401
                return AuthorizationDecision(
                    allowed=False,
                    reason_code=code,
                    message=msg,
                    status_code=status,
                    client_ip=effective_ip,
                    is_loopback=is_loopback,
                    allowed_origin=allowed_origin,
                )

            return AuthorizationDecision(
                allowed=True,
                reason_code="REMOTE_SESSION_AUTHORIZED",
                message=msg,
                status_code=200,
                device_id=sess.device_id,
                session_id=sess.session_id,
                client_ip=effective_ip,
                is_loopback=is_loopback,
                allowed_origin=allowed_origin,
            )

        return AuthorizationDecision(
            allowed=True,
            reason_code="LOOPBACK_LOCAL_AUTHORIZED",
            message="Local Home Mac loopback authorized.",
            status_code=200,
            client_ip=effective_ip,
            is_loopback=True,
            allowed_origin=allowed_origin,
        )

    def summarize_connectivity(self) -> Dict[str, Any]:
        """Return safe connectivity summary (no secrets, private keys, or raw token strings)."""
        self._sync_state_from_disk_if_changed()
        now = self._now()
        active_sessions = []
        for sess in self.sessions.values():
            if sess.revoked:
                continue
            dev = self.devices.get(sess.device_id)
            if not dev or dev.status != DEVICE_STATUS_ACTIVE:
                continue
            exp = datetime.fromisoformat(sess.expires_at)
            if now >= exp:
                continue
            active_sessions.append(
                {
                    "session_id": sess.session_id,
                    "device_id": sess.device_id,
                    "device_name": dev.device_name,
                    "public_key_fingerprint": dev.public_key_fingerprint,
                    "last_seen_at": sess.last_seen_at,
                    "expires_at": sess.expires_at,
                    "seconds_remaining": max(0, int((exp - now).total_seconds())),
                }
            )
        return {
            "active_remote_sessions": len(active_sessions),
            "trusted_devices_total": len(self.devices),
            "trusted_devices_pending": sum(1 for d in self.devices.values() if d.status == DEVICE_STATUS_PENDING),
            "trusted_devices_approved": sum(1 for d in self.devices.values() if d.status == DEVICE_STATUS_APPROVED),
            "trusted_devices_active": sum(1 for d in self.devices.values() if d.status == DEVICE_STATUS_ACTIVE),
            "trusted_devices_revoked": sum(1 for d in self.devices.values() if d.status == DEVICE_STATUS_REVOKED),
            "sessions": active_sessions,
            "allowed_peer_ips": list(self.allowed_peer_ips),
            "transport_tls_enabled": self.tls_enabled,
            "order_execution_enabled": False,
            "proves_live_market_data": False,
        }


def wrap_server_tls(
    httpd: Any,
    tls_certfile: Optional[str | Path] = None,
    tls_keyfile: Optional[str | Path] = None,
) -> bool:
    """
    Optionally wrap an HTTPServer socket in TLS 1.2+ using provided certificate/key paths.
    Returns True if TLS wrapping was configured, False otherwise.
    """
    if not tls_certfile and not tls_keyfile:
        return False
    if not tls_certfile or not tls_keyfile:
        raise ValueError("Both tls_certfile and tls_keyfile must be provided together to enable TLS.")
    cert_path = Path(tls_certfile)
    key_path = Path(tls_keyfile)
    if not cert_path.exists() or not key_path.exists():
        raise FileNotFoundError("Configured TLS certificate or key file does not exist.")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return True

