# TradeLens AI — Home Mac Command Hub

Run all commands from `/Users/abhisheksuman/Documents/GitHub/trading-assistant` (either via `mise run <task>` or directly with `backend/.venv/bin/python3.12`).

> **Safety Locks Active:** `order_execution_enabled = False`, automatic Angel One login disabled (`BROKER_AUTH_EXPLICIT_AUTH_REQUIRED`), and local listeners bind strictly to `127.0.0.1`.

---

## 1. Run & Test in Browser (Everyday Local Hub)

| Action | `mise` Command (Recommended) | Direct CLI Equivalent |
| --- | --- | --- |
| **List all tasks** | `mise tasks` | — |
| **Start local server (strict mode)** | `mise run tradelens-start` | `PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli supervise --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080` |
| **Start with sample UI data (offline)** | `mise run tradelens-start-sim` | `TRADELENS_ALLOW_SIMULATED_FALLBACK=1 PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli supervise --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080` |
| **Open dashboard in browser** | `mise run tradelens-open` | `open http://127.0.0.1:8080` |
| **Probe local HTTP health** | `mise run tradelens-health` | `PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli health-check --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080` |
| **Inspect supervisor & power status** | `mise run tradelens-status` | `PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli status` |
| **Restart preserving security config** | `mise run tradelens-restart` | `PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli restart` |
| **Stop cleanly & release ports** | `mise run tradelens-stop` | `PYTHONPATH=.:./backend backend/.venv/bin/python3.12 -m backend.app.runtime.cli stop` |

### Quick Browser Flow

1. Start in Terminal Tab 1:
   ```zsh
   mise run tradelens-start-sim
   ```
2. Open browser & check health in Terminal Tab 2:
   ```zsh
   mise run tradelens-health
   mise run tradelens-open
   ```
3. Inspect in Chrome/Safari (`http://127.0.0.1:8080`):
   - **Setup & Remote Access** tab (`#tab-setup`): Step 1 shows `VERIFIED`; Steps 2–10 remain `REQUIRES_OWNER_ACTION`/`BLOCKED` until Tailscale + Stage 2+ provisioning.
   - **DevTools -> Network/Console**: verify `GET /`, `GET http://127.0.0.1:8000/api/health`, and `GET http://127.0.0.1:8000/api/setup/state` return HTTP `200`, with CSP + `X-Frame-Options: DENY`.
4. Stop when finished (`Ctrl+C` in Tab 1 or run `mise run tradelens-stop`).

---

## 2. Automated Verification & Tests

| Check | `mise` Command | Scope |
| --- | --- | --- |
| **Fast engine smoke check** | `mise run tradelens-test-smoke` | Core Black-Scholes, sample scanner, veto gate (`backend/test_api.py`) |
| **Full backend regression** | `mise run tradelens-test` | Full `backend/tests/test_*.py` suite (489 tests) |

---

## 3. Stage B Setup, Security Gates & Remote Readiness (`mise`)

| Task | Purpose |
| --- | --- |
| `mise run tradelens-stage-b-status` | Read-only 10-step Stage B status overview (`--no-save`) |
| `mise run tradelens-stage-b-home-preflight` | Step 1 gate: host, Python 3.12, `.env` `0600`, Angel One key format |
| `mise run tradelens-stage-b-tailscale-check` | Steps 2–3 gate: Tailscale tunnel & ACL confirmation |
| `mise run tradelens-stage-b-tls-check` | Step 4 gate: Home Mac SAN TLS certificate & `0600` key check |
| `mise run tradelens-stage-b-register-device-guide` | Read-only instructions for Office Mac Keychain keygen & registration |
| `mise run tradelens-stage-b-verify-device` | Steps 5–6 gate: Office Mac public key + pinned peer IP check |
| `mise run tradelens-stage-b-remote-check` | Step 7 gate: remote HTTPS challenge-response check |
| `mise run tradelens-stage-b-security-check` | Step 8 gate: offline + live Stage B boundary checks |
| `mise run tradelens-stage-b-revocation-selftest` | Isolated simulation of token revocation (does not replace live Step 9) |
| `mise run tradelens-stage-b-revocation-check` | Step 9 gate: live revocation & re-enrollment verification |
| `mise run tradelens-stage-b-render-launchd` | Render `deploy/launchd/*.plist` templates (non-installing) |
| `mise run tradelens-stage-b-launchd-lint` | Validate rendered `deploy/launchd/*.plist` with `plutil -lint` |
| `mise run tradelens-stage-b-launchd-status` | Step 10 gate: LaunchAgent install/running status (locked until Steps 2–9 pass) |
| `mise run tradelens-stage-b-report` | Generate `reports/phase3e_5_2_6_stage_b_provisioning_report.md` |

See [`docs/operations/STAGE_B_SETUP.md`](operations/STAGE_B_SETUP.md) for the full remote provisioning runbook.

