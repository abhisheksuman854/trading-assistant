# Phase 3E.5.2.6 — Controlled Stage B Provisioning, Setup Dashboard & Mise Report

- **Generated (UTC):** 2026-11-11T10:33:54+00:00
- **Host:** Darwin 25.0.0 (arm64), Python 3.12.13
- **Live Provisioning Verdict:** `STAGE_B_BLOCKED`
- **Order Execution Enabled:** `False` (permanently disabled)
- **Broker Authentication Executed:** `False` (strictly excluded)
- **Phase 3F Started:** `False` (strictly excluded)

## 1. Delivered Software Architecture & UI Controls

- **Setup & Remote Access Dashboard (`frontend/index.html`):** `#tab-setup` & `#panel-setup` featuring 9-card Overview, Sequential 10-Step Setup Wizard, native `<dialog>` confirmation modal (`#setupConfirmDialog`), accessible copy-feedback live regions, and 10-playbook Setup Recovery Center.
- **Persistent Secret-Free Progress Store:** `stage_b_progress.json` (`0600` file inside `0700` directory, refreshed dynamically from live read-only probes).
- **`mise.toml` Integration & CLI Parity:** 16 `tradelens-stage-b-*` tasks mapped cleanly to `backend.app.runtime.cli stage-b` and verified against `cli.build_parser()`.
- **Documentation:** `docs/operations/STAGE_B_SETUP.md` covers Home/Office command labels, prerequisites, security rules, `mise` tasks, and all 10 recovery playbooks.

## 2. Environment & Preflight Observations (Home Mac vs Unprovisioned Hardware)

- **`.env` present:** `True`, **symlink:** `False`, **mode:** `0o644`, **strict 0600 ok:** `False`
- **Angel One key readiness (format status only, zero secret values):** `{"ANGEL_API_KEY_FORMAT_STATUS": "PRESENT_VALID_FORMAT", "ANGEL_CLIENT_CODE_FORMAT_STATUS": "PRESENT_VALID_FORMAT", "ANGEL_PASSWORD_FORMAT_STATUS": "PRESENT_VALID_FORMAT", "ANGEL_TOTP_SECRET_FORMAT_STATUS": "PRESENT_VALID_FORMAT"}`
- **Tailscale installed:** `False`, **running:** `False`, **state:** `NOT_INSTALLED`
- **TLS configured:** `False`, **valid:** `False`, **SAN:** `[]`
- **Registered devices:** total=`0`, approved=`0`, active=`0`, revoked=`0`
- **LaunchAgent plists rendered & linted:** `True`, **installed into `~/Library/LaunchAgents`:** `False` (strictly locked until Steps 2–9 pass)

## 3. Guided 10-Step Stage B Status Table

| Step | Title | Status | Reason / Verification Summary |
| --- | --- | --- | --- |
| Step 1 | Home Mac Preflight | `REQUIRES_OWNER_ACTION` | Home Mac .env permissions are 0o644; run `chmod 600 .env` before Stage B. |
| Step 2 | Tailscale Installation & Sign-In (Home + Office Mac) | `BLOCKED` | Locked until prerequisites verified: step_01_home_mac_preflight. |
| Step 3 | Tailscale ACL Verification | `BLOCKED` | Locked until prerequisites verified: step_02_tailscale_home_office. |
| Step 4 | TLS Certificate Setup (SAN + Fingerprint) | `BLOCKED` | Locked until prerequisites verified: step_01_home_mac_preflight, step_02_tailscale_home_office. |
| Step 5 | Office Mac Device Key Generation & Registration | `BLOCKED` | Locked until prerequisites verified: step_01_home_mac_preflight, step_04_tls_certificate_setup. |
| Step 6 | Owner Approval & Exact IP Pin Verification | `BLOCKED` | Locked until prerequisites verified: step_05_office_mac_device_key_gen. |
| Step 7 | Remote HTTPS Challenge-Response Verification | `BLOCKED` | Locked until prerequisites verified: step_03_tailscale_acl_verification, step_04_tls_certificate_setup, step_06_owner_approval_device_pin. |
| Step 8 | Security Boundary & Negative Tests | `BLOCKED` | Locked until prerequisites verified: step_07_remote_https_challenge_verify. |
| Step 9 | Revocation & Re-Enrolment Test | `BLOCKED` | Locked until prerequisites verified: step_08_security_boundary_tests. |
| Step 10 | LaunchAgent Installation (Locked Until Steps 2-9 Verified) | `BLOCKED` | Locked by policy: LaunchAgents MUST NOT be installed until Steps 2 through 9 are all VERIFIED. |

## 4. Automated Security Boundary & Isolation Verification

- **Core offline boundary checks passed:** `True`
- **All Stage B live+offline boundary checks passed:** `False`
- **Isolated cross-process revocation self-test:** `NOT_RUN` (live Office Mac drill: `False`)

- `order_execution_disabled` -> **PASS**: Order execution paths blocked; read-only assistance architecture active.
- `wildcard_bind_rejected` -> **PASS**: 0.0.0.0 bind test -> allowed=False (PUBLIC_WILDCARD_BIND_FORBIDDEN)
- `home_owner_only_setup_isolation` -> **PASS**: All setup mutation, device approval, and broker endpoints are restricted to HOME_OWNER_ONLY_ENDPOINTS.
- `dotenv_permissions_0600` -> **PENDING_OR_FAIL**: .env hardening incomplete (exists=True, mode=0o644, symlink=False).
- `csp_and_http_headers` -> **PASS**: Strict nonce CSP, frame-ancestors 'none', X-Frame-Options DENY.
- `exact_peer_ip_pinning` -> **PENDING_OR_FAIL**: All approved Office Mac devices must have exact pinned Tailscale peer IPs.
- `tailscale_and_tls_transport` -> **PENDING_OR_FAIL**: Awaiting running Tailscale tunnel + valid Home Mac SAN TLS certificate.

## 5. Remaining Owner Actions Before Full Live Stage B Sign-Off

1. Restrict `.env` permissions on Home Mac (`chmod 600 .env`) without printing secrets, then run `mise run tradelens-stage-b-home-preflight`.
2. Install and sign into Tailscale on Home Mac and Office Mac; configure least-privilege Tailscale ACLs and confirm in Setup Wizard Steps 2–3.
3. Generate Home Mac SAN TLS certificate (`generate-tls-cert`) targeting Home Tailscale IP and MagicDNS name.
4. Initialize Home Mac owner secret (`init-owner --remote-access --owner-secret-prompt`), generate Office Mac Keychain device key (`generate-device-key`), register & approve device with exact Office Mac Tailscale IP.
5. Verify foreground HTTPS challenge-response from Office Mac, complete security & live revocation drills (Steps 7–9), and only then install LaunchAgents into `~/Library/LaunchAgents`.
