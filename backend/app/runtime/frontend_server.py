"""
TradeLens AI - Hardened Static Frontend Dashboard Server.

Serves strictly `frontend/index.html` over loopback or an approved private VPN IP,
enforcing Host/Origin validation, rejecting URL tokens/traversal, emitting strict CSP
nonces and defensive security headers, refusing public binds, and supporting optional TLS.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import json
import os
from pathlib import Path
from typing import Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from .security import (
    PROXY_HEADER_NAMES,
    _extract_hostname_without_port,
    _parse_ip_clean,
    build_security_headers,
    has_forbidden_url_secret,
    is_allowed_host_header,
    is_allowed_origin,
    is_allowed_remote_client_ip,
    normalize_allowed_peer_ips,
    render_hardened_html,
    validate_bind_host,
    wrap_server_tls,
)


class FrontendStaticHandler(BaseHTTPRequestHandler):
    index_path: Path = Path(__file__).resolve().parents[3] / "frontend" / "index.html"
    allowed_vpn_cidrs: Sequence[str] = ()
    allowed_peer_ips: Sequence[str] = ()
    allowed_hosts: Sequence[str] = ()
    allowed_origins: Sequence[str] = ()
    backend_port: int = 8000

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_headers_list(self, headers: Sequence[Tuple[str, str]]) -> None:
        for key, value in headers:
            self.send_header(key, value)

    def _reject(self, status: int, code: str, message: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_headers_list(build_security_headers(is_html=False))
        self.end_headers()
        self.wfile.write(json.dumps({"error": message, "reason_code": code}).encode("utf-8"))

    def do_GET(self) -> None:
        client_ip = self.client_address[0]
        parsed = urlparse(self.path)
        norm_headers = {str(k).lower(): str(v) for k, v in self.headers.items()}
        probe_secret = os.environ.get("TRADELENS_SUPERVISOR_PROBE_SECRET", "")
        supplied_probe = norm_headers.get("x-tradelens-supervisor-probe", "")
        is_valid_supervisor_probe = (
            parsed.path == "/frontend-health"
            and not parsed.query
            and len(probe_secret) >= 12
            and bool(supplied_probe)
            and hmac.compare_digest(supplied_probe, probe_secret)
        )
        net_ok, reason = is_allowed_remote_client_ip(
            client_ip,
            self.allowed_vpn_cidrs,
            allowed_peer_ips=None if is_valid_supervisor_probe else self.allowed_peer_ips,
        )
        if not net_ok:
            self._reject(403, reason, "Client IP outside approved private network / peer allowlist.")
            return

        host_ok, host_reason = is_allowed_host_header(
            norm_headers.get("host"),
            allowed_hosts=self.allowed_hosts,
            allowed_vpn_cidrs=self.allowed_vpn_cidrs,
        )
        if not host_ok:
            self._reject(403, host_reason, "Rejected unapproved Host header.")
            return

        origin_ok, allowed_origin, origin_reason = is_allowed_origin(
            norm_headers.get("origin"),
            allowed_origins=self.allowed_origins,
            allowed_hosts=self.allowed_hosts,
            allowed_vpn_cidrs=self.allowed_vpn_cidrs,
        )
        if not origin_ok:
            self._reject(403, origin_reason, "Rejected unapproved Origin header.")
            return

        if any(h in norm_headers and norm_headers[h].strip() for h in PROXY_HEADER_NAMES):
            peer_obj = _parse_ip_clean(client_ip)
            if not peer_obj.is_loopback:
                self._reject(403, "FORGED_PROXY_HEADERS", "Proxy headers from non-loopback peer rejected.")
                return

        has_secret, bad_param = has_forbidden_url_secret(parsed.query)
        if has_secret:
            self._reject(400, "URL_TOKEN_FORBIDDEN", f"Query parameter '{bad_param}' is forbidden.")
            return

        path = parsed.path
        decoded = unquote(path)
        if (
            "\x00" in path
            or "\x00" in decoded
            or ".." in path
            or ".." in decoded
            or "\\" in path
            or "\\" in decoded
            or "//" in path
        ):
            self._reject(404, "NOT_FOUND", "Only root dashboard asset is exposed.")
            return

        if path == "/frontend-health":
            if not self.index_path.exists():
                self._reject(503, "FRONTEND_ASSET_MISSING", "frontend/index.html missing.")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_headers_list(build_security_headers(allowed_origin=allowed_origin, is_html=False))
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": "ok", "service": "TradeLens AI Frontend Dashboard"}).encode("utf-8")
            )
            return

        if path not in {"/", "/index.html"}:
            self._reject(404, "NOT_FOUND", "Only root dashboard asset is exposed.")
            return

        if not self.index_path.exists():
            self._reject(503, "FRONTEND_ASSET_MISSING", "Dashboard HTML bundle missing.")
            return

        _, hostname = _extract_hostname_without_port(norm_headers.get("host", "127.0.0.1"))
        fmt_host = f"[{hostname}]" if ":" in hostname else (hostname or "127.0.0.1")
        connect_origins = (
            f"http://{fmt_host}:{self.backend_port}",
            f"https://{fmt_host}:{self.backend_port}",
        )
        content, html_headers = render_hardened_html(
            self.index_path.read_bytes(),
            connect_origins=connect_origins,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_headers_list(html_headers)
        self.end_headers()
        self.wfile.write(content)


def _is_loopback_bind(host: str) -> bool:
    clean = (host or "").strip().lower()
    if clean == "localhost":
        return True
    try:
        return _parse_ip_clean(clean).is_loopback
    except ValueError:
        return False


def build_frontend_server(
    bind_host: str = "127.0.0.1",
    port: int = 8080,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_origins: Optional[Sequence[str]] = None,
    backend_port: int = 8000,
    index_path: Optional[Path | str] = None,
    tls_certfile: Optional[str | Path] = None,
    tls_keyfile: Optional[str | Path] = None,
    allow_insecure_remote_http: bool = False,
    allowed_peer_ips: Optional[Sequence[str]] = None,
) -> HTTPServer:
    safe_host = validate_bind_host(bind_host, allowed_vpn_cidrs=allowed_vpn_cidrs)
    if not _is_loopback_bind(safe_host) and not allow_insecure_remote_http:
        if not tls_certfile or not tls_keyfile:
            raise ValueError(
                "TLS certificate and key (--tls-cert/--tls-key or TRADELENS_TLS_CERT_FILE/TRADELENS_TLS_KEY_FILE) "
                "are required when binding frontend server to a non-loopback remote VPN interface."
            )

    class ConfiguredFrontendHandler(FrontendStaticHandler):
        pass

    if index_path is not None:
        ConfiguredFrontendHandler.index_path = Path(index_path)
    ConfiguredFrontendHandler.allowed_vpn_cidrs = tuple(allowed_vpn_cidrs or ())
    ConfiguredFrontendHandler.allowed_peer_ips = tuple(normalize_allowed_peer_ips(allowed_peer_ips))
    ConfiguredFrontendHandler.allowed_hosts = tuple(allowed_hosts or ())
    ConfiguredFrontendHandler.allowed_origins = tuple(allowed_origins or ())
    ConfiguredFrontendHandler.backend_port = int(backend_port)
    httpd = HTTPServer((safe_host, int(port)), ConfiguredFrontendHandler)
    wrap_server_tls(httpd, tls_certfile=tls_certfile, tls_keyfile=tls_keyfile)
    return httpd


def run_frontend_server(
    bind_host: str = "127.0.0.1",
    port: int = 8080,
    allowed_vpn_cidrs: Optional[Sequence[str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_origins: Optional[Sequence[str]] = None,
    backend_port: int = 8000,
    tls_certfile: Optional[str | Path] = None,
    tls_keyfile: Optional[str | Path] = None,
    allow_insecure_remote_http: bool = False,
    allowed_peer_ips: Optional[Sequence[str]] = None,
) -> None:
    cert = tls_certfile or os.environ.get("TRADELENS_TLS_CERT_FILE")
    key = tls_keyfile or os.environ.get("TRADELENS_TLS_KEY_FILE")
    eff_cidrs = (
        allowed_vpn_cidrs
        if allowed_vpn_cidrs is not None
        else [c.strip() for c in os.environ.get("TRADELENS_ALLOWED_VPN_CIDRS", "").split(",") if c.strip()]
    )
    eff_peers = (
        allowed_peer_ips
        if allowed_peer_ips is not None
        else [p.strip() for p in os.environ.get("TRADELENS_ALLOWED_PEER_IPS", "").split(",") if p.strip()]
    )
    eff_hosts = (
        allowed_hosts
        if allowed_hosts is not None
        else [h.strip() for h in os.environ.get("TRADELENS_ALLOWED_HOSTS", "").split(",") if h.strip()]
    )
    eff_origins = (
        allowed_origins
        if allowed_origins is not None
        else [o.strip() for o in os.environ.get("TRADELENS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    )
    httpd = build_frontend_server(
        bind_host=bind_host,
        port=port,
        allowed_vpn_cidrs=eff_cidrs,
        allowed_peer_ips=eff_peers,
        allowed_hosts=eff_hosts,
        allowed_origins=eff_origins,
        backend_port=backend_port,
        tls_certfile=cert,
        tls_keyfile=key,
        allow_insecure_remote_http=allow_insecure_remote_http,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
