# Phase 3E.5.2.6.1 — Setup Dashboard Security, Mise Task Semantics & Live-State Integrity Audit Report

- **Date (UTC):** 2026-11-11
- **Audit Target:** TradeLens AI Setup & Remote Access Dashboard, Stage B Coordinator, CLI, `mise.toml` Task Automation & Recovery Playbooks
- **Host Inspected:** macOS (`Darwin 25.0.0`, `arm64`), Python `3.12.13`, `mise 2026.2.19`, Node `v22.22.3`
- **Dashboard & Task Implementation Audit Verdict:** `SETUP_DASHBOARD_SECURITY_VERIFIED`
- **Live Stage B Provisioning Verdict:** `STAGE_B_BLOCKED`
- **Hard Operational Guarantees Confirmed:**
  - Order execution remains **PERMANENTLY DISABLED** (`order_execution_enabled: false`)
  - Angel One broker authentication was **NOT EXECUTED** (`broker_auth_excluded: true`)
  - Phase 3F was **NOT STARTED** (`phase_3f_excluded: true`)
  - Tailscale, `.env`, macOS Keychain, TLS certificates, `remote_auth.json`, and `~/Library/LaunchAgents` were **NOT MUTATED**

---

## 1. Executive Summary

An independent source, HTTP-boundary, CLI/Mise task, persistence, frontend, and live probe audit (Audits A–F) was executed against the Phase 3E.5.2.6 Setup & Remote Access Dashboard and Stage B control plane. Targeted security hardening was applied across backend handlers, setup coordination, peer-IP session invalidation, `mise.toml`, `frontend/index.html`, operational docs, and automated regression tests without weakening any existing Phase 3E.5.2.5/A.1/3E.5.2.6 security gates.

Every audit finding has been resolved in code and verified against an expanded automated test suite (`474` passing backend tests including `16` Stage B setup/security/integrity tests), JavaScript syntax checking (`node --check`), and safe non-mutating `mise` / CLI dry runs. Live Stage B provisioning on this machine remains `STAGE_B_BLOCKED` because live setup steps (`.env` `0600` hardening, Tailscale installation, Home/Office peer pinning, SAN TLS generation, remote challenge-response, live revocation drill, and LaunchAgent activation) have intentionally not been performed during this offline audit.

---

## 2. Audit A–F Findings & Hardening Matrix

| Audit | Findings Identified | Targeted Fixes Implemented | Files Touched |
| --- | --- | --- | --- |
| **Audit A: Remote Setup-State Disclosure** | 1. Remote `/api/setup/state` exposed local paths, `.env` inspection metadata, other devices, public key fingerprints, peer IP allowlists, `supervisor_pid`, `command_catalog`, and `recovery_playbooks`.<br>2. Initial remote DTO hit `KeyError: 'expired'` when TLS cert did not exist yet (`_probe_tls` emits `"unexpired"`). | 1. `GET /api/setup/state` now forwards `remote_device_id=dec.device_id` to `StageBSetupCoordinator.get_setup_state`.<br>2. `_project_remote_setup_state` strips `.env`, local paths, `devices` list, fingerprints, peer IPs, `supervisor_pid`, internal failure reasons, owner notes, `command_catalog`, `recovery_playbooks`, and sensitive keys via `_strip_sensitive_keys` (`SENSITIVE_REMOTE_DROP_KEYS`), surfacing only `self_device` + coarse status.<br>3. Fixed remote TLS projection to use `.get("configured", False)`, `.get("valid", False)`, and `.get("unexpired", False)`. | `backend/app/main.py`, `backend/app/runtime/stage_b_setup.py`, `frontend/index.html` |
| **Audit B: HTTP Execution Boundary & Confirmation Gating** | 1. `POST /api/setup/run-task` needed explicit HTTP rejection of arbitrary `extra_args`.<br>2. `python_executable` needed validation against shell metacharacters or non-Python binaries.<br>3. Manual Step 2/3 confirmations needed live prerequisite enforcement. | 1. `main.py` rejects non-empty `extra_args` over HTTP (`HTTP_EXTRA_ARGS_NOT_ALLOWED` -> HTTP `400`).<br>2. `run_allowlisted_task` enforces `called_from_http`, per-task `allowed_extra_keys`, and `_validate_python_executable`.<br>3. `confirm_manual_step` refuses automated steps (`MANUAL_CONFIRMATION_CANNOT_BYPASS_VERIFICATION`) and blocks Step 2/3 unless Step 1 `.env` and running Tailscale hold (`PREREQUISITE_NOT_MET`). | `backend/app/main.py`, `backend/app/runtime/stage_b_setup.py` |
| **Audit C: CLI & `mise.toml` Exit Codes & Honest Semantics** | 1. Missing-arg `register-device`, unverified gates (`security-check`, `revocation-check`, `home-preflight`, etc.), and simulated revocation self-test risked ambiguous exit `0` semantics.<br>2. Registration guide and mutating device registration needed separate tasks; simulated vs live revocation needed explicit task separation. | 1. Added `tradelens-stage-b-register-device-guide` (exits `0`, read-only) while `tradelens-stage-b-register-device` exits `2` when required flags are omitted.<br>2. Split `tradelens-stage-b-revocation-selftest` (`simulated_only=true`, `proves_live_office_mac_revocation=false`, `verification_passed=false`) from live `tradelens-stage-b-revocation-check` gate.<br>3. All unverified verification gates exit `2` even under `--no-save`. Consistent `exit_code` emitted across `cmd_stage_b` JSON outputs. `tradelens-stage-b-launchd-lint` explicitly emits `templates_only=true`, `proves_installed_or_running=false`. | `backend/app/runtime/stage_b_setup.py`, `backend/app/runtime/cli.py`, `mise.toml`, `docs/operations/STAGE_B_SETUP.md` |
| **Audit D: Progress Integrity, Invalidation & Tamper Resistance** | 1. Pre-existing `last_auth_at` survived Office IP changes and masked fresh re-auth checks.<br>2. Revoked Device A + unauthenticated Device B could falsely satisfy Step 7.<br>3. Missing HMAC forwarding in `_get_remote_manager` broke reading HMAC-signed `remote_auth.json`.<br>4. Needed symlink blocking, `flock` concurrency protection, corruption recovery, Home Mac reboot invalidation, peer IP drift checks, and Step 9 tamper rejection. | 1. `approve_device` and `update_device_allowed_ips` clear `dev.last_auth_at = None` on IP changes.<br>2. Fixed revoked-device masking in `_evaluate_steps`; detects `peer_ip_config_drift` vs `runtime_config.json`.<br>3. `_get_remote_manager` forwards `TRADELENS_STATE_HMAC_SECRET` from `self.environ`.<br>4. Added `_assert_no_symlinks_on_progress_store`, `_progress_file_lock` (`RLock` + `fcntl.flock`), `_load_progress_store` (`CORRUPTION_RECOVERED`), `_probe_boot_session` (`sysctl -n kern.boottime` -> `STALE_PRIOR_BOOT`), and `_has_live_revocation_evidence` rejecting forged `live_drill_verified` without post-revocation `remote_auth.json` evidence. | `backend/app/runtime/security.py`, `backend/app/runtime/stage_b_setup.py` |
| **Audit E: Frontend Dashboard UI/UX, Accessibility & CSP** | 1. Task runner treated HTTP `200` with `passed === false`, `simulated_only`, or `templates_only` ambiguously.<br>2. Stale/failed setup banners lacked explicit assertive alert semantics and non-color indicators; remote view needed strict UI control suppression. | 1. `runSetupBackendTask` inspects `tr.passed`, `tr.simulated_only`, and `tr.templates_only` and triggers distinct error/warning states.<br>2. Added `#setupFreshnessBadge`, `#setupActionBanner`, `#setupRecoverySection`; `setSetupBanner` switches `role="alert"` / `aria-live="assertive"` for errors and `aria-live="polite"` + icon-plus-text prefixes (`⚠`, `⛔`, `✔`).<br>3. Remote view renders `"Minimized Remote Workstation Projection (Read-Only)"`, shows `state.self_device`, hides/disables owner controls, and suppresses recovery CLI cards. | `frontend/index.html` |
| **Audit F: LaunchAgent Running State & Permanent Office Retirement** | 1. `_probe_launchd` needed to distinguish `bootstrapped_not_running` (`rc=0`, `state = not running`) from genuinely `running` (`state = running`, `pid`).<br>2. Permanent Office Mac retirement guidance needed explicit ACL / `--allowed-peer-ip` cleanup and `127.0.0.1` rollback steps. | 1. `_probe_launchd` parses `launchctl print` `state` and `pid`, exposing `launchd_state`, `pid`, `all_installed_and_loaded`, and `all_running`; Step 10 requires `all_running`.<br>2. Updated `rec_device_revoked` playbook and `#recovery-device-revoked` with `remove-device --device-id <OFFICE_DEVICE_ID> --reason PERMANENTLY_RETIRED --remote-access --owner-secret-prompt`, Tailscale ACL / peer-IP cleanup, and return to `127.0.0.1`. | `backend/app/runtime/stage_b_setup.py`, `docs/operations/STAGE_B_SETUP.md` |

---

## 3. `mise.toml` & CLI Exit-Code Matrix (All 16 Tasks)

Every `mise` task delegates to `backend.app.runtime.cli stage-b --action ...` and was cross-checked via `validate_command_catalog_against_cli(build_parser(), COMMAND_CATALOG)`:

| # | `mise` Task | CLI `--action` | Category | Exit `0` Semantics | Exit `2` Condition |
| --- | --- | --- | --- | --- | --- |
| 1 | `tradelens-stage-b-preflight` | `preflight` | Read-Only Inspector (saves progress) | Preflight inspected cleanly (check `stage_verdict`) | Invalid store/symlink error |
| 2 | `tradelens-stage-b-status` | `status` | Read-Only Inspector (`--no-save`) | Status rendered cleanly (`exit_0_means_stage_b_verified: false`) | Invalid store/symlink error |
| 3 | `tradelens-stage-b-home-preflight` | `home-preflight` | Step 1 Gate | Step 1 `.env` (`0600`) & host checks `VERIFIED` | Step 1 unverified / blocked |
| 4 | `tradelens-stage-b-tailscale-check` | `tailscale-check` | Steps 2–3 Gate | Tailscale running & manual Steps 2–3 confirmed | Tailscale down / Steps 2–3 unverified |
| 5 | `tradelens-stage-b-tls-check` | `tls-check` | Step 4 Gate | SAN TLS cert + `0600` key valid | TLS cert missing / invalid / expired |
| 6 | `tradelens-stage-b-register-device-guide` | `register-device-guide` | Read-Only Guide | Prints Office keygen & Home registration commands (`registered_any_device: false`) | Store / argparse error |
| 7 | `tradelens-stage-b-register-device` | `register-device` | Step 5 Mutating CLI | Explicit registration succeeded with valid flags & owner auth | Required flags omitted (`MISSING_REGISTRATION_FLAGS`) |
| 8 | `tradelens-stage-b-verify-device` | `verify-device` | Steps 5–6 Gate | Approved active device with pinned IP & no config drift | Device unapproved / revoked / unpinned / drifted |
| 9 | `tradelens-stage-b-remote-check` | `remote-check` | Step 7 Gate | Remote HTTPS bind active & fresh challenge-response auth verified | Remote listener inactive / no valid session |
| 10 | `tradelens-stage-b-security-check` | `security-check` | Step 8 Gate | All Stage B security boundary checks pass (`all_checks_passed: true`) | Any boundary check fails or pending |
| 11 | `tradelens-stage-b-revocation-selftest` | `revocation-selftest` | Simulated Self-Test | Isolated temp-dir revocation logic passes (`simulated_only: true`, `verification_passed: false`) | Isolated self-test fails |
| 12 | `tradelens-stage-b-revocation-check` | `revocation-check` | Step 9 Live Gate | Steps 2–8 valid, self-test passed, & live post-revocation evidence verified | Live revocation unverified / forged / invalidated |
| 13 | `tradelens-stage-b-render-launchd` | `render-launchd` | Step 10 Prep (`--confirm`) | Plist templates rendered to `deploy/launchd/` (`templates_only: true`, not installed) | Unconfirmed / invalid `--python-exe` |
| 14 | `tradelens-stage-b-launchd-lint` | `launchd-lint` | Templates-Only Gate | Rendered `deploy/launchd/*.plist` pass `plutil -lint` (`proves_installed_or_running: false`) | Templates missing or invalid plist |
| 15 | `tradelens-stage-b-launchd-status` | `launchd-status` | Step 10 Live Gate | Steps 2–9 `VERIFIED` and both LaunchAgents installed, bootstrapped, & running (`pid != null`) | LaunchAgents missing, stopped, or Steps 2–9 incomplete |
| 16 | `tradelens-stage-b-report` | `report` | Report Generator | Secret-free markdown report generated (check `stage_verdict`) | Report write / store error |

---

## 4. Files Modified During Phase 3E.5.2.6.1

- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/backend/app/runtime/security.py`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/backend/app/main.py`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/backend/app/runtime/stage_b_setup.py`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/backend/app/runtime/cli.py`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/mise.toml`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/frontend/index.html`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/docs/operations/STAGE_B_SETUP.md`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/backend/tests/test_stage_b_setup_and_mise.py`
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/reports/phase3e_5_2_6_stage_b_provisioning_report.md` (regenerated via `--action report --no-save`)
- `/Users/abhisheksuman/Documents/GitHub/tradelens-ai/reports/phase3e_5_2_6_1_setup_dashboard_integrity_audit.md` (this report)

---

## 5. Automated Verification Results (Observed After Final Code Edits)

1. **Targeted Stage B Setup, Security & Mise Test Suite:**
   - Command: `PYTHONPATH=.:backend backend/.venv/bin/python -m unittest backend/tests/test_stage_b_setup_and_mise.py -v`
   - Result: **Ran 16 tests in 7.296s — `OK`** (0 failures, 0 errors, 0 skipped).
   - Covered all `test_01`..`test_16` scenarios: argparse-catalog parity, `0600` zero-secret progress persistence, tampered state rejection, Step 10 prerequisite lock, remote Audit A redaction & `403` isolation, HTTP `extra_args` `400` rejection, unpinned/unknown session rejection, all 16 `mise.toml` tasks & doc anchors, non-saving CLI `0` vs `2` gate checks, UI accessibility/banner/CSP checks, HTTP wire loopback roundtrip, invalid `python_executable` rejection, Step 2/3 manual confirmation gating, revoked device not masking unauthenticated active device, IP change clearing `last_auth_at` & config drift invalidation, simulated reboot (`STALE_PRIOR_BOOT`) invalidating Steps 7–10, corruption recovery (`CORRUPTION_RECOVERED`), symlink blocking, 8-thread `flock` concurrency integrity, `launchctl print` `bootstrapped_not_running` vs `running` distinction, tampered `live_drill_verified` rejection, and permanent retirement recovery checks.
2. **Full Backend Regression & Security Unittest Suite:**
   - Command: `PYTHONPATH=.:backend backend/.venv/bin/python -m unittest discover -s backend/tests -v`
   - Result: **Ran 474 tests in 79.196s — `OK`** (0 failures, 0 errors, 0 skipped).
3. **Frontend JavaScript Verification:**
   - Command: Extracted `<script>` block (`144,284` chars) from `frontend/index.html` and ran `/Users/abhisheksuman/.nvm/versions/node/v22.22.3/bin/node --check /tmp/tradelens_fe_check.js` (Node `v22.22.3`).
   - Result: **Exit code `0`** (`OK`). Note: `frontend/` contains no `package.json`, bundler (Vite), or ESLint config; verification covers embedded `<script>` syntax checking and Python UI/HTML/CSP regex assertions in `test_07` and `test_08`.

---

## 6. Safe Non-Mutating Live Environment & Mise Verification Results

Executed inside standard sandbox with isolated temp directories where applicable; zero persistent state on the machine was altered:

| Check Executed | Exit Code | Key Observed Fields | Verdict / Interpretation |
| --- | --- | --- | --- |
| `mise tasks` | `0` | 16 `tradelens-stage-b-*` tasks listed | Task inventory matches spec & docs |
| `mise run tradelens-stage-b-status` | `0` | `stage_verdict="STAGE_B_BLOCKED"`, `verification_passed=false`, `exit_0_means_stage_b_verified=false`, `step_01` failure `.env` mode `0o644` | Honest read-only status inspection |
| `mise run tradelens-stage-b-register-device-guide` | `0` | `status="GUIDE_ONLY"`, `registered_any_device=false`, `verification_passed=false` | Safe non-mutating registration guide |
| `mise run tradelens-stage-b-revocation-selftest --no-save` | `0` | `simulated_only=true`, `proves_live_office_mac_revocation=false`, `selftest_passed=true`, `verification_passed=false` | Isolated mechanism self-test passed without spoofing live Step 9 |
| CLI `--action home-preflight --no-save` | `2` | `verification_passed=false`, `stage_verdict="STAGE_B_BLOCKED"` (`.env` is `0o644`) | Gate accurately blocked non-`0600` `.env` |
| CLI `--action tailscale-check --no-save` | `2` | `verification_passed=false` (Tailscale not installed/running) | Gate accurately failed closed |
| CLI `--action tls-check --no-save` | `2` | `verification_passed=false` (TLS not configured) | Gate accurately failed closed |
| CLI `--action register-device --no-save` | `2` | `status="MISSING_REGISTRATION_FLAGS"`, `registered_any_device=false` | Refused argless registration |
| CLI `--action verify-device --no-save` | `2` | `verification_passed=false` (0 devices) | Gate accurately failed closed |
| CLI `--action remote-check --no-save` | `2` | `verification_passed=false` | Gate accurately failed closed |
| CLI `--action security-check --no-save` | `2` | `verification_passed=false` (4 core offline checks `true`; `.env`, peer pin, transport `false`) | Mixed core-offline pass no longer exits `0` |
| CLI `--action revocation-check --no-save` | `2` | `verification_passed=false` | Live Step 9 gate accurately blocked |
| CLI `--action launchd-lint --no-save` | `0` | `templates_only=true`, `proves_installed_or_running=false`, `verification_passed=true` (`plutil -lint OK` on `deploy/launchd/*.plist`) | Correctly distinguishes rendered templates from active services |
| CLI `--action launchd-status --no-save` | `2` | `any_installed=false`, `all_running=false`, `verification_passed=false` | LaunchAgents not installed or running |
| CLI `--action report --no-save` | `0` | Regenerated `reports/phase3e_5_2_6_stage_b_provisioning_report.md` (`STAGE_B_BLOCKED`, 16 tasks documented) | Honest status report refreshed |

---

## 7. Open Operational Limitations & Next Authorized Steps

- **Live Provisioning Remains `STAGE_B_BLOCKED` Until Home Mac Execution:** None of the live infrastructure steps are active on this workstation (`.env` is still `0o644`, Tailscale is not installed, no SAN TLS cert or owner secret is configured, no devices are registered, and LaunchAgents are not bootstrapped).
- **Pre-Existing `deploy/launchd/*.plist` Files:** Two template plists (`com.tradelens.home-engine.plist` and `com.tradelens.frontend-dashboard.plist`) exist from Phase 3E.5.2.6 in `deploy/launchd/`, causing `launchd-lint` to report `templates_only=true` while `launchd-status` rightly exits `2` (`any_installed=false`, `all_running=false`). They should be re-rendered with actual Tailscale bind host, SAN TLS paths, and `--allowed-peer-ip` only during approved live provisioning after Steps 2–9 pass.
- **Next Authorized Steps On Home Mac (Once Approved):**
  1. Harden `.env` on Home Mac (`chmod 600 .env`) and verify with `mise run tradelens-stage-b-home-preflight` (expect exit `0`).
  2. Install & sign into Tailscale on Home Mac and Office Mac, apply least-privilege ACLs + macOS Firewall stealth mode, confirm Steps 2–3 cleanly, and run `mise run tradelens-stage-b-tailscale-check`.
  3. Generate SAN TLS certificate (`generate-tls-cert`) and run `mise run tradelens-stage-b-tls-check`.
  4. Initialize owner secret (`init-owner`), generate Keychain key on Office Mac (`generate-device-key`), register & approve with exact Office Mac Tailscale IP, and verify with `mise run tradelens-stage-b-verify-device`.
  5. Start Home Mac runtime over TLS bound to Home Tailscale IP, verify remote HTTPS challenge-response (`tradelens-stage-b-remote-check`), boundary checks (`tradelens-stage-b-security-check`), and live revocation + re-enrolment drill (`tradelens-stage-b-revocation-check`).
  6. Only after Steps 2–9 are all `VERIFIED`, render final LaunchAgent plists (`tradelens-stage-b-render-launchd`), bootstrap into `~/Library/LaunchAgents`, and verify with `mise run tradelens-stage-b-launchd-status`.

