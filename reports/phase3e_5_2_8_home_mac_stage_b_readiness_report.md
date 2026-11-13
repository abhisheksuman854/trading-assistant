# Phase 3E.5.2.8: Home Mac Stage B Readiness Review & Remote Access Pre-Provisioning Audit

**Date:** 2026-11-12  
**Machine:** Home Mac (`abhisheks-MacBook-Pro.local`)  
**Repository:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`  
**Branch:** `main` (`HEAD`: `fd24424d563770684d33a37afc4f0d7b3b6969f8`)  
**Mode:** Strictly Offline, Read-Only Audit  
**Verdict:** `STAGE_B_READINESS_REVIEW_PASSED`

---

## 1. Step A — Machine & Repository Identity

- **Hostname:** `abhisheks-MacBook-Pro.local`
- **macOS:** `26.0` (Build `25A354`), Architecture: `arm64`
- **Repository Root:** `/Users/abhisheksuman/Documents/GitHub/trading-assistant`
- **Branch / HEAD:** `main` / `fd24424d563770684d33a37afc4f0d7b3b6969f8`
- **Python Interpreter:** `backend/.venv/bin/python3.12` (`Python 3.12.13`)
- **Git Status:**
  - ` M backend/tests/test_gitignore_environment_protection.py` (`1 file changed, 60 insertions(+), 10 deletions(-)`)
  - `?? reports/phase3e_5_2_7_6_home_mac_local_verification_report.md`
  - `?? reports/phase3e_5_2_7_7_home_mac_test_portability_report.md`
  - `?? reports/phase3e_5_2_7_8_home_mac_foreground_runtime_report.md`
  - `?? reports/phase3e_5_2_7_9_home_mac_checksum_and_runtime_baseline_report.md`
- **Dataset SHA-256 Checksums (verified intact):**
  - `3be81f5e3ef7955343cadc43bc6e438285de070fd78d530610ea7b3276917adf  data/checkpoints/checkpoint_nifty_2026-11-02_2026-11-06.json`
  - `9c7008c81e2045e6fc52fc5a197cf9891d30e45d38d9f342b03a013365fa0065  data/checkpoints/raw_nifty_2026-11-02_2026-11-06_chunk_0.json`
  - `bd0ae4959ee8d3ed5c9bea5877343f74883894d71651a74bbfc19fd8d8943d34  data/historical/spot/NIFTY/manifests/manifest_nifty_69ca9b77.json`
  - `2f3010fbca42bebbd0c7ce5c8876aa7a954c1bca48a304fd2758c906ce7ebde0  data/historical/spot/NIFTY/year=2026/month=11/candles_NIFTY_2026_11.csv`

---

## 2. Step B — Remote Access Architecture Security Controls (1–18)

| # | Control | Source Reference | Enforcement Mechanism | Test Coverage | Status |
|---|---|---|---|---|---|
| 1 | Private-network-only remote binding | `security.py:363-408`, `main.py:1198`, `frontend_server.py:184` | `validate_bind_host` allows only loopback or Tailscale/approved VPN CIDRs; rejects `0.0.0.0`, `::`, LAN, public IPs | `test_private_network_provisioning_stage_a.py` | **PASS** |
| 2 | Explicit device approval | `security.py:1547-1718`, `stage_b_setup.py:145-156` | Newly registered keys start `PENDING`; require owner-authenticated `approve_device` (`ACTIVE`) before auth | `test_remote_access_security_audit.py` | **PASS** |
| 3 | Device-bound cryptographic identity | `security.py:757-885, 916-1055` | EC P-256 keypair (`DeviceKeyCrypto` / `MacOSKeychainKeyStore`), SHA-256 fingerprint, signature verification | `test_remote_access_security_audit.py` | **PASS** |
| 4 | Short-lived sessions | `security.py:1243-1289, 2268-2271` | `session_ttl_seconds=900` (clamped `60..3600`), `challenge_ttl_seconds=60` (`15..300`) | `test_remote_access_security_audit.py` | **PASS** |
| 5 | Session revocation | `security.py:1523-1534, 1719-1745, 2255-2259` | `revoke_device` and `_revoke_device_sessions_and_challenges` mark all device sessions `revoked=True` | `test_remote_access_security_audit.py` | **PASS** |
| 6 | Cross-process revocation enforcement | `security.py:1274-1284, 2310` | `_sync_state_from_disk_if_changed()` reloads HMAC-verified `remote_auth.json` on each request | `test_stage_b_setup_and_mise.py` (`test_12`) | **PASS** |
| 7 | Strict application-layer authentication | `security.py:2503-2525` | All non-loopback requests require valid `Authorization: Bearer <token>` matching active IP-pinned session | `test_remote_access_security_audit.py` | **PASS** |
| 8 | Server-side owner/remote role separation | `security.py:2468-2500` | `HOME_OWNER_ONLY_ENDPOINTS` reject non-loopback with `403 HOME_OWNER_ONLY_OPERATION` + require owner secret on loopback | `test_remote_access_security_audit.py` | **PASS** |
| 9 | Remote read-only route allowlisting | `security.py:57-73, 2277-2299` | Explicit `ALLOWED_REMOTE_ROUTE_METHODS` + `ALLOWED_REMOTE_PREFIXES = ("/api/stock/",)`; all else `403`/`405` | `test_remote_access_security_audit.py` | **PASS** |
| 10 | Owner-only endpoint protection | `security.py:76-91, 2469-2490`, `stage_b_setup.py:2928, 2983` | Covers shutdown, broker auth, device lifecycle, setup mutations, and ledger mutations | `test_remote_access_security_audit.py` | **PASS** |
| 11 | No remote access to broker credentials | `security.py:57-91`, `main.py` | Zero endpoints expose `.env`, `ANGEL_*`, `jwt_token`, or `feed_token` | `test_remote_access_security_audit.py` | **PASS** |
| 12 | No remote broker authentication | `security.py:78`, `main.py:981` | `/api/broker/authenticate` is in `HOME_OWNER_ONLY_ENDPOINTS` (`403` remotely) | `test_remote_access_security_audit.py` | **PASS** |
| 13 | No remote order placement | `security.py:93-111, 2282-2288`, `main.py:212` | `is_forbidden_order_execution_path` blocks `/api/order*`, `/api/trade*`, `/api/execute*`; `order_execution_enabled=False` | `test_remote_access_security_audit.py` | **PASS** |
| 14 | No public listener fallback | `security.py:374-395`, `main.py:1198`, `cli.py:320` | `validate_bind_host` raises `RefusePublicBindError` on `0.0.0.0`, `::`, LAN, or public IPs | `test_private_network_provisioning_stage_a.py` | **PASS** |
| 15 | No default permissive CORS or Host behavior | `security.py:487-565`, `frontend_server.py:79-96` | `is_allowed_host_header` & `is_allowed_origin` enforce explicit allowlists / VPN literal IPs | `test_remote_access_security_audit.py` | **PASS** |
| 16 | Secure secret redaction | `security.py:661-756`, `angel_one.py:88-89` | `collect_known_secrets`, `redact_runtime_text`, `redact_payload`, `RedactingLogFilter` | `test_remote_access_security_audit.py` | **PASS** |
| 17 | Fail-closed behavior on invalid config | `security.py:1275-1283`, `main.py:1199-1204`, `cli.py:211-215` | Missing/weak `TRADELENS_STATE_HMAC_SECRET` or missing TLS on non-loopback raises `PermissionError`/`ValueError` | `test_pre_provisioning_security_gate_stage_a1.py` | **PASS** |
| 18 | Safe shutdown and session invalidation | `supervisor.py:569-586`, `cli.py:353-357`, `main.py:1254-1259` | Terminates children, marks clean shutdown, releases `flock`, supports `owner-shutdown` lock | `test_home_mac_runtime_and_remote_availability.py` | **PASS** |

---

## 3. Step C — Tailscale Readiness (Read-Only Inspection)

- **Installation Status:** `NOT_INSTALLED` (`which tailscale` not found; `/Applications/Tailscale.app`, `/opt/homebrew/bin/tailscale`, `/usr/local/bin/tailscale` absent; no Tailscale `launchd` service running; no Tailscale IPv4/IPv6 assigned).
- **Application Support:** `normalize_allowed_peer_ips` (`security.py:411`) and `is_allowed_remote_client_ip` (`security.py:456`) support exact Office Mac peer IP pinning and `100.64.0.0/10` CIDR allowlisting. `docs/operations/STAGE_B_SETUP.md` (Step 3, lines 100–108) documents least-privilege Tailscale ACLs.
- **Three Separate Security Layers:**
  1. **Private-network transport protection (Tailscale/WireGuard + ACLs):** Restricts network reachability to authenticated tailnet peers on ports `8000`/`8080`.
  2. **TLS server identity verification (HTTPS + SAN + SHA-256 fingerprint):** Authenticates the Home Mac server and encrypts HTTP traffic in transit inside the tunnel.
  3. **Application-level device authentication (P-256 challenge-response + short-lived IP-pinned bearer token):** Verifies the approved Office Mac device identity and enforces read-only route allowlists and owner/remote separation.

---

## 4. Step D — TLS Readiness

- **Implementation (`cli.py:555-635`, `security.py:2597-2619`, `stage_b_setup.py:1405-1485`):**
  - `generate-tls-cert` generates EC P-256 (`prime256v1`) SHA-256 certificates (`basicConstraints = critical, CA:TRUE, pathlen:0`, `keyUsage = critical, digitalSignature, keyCertSign`, `extendedKeyUsage = serverAuth`), validity clamped `1..825` days, private key `0600`.
  - Required SANs: `IP:<HOME_TAILSCALE_IP>` plus optional `DNS:<HOME_MAGICDNS_NAME>` (`*.ts.net`).
  - `wrap_server_tls` enforces `ssl.TLSVersion.TLSv1_2` minimum on both backend (`main.py:1206`) and frontend (`frontend_server.py:203`).
  - Recovery/rotation documented in `docs/operations/STAGE_B_SETUP.md:278-283`.
- **Deployment Distinction:**
  - Certificate generation implemented: **Yes**
  - Certificate generated on Home Mac: **No**
  - Certificate installed / trusted on Office Mac: **No**
  - Live TLS handshake over Tailscale verified: **No**

---

## 5. Step E — Office Mac Device Registration Design

- **Key Generation & Storage:** Private key generated on Office Mac; never transmitted to Home Mac. `MacOSKeychainKeyStore` (`security.py:916-1055`) stores persistent PEM keys in macOS Keychain with `-T ""` (no silent creator-app access); Browser WebCrypto (`STAGE_B_SETUP.md:293-298`) uses an ephemeral in-RAM key lost on reload (requires re-enrollment).
- **Approval & Auth:** `register-device` creates `PENDING` record; explicit owner `approve-device` required (`ACTIVE`). Challenge is single-use, 60s TTL, bound to device ID/peer context, cannot downgrade to static shared secret. Session tokens are 900s TTL, IP-pinned, revocable; re-enrollment of a revoked device requires fresh owner approval (`security.py:1547`). No owner secret or Angel One credential is stored on Office Mac.

---

## 6. Step F — Stage B Wizard & Recovery Review

- **10-Step Prerequisites (`stage_b_setup.py:80-220`):** Enforced programmatically; Step 10 (`launchd_status` / LaunchAgent install) is locked until Steps 2–9 are all `VERIFIED`.
- **Anti-Bypass & Freshness Guards:** Manual confirmation cannot bypass live checks; simulated self-tests (`revocation-selftest`) are labeled `simulated_only` and do not satisfy Step 9 (`revocation-check`); reboot invalidates prior boot runtime evidence (`STALE_PRIOR_BOOT`); peer IP drift triggers re-verification.
- **Recovery & Operations (`docs/operations/STAGE_B_SETUP.md:244-319`):** Covers reboot, sleep/network drop, Tailscale disconnect, Office IP change, TLS expiry/rotation, revocation, permanent Office Mac retirement (`remove-device --reason PERMANENTLY_RETIRED`), LaunchAgent bootout, and local `owner-shutdown` / `resume` without Office Mac access. Stage B progress store contains no secrets.

---

## 7. Step G — Home Mac Operational Readiness

- `.env`: Regular non-symlink file, mode `0600`, gitignored (`.gitignore:11:*.env`), untracked.
- Python: `backend/.venv/bin/python3.12` (`Python 3.12.13`).
- LaunchAgent templates: Render cleanly with Home Mac repo/venv paths and pass `plutil -lint`; none installed in `~/Library/LaunchAgents`.
- Power/Sleep/Preboot: Scoped `caffeinate -i -s` on AC power during trading hours; battery/lid-clamshell and FileVault preboot limitations documented in `power_policy.py` and `STAGE_B_SETUP.md`.
- Fail-closed & safety: `is_authenticated=False` until explicit owner unlock; `order_execution_enabled=False`.

---

## 8. Step H — Offline Test Results

Executed with `backend/.venv/bin/python3.12`:
- **Command:** `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m unittest backend/tests/test_gitignore_environment_protection.py backend/tests/test_remote_access_security_audit.py backend/tests/test_pre_provisioning_security_gate_stage_a1.py backend/tests/test_private_network_provisioning_stage_a.py backend/tests/test_home_mac_runtime_and_remote_availability.py backend/tests/test_stage_b_setup_and_mise.py`
- **Result:** `Ran 77 tests in 16.637s` — **OK** (`77` passed, `0` failed, `0` errors, `0` skipped, exit code `0`).

---

## 9. Step I — Final Stage B Readiness Matrix

| Area | Status | Evidence | Remaining Action |
|---|---|---|---|
| Home Mac local baseline | `VERIFIED` | Repo `fd24424`, `.env` `0600`, Python `3.12.13`, 4 dataset SHA-256 hashes matched, foreground loopback verified & stopped | Commit/push Phase 3E.5.2.7.7 test fix when ready |
| Application security | `VERIFIED` | 18/18 controls verified in `security.py`, `main.py`, `frontend_server.py`; 77/77 offline tests pass | None for code; live Stage B drills after Tailscale |
| Tailscale | `NOT_INSTALLED` | `which tailscale` / `/Applications/Tailscale.app` / `launchctl` show absent | Install & authenticate Tailscale on Home & Office Macs; set ACLs |
| TLS | `IMPLEMENTED_NOT_DEPLOYED` | `generate-tls-cert` + `wrap_server_tls` (TLS 1.2+, SAN, EKU, `0600` key) verified in code & temp fixtures | Generate cert for Home Tailscale IP/MagicDNS & trust fingerprint on Office Mac |
| Office Mac device registration | `IMPLEMENTED_NOT_DEPLOYED` | P-256 Keychain + WebCrypto + owner approval flows verified in code & tests | Generate Office Mac key, register & approve on Home Mac |
| Remote authentication | `IMPLEMENTED_NOT_DEPLOYED` | 60s challenge-response, 900s IP-pinned session verified in code & tests | Verify live HTTPS challenge-response in Step 7 |
| Read-only API enforcement | `VERIFIED` | `ALLOWED_REMOTE_ROUTE_METHODS`, `HOME_OWNER_ONLY_ENDPOINTS`, `order_execution_enabled=False` | Re-verify live in Step 8 |
| Revocation | `VERIFIED` | Code + cross-process HMAC-verified self-test & unit tests pass | Execute live post-revocation drill in Step 9 |
| Recovery | `VERIFIED` | Documented in `STAGE_B_SETUP.md` §4 + `owner-shutdown`/`resume`/`remove-device` | None |
| LaunchAgent readiness | `IMPLEMENTED_NOT_DEPLOYED` | Renders Home Mac paths & passes `plutil -lint`; gated behind Steps 2–9 | Install only after Steps 2–9 are all `VERIFIED` |
| Operational readiness | `VERIFIED` | Offline baseline, power/sleep/FileVault docs, fail-closed auth verified | Ensure AC power & lid open/clamshell for market-hours hosting |

---

## 10. Final Verdict & Prerequisites Requiring Explicit Approval

**Verdict:** `STAGE_B_READINESS_REVIEW_PASSED`

No provisioning, network changes, certificate generation, device registration, broker authentication, or live network actions occurred in this phase. Before beginning Stage B provisioning (Phase 3E.5.2.8.1+), explicit owner approval is required for:
1. Installing and signing into Tailscale on both Home Mac and Office Mac, and configuring least-privilege Tailscale ACLs.
2. Generating `TRADELENS_STATE_HMAC_SECRET` and initializing the Home Mac owner secret (`init-owner --owner-secret-prompt --remote-access`).
3. Generating the Home Mac Tailscale TLS certificate (`generate-tls-cert`) and trusting its SHA-256 fingerprint on Office Mac.
4. Generating the Office Mac Keychain device key, registering/approving it on Home Mac, and executing live Steps 7–9 verification drills (with `order_execution_enabled=False` and `ANGEL_*` remaining local-only).

