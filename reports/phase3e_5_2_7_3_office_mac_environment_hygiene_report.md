# TradeLens AI — Phase 3E.5.2.7.3: Office Mac Environment Hygiene & Credential Isolation Audit Report

- **Audit Date:** 2026-11-12
- **Phase:** 3E.5.2.7.3 (Read-Only Office Mac Environment Hygiene & Credential Isolation Audit)
- **`.env` Presence Classification:** `OFFICE_ENV_ABSENT`
- **Final Audit Verdict:** `OFFICE_ENVIRONMENT_CLEAN`

---

## 1. Executive Summary

This read-only security and environment hygiene audit verifies that the Office Mac repository at `/Users/abhisheksuman/Documents/GitHub/tradelens-ai` retains **no** Home Mac `.env` file, **no** Angel One broker credentials, **no** owner/HMAC secrets, **no** broker session tokens (`jwtToken`, `feedToken`, `refreshToken`), and **no** private TLS keys outside synthetic unit-test fixtures and documentation placeholders.

Without `.env`, the backend remains **fail-closed** (`order_execution_enabled=False`, zero automatic Angel One login, `UNAVAILABLE (Fail-Closed Safety Mode)` health/scan status), while offline engineering workflows and the full backend test suite (`490` unit tests + smoke suite) execute cleanly on the Office Mac.

---

## 2. Office Mac & Repository Identity Verification

| Check | Observed Value |
|---|---|
| `hostname` | `Abhisheks-MacBook-Air.local` |
| Current Working Directory (`pwd`) | `/Users/abhisheksuman/Documents/GitHub/tradelens-ai` |
| Git Repository Root | `/Users/abhisheksuman/Documents/GitHub/tradelens-ai` |
| Current Branch | `main` (`origin/main` tracking; `[ahead 1]` local commit unpushed, untouched by this audit) |
| `HEAD` Commit | `9d1329e3afbc8b7aafd971246f303a68f317c0eb` |
| Git Working Tree Status (audit start) | Clean (empty `git status --short`) |
| Workspace Identity Confirmation | Confirmed Office Mac development workspace (`/Users/abhisheksuman/Documents/GitHub/tradelens-ai`), distinct from Home Mac (`/Users/abhisheksuman/Documents/GitHub/trading-assistant`) |

---

## 3. `.env` Presence, Metadata & Git Hygiene Inspection

Filesystem metadata and Git tracking checks were performed without printing or exposing secret values:

| Check | Observed Result | Classification / Notes |
|---|---|---|
| `.env` presence at repository root | Absent (`exists: false`) | `OFFICE_ENV_ABSENT` |
| Other env variants (`.env.local`, `.env.dev`, `.env.production`, etc.) | Absent (`find . -maxdepth 3 -name ".env*"` returned `./.env.example` only) | Clean |
| `.env.example` | Present (`1586` bytes, `-rw-r--r--`), tracked by Git | Contains placeholders only (`your_api_key_here`, `your_client_code_here`, `your_password_or_mpin_here`, `your_totp_secret_here`, `generate_min_32_byte_random_hex_hmac_secret`), default `PORT=8000`, `TRADELENS_BIND_HOST=127.0.0.1` |
| Git tracking of `.env*` | Only `.env.example` tracked (`git ls-files .env .env.example ".env.*"`) | Clean |
| `.gitignore` rule for `.env` | Line 7 ignores `.env`; lines 13–16 ignore `deploy/launchd/*.plist`, `*.crt`, `*.key`, `*.pem`; lines 11–12 ignore `runtime_state/`, `runtime_logs/` | Advisory: `.env.local` / `.env.dev` / `.env.production` would not match line 7 (`.env` exact); none currently exist on disk |
| Git history check for `.env` / keys / certs | `git log --all --stat -- ".env" ".env.*" "*.key" "*.pem" "*.crt"` showed `.env.example` commits only (`f4d6b0f`, `3db90d8`, `1a1623b`); zero `.env`, `.key`, `.pem`, or `.crt` commits | Clean |
| `runtime_state/` and `runtime_logs/` on disk | Absent on Office Mac | Clean |
| `deploy/launchd/` rendered plists | Only `deploy/launchd/README.md` present; zero rendered `.plist` files | Clean |

---

## 4. Credential Isolation Inspection

Non-secret-printing pattern scans and targeted line inspections were executed across source code, tests, docs, scratch artifacts, ops state, `.git/config`, and process environment variables:

| Scope / File & Line | Credential Category | Classification | Real Exposure? |
|---|---|---|---|
| Process environment (`os.environ`) | `ANGEL_*`, `TRADELENS_*` | `0` variables present (`[]`) | No |
| `.git/config` line 9 (`remote "origin"` URL) | GitHub PAT / credentials | Username-only HTTPS remote URL (length `16`); zero embedded PAT/password | No |
| Repo-wide secret prefix scan (`ghp_`, `github_pat_`, `AIza`, `AKIA`) | Personal access tokens / cloud keys | `0` matches across entire non-`.git`/non-`.venv` repository | No |
| Repo-wide JWT scan (`eyJ...` 3-part JWS) — `backend/tests/test_broker_live_integration.py` (L87–88), `backend/tests/test_provider_probe_audit.py` (L73–74), `backend/tests/test_home_mac_runtime_and_remote_availability.py` (L123) | Mock Angel One JWT/refresh payloads | `5` synthetic test fixtures (`mock_signature_for_testing`, `mock_payload_token_*`, `stub_payload`) | No (Safe mock/test fixture) |
| Repo-wide PEM scan (`-----BEGIN * PRIVATE KEY-----`) — `backend/tests/test_remote_access_security_audit.py` (L480, L869, L879) | Synthetic PEM redaction test string | Synthetic `"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"` negative test | No (Safe synthetic test fixture) |
| Credential literal assignment scan (`ANGEL_*`, `TRADELENS_*_SECRET`, token keys) — `backend/tests/test_provider_probe_audit.py` (L210–212), `backend/tests/test_pre_provisioning_security_gate_stage_a1.py` (L508–509) | Mock env fixtures | `0` non-test/non-doc literal assignments; all test strings are mock values (`MockApiKeyAlpha99`, `MockPasswordSecret77`, `SHOULD_STAY_OUT_OF_CLI`) | No (Safe test fixture) |
| `backend/app/data/angel_one.py` (L60–157, L209, L252, L288, L527, L546) & `backend/app/data/historical_acquisition.py` (L416–731) | `jwt_token`, `feed_token` handling | Held in instance memory only during active runtime; scrubbed by `_redact()`; never written to JSON/disk | No (Safe runtime handling) |
| `backend/app/runtime/security.py` (L163, L170, L714–718) | Token/secret redaction keys | Redaction key lists and regex filters preventing secret leakage in logs/JSON | No (Defensive control) |
| `frontend/index.html` | Broker/owner/HMAC/JWT references | `0` hits for `ANGEL_`, `TRADELENS_OWNER`, `HMAC`, `jwtToken`, `feedToken` | No |
| `scratch/provider_probe_audit_2026-11-06.json` (L324–327, L409–579) & `scratch/*.py` | Env var readiness metadata | Key names only (`"Missing/placeholder env vars"`); `0` JWTs, `0` `ghp_`, `0` assignments in `scratch/*.py` | No (Metadata keys only) |
| `.ops_state/stage_b_progress.json` (L1–196) | Stage B setup progress snapshot | Secret-free state (`step_01_home_mac_preflight=VERIFIED`, `step_02_tailscale_home_office=REQUIRES_OWNER_ACTION`, Steps 3–10 `BLOCKED`, `tls_fingerprint=null`, empty device maps) | No |

---

## 5. Application Behaviour Without `.env` (Fail-Closed Verification)

All checks below were executed strictly in-process without starting any HTTP server, supervisor, daemon, or broker network request:

1. **Parent-directory isolation (`backend/app/main.py` L28–65 & `backend/app/runtime/cli.py` L78–123):**
   - `read_env_dict(base_env={}, env_path=Path(".env"), require_strict_perms=False)` resolves strictly `REPO_ROOT / ".env"` (or explicit `TRADELENS_ENV_FILE`) and never traverses parent directories. Verified output keys with `.env` absent: `[]`.
   - CLI `_load_cli_dotenv(strict_perms=...)` returns `None` cleanly when `.env` is absent; `_verify_dotenv_perms_if_remote(True)` safely no-ops when `.env` does not exist on local/offline CLI invocations, whereas `deploy/scripts/run_launchd_service.sh` allows missing `.env` in local mode and fails closed (exit `78`) in remote mode.
2. **Offline initialization & zero automatic login (`TradeLensAppRuntime`):**
   - Instantiating `TradeLensAppRuntime(state_dir=<temp>/strict, environ={})` triggers zero Angel One login on import or initialization.
   - `order_execution_enabled` is `False` in both strict (`environ={}`) and simulated fallback (`TRADELENS_ALLOW_SIMULATED_FALLBACK="1"`) modes.
   - Calling `rt_strict.authenticate_broker(verify_fresh_tick=False)` prints `[!] Angel One credentials missing in .env.` and leaves `engine_status='UNAVAILABLE'`, `data_status='UNAVAILABLE'`, `signals['broker_auth_status']='AUTH_FAILED'`, and `data_source='UNAVAILABLE (Fail-Closed Safety Mode)'`.
   - Calling `rt_strict.get_health_payload()` and `rt_strict.get_scan_payload()` returns `data_status='UNAVAILABLE'` and `proves_live_market_data=False`.
   - Calling `rt_sim.get_health_payload()` and `rt_sim.get_scan_payload()` with `TRADELENS_ALLOW_SIMULATED_FALLBACK="1"` also reports `order_execution_enabled=False`, `engine_status='UNAVAILABLE'`, `data_status='UNAVAILABLE'`, and `candidates: 0` without attempting broker login.
3. **Broker provider & placeholder gating:**
   - `AngelOneSmartAPIProvider().connect()` without credentials prints `[!] Angel One credentials missing in .env.` and returns `False` without initiating network authentication.
   - `RuntimeHealthCoordinator().check_provider_config_readiness({...})` marks `.env.example` placeholder values as `'PLACEHOLDER'` with `all_ready: False`.
4. **`mise.toml` task safety:**
   - Inspected `mise.toml` (L1–123): `[env]` sets `PYTHONPATH` and `_.path` (`backend/.venv/bin`) only; zero credential variables or secret defaults exist.

---

## 6. Offline Development & Testing Readiness

The following offline test suites were executed on the Office Mac with `.env` absent:

| Command | Outcome | Observed Details |
|---|---|---|
| `PYTHONPATH=.:backend backend/.venv/bin/python3.12 -m unittest discover -s backend/tests -v` | **PASS** | `Ran 490 tests in 103.604s — OK` (`0` failures, `0` errors, `0` skipped tests verified across full log) |
| `PYTHONPATH=.:backend backend/.venv/bin/python3.12 backend/test_api.py` | **PASS** | Black-Scholes Greeks (`PASSED`), Universe shortlisting scanner (`PASSED`), `'Why NOT to Trade'` Veto Gate (`PASSED`) — `ALL TESTS PASSED!` |
| Frontend inline `<script>` presence (`frontend/index.html`) | **Verified (structure)** | `1` inline script block (`144,301` bytes); `node` is not installed on PATH in this environment, so standalone `node --check` was not executed (`FileNotFoundError`) and JS syntax was not checked outside Python inspection |

**Result:** Offline backend development, static/runtime unit testing, Stage B security unit tests, and simulated development workflows remain completely functional on the Office Mac without `.env`.

---

## 7. Home Mac vs. Office Mac Responsibility Separation

| Responsibility | Office Mac (`/Users/abhisheksuman/Documents/GitHub/tradelens-ai`) | Home Mac (`/Users/abhisheksuman/Documents/GitHub/trading-assistant`) |
|---|---|---|
| Code editing, refactoring, portability checks | **Active** | Pulls clean tested commits from GitHub |
| Offline unit/regression testing (`unittest`, `test_api.py`) | **Active** (verified 490/490 tests without `.env`) | Local runtime verification |
| `.env` with Angel One credentials (`0600`) | **Must remain absent** (`OFFICE_ENV_ABSENT`) | **Owner-provisioned locally (`0600`)** |
| Owner secret, HMAC secret, SAN TLS cert/key (`0600`) | **Must remain absent** | **Owner-provisioned locally (`0600`)** |
| Supervisor / LaunchAgents / Angel One market-hours session | **Disabled / not run here** | **Active runtime host** (with `order_execution_enabled=False`) |
| Remote dashboard access | Approved Tailscale client device (`Step 5` client key in Keychain when configured by owner) | Tailscale HTTPS server endpoint |

---

## 8. Non-Modifying Hygiene Recommendation

1. **Optional `.gitignore` tightening (future code PR, not modified in this read-only audit):** `.gitignore` line 7 currently lists `.env` without covering sibling env filenames (`.env.local`, `.env.dev`, `.env.production`). In a normal future commit, consider adding `.env.*` alongside `!.env.example` so accidental local env variants cannot be staged on either machine.

---

## 9. Audit Boundary & Non-Modification Statement

In strict compliance with Phase 3E.5.2.7.3 execution boundaries:
- No `.env` or environment file was created, modified, renamed, or deleted.
- No secret values, token fragments, or broad shell history were printed or logged.
- No Angel One authentication or external broker API request was performed.
- No supervisor, backend/frontend server, Tailscale configuration, Keychain modification, LaunchAgent action, TLS certificate action, or Home Mac connection was performed.
- No dataset, checkpoint, source file, `.gitignore`, or Git commit/push/pull/history operation was modified; only this report file (`reports/phase3e_5_2_7_3_office_mac_environment_hygiene_report.md`) was written.

---

## 10. Final Verdict

- **`.env` Presence State:** `OFFICE_ENV_ABSENT`
- **Audit Verdict:** **`OFFICE_ENVIRONMENT_CLEAN`**
