# TradeLens AI — Stage B Home & Office Mac Setup Guide

> **Scope & Safety Boundaries**
> - **Home MacBook Air M1 (`arm64`)**: Sole owner-controlled host holding `.env` Angel One credentials, runtime engine, and `remote_auth.json`.
> - **Office Mac**: Temporary, revocable, read-only assistance client over private Tailscale TLS (`https://<HOME_TAILSCALE_IP>:8080`).
> - **Order execution**: Permanently disabled (`False`).
> - **Broker authentication & Phase 3F**: Excluded from Stage B setup automation and UI (`broker-auth` is local owner CLI only when explicitly unlocked later).

---

## 1. Quickstart with `mise` & Integrated Setup Dashboard

TradeLens AI manages Stage B verification and repeatable tasks cleanly through [`mise.toml`](../../mise.toml) and the integrated **Setup & Remote Access** tab in the dashboard (`http://127.0.0.1:8080` locally on Home Mac).

### Trust & Inspect `mise` Tasks (Once per Machine)

```zsh
mise trust
mise install
mise tasks
```

`mise.toml` reuses the repository's configured Python 3.12 environment (`backend/.venv/bin/python3.12`, `TRADELENS_PYTHON_EXECUTABLE`, `/opt/homebrew/bin/python3.12`, or `/usr/local/bin/python3.12`) without installing duplicate Python toolchains or loading Angel One secrets.

> **Portability Note (Office Mac vs. Home Mac):**
> Always run Home Mac provisioning from the Home Mac's own checkout (for example, `/Users/abhisheksuman/Documents/GitHub/trading-assistant`). Never copy `.env`, TLS keys, or `deploy/launchd/*.plist` files generated on an Office Mac to the Home Mac.

---

## 2. Complete `mise` Stage B Task Reference

> **Exit-Code Contract (`Phase 3E.5.2.6.1`)**
> - **Verification gates** (`home-preflight`, `tailscale-check`, `tls-check`, `verify-device`, `remote-check`, `security-check`, `revocation-check`, `launchd-lint`, `launchd-status`): exit `0` **only** when their verification gate passes; exit `2` when `BLOCKED`, incomplete, or missing templates.
> - **Read-only inspectors & guides** (`preflight`, `status`, `register-device-guide`, `report`): exit `0` when the inspection/report succeeds while emitting `stage_verdict` (`STAGE_B_BLOCKED` / `STAGE_B_PARTIALLY_COMPLETE`) and `verification_passed=false`.
> - **Simulation (`revocation-selftest`)**: runs an isolated temporary cross-process test only (`simulated_only=true`, `proves_live_office_mac_revocation=false`); never satisfies Step 9 live verification by itself.

| Task Name | Machine / Role | Mutates Host | Exit Behavior & Expected Output |
| --- | --- | --- | --- |
| `tradelens-stage-b-preflight` | Home Mac / Inspector | Updates `.ops_state/stage_b_progress.json` (`0600`) | Exits `0` on read; emits live `stage_verdict` (`verification_passed=false` until complete). |
| `tradelens-stage-b-status` | Home Mac / Inspector | No (`--no-save`) | Exits `0` showing Steps 1–10 readiness JSON; does NOT imply Stage B verified. |
| `tradelens-stage-b-home-preflight` | Home Mac / Gate | Updates Step 1 | **Gate:** Exits `0` iff Step 1 (`0600` non-symlink `.env`, key format, safety lock) passes; otherwise exits `2`. |
| `tradelens-stage-b-tailscale-check` | Home Mac / Gate | Updates Steps 2–3 | **Gate:** Exits `0` iff Tailscale running, manual attestations recorded, and peer IPs pinned; otherwise exits `2`. |
| `tradelens-stage-b-tls-check` | Home Mac / Gate | Updates Step 4 | **Gate:** Exits `0` iff SAN TLS cert valid + `0600` key verified; otherwise exits `2`. |
| `tradelens-stage-b-register-device-guide` | Home + Office Mac / Guide | No (read-only) | Exits `0` printing Keychain keygen & `register-device` instructions without mutating state. |
| `tradelens-stage-b-register-device` | Home Mac / Mutating | Registers device in `remote_auth.json` | Requires `--device-id`, `--public-key-file` (or `--public-key-pem`), `--allowed-ip`, owner secret; **exits `2` if omitted**. |
| `tradelens-stage-b-verify-device` | Home Mac / Gate | Updates Steps 5–6 | **Gate:** Exits `0` iff owner secret, non-revoked approved device, and peer IP config alignment pass; otherwise exits `2`. |
| `tradelens-stage-b-remote-check` | Home Mac / Gate | Updates Step 7 | **Gate:** Exits `0` iff foreground remote HTTPS active and fresh post-approval/current-boot session exists; otherwise exits `2`. |
| `tradelens-stage-b-security-check` | Home Mac / Gate | Updates Step 8 | **Gate:** Exits `0` iff all offline + live Stage B boundary checks pass; otherwise exits `2`. |
| `tradelens-stage-b-revocation-selftest` | Simulated Self-Test | Updates offline flag only | Exits `0` iff isolated tempdir cross-process revocation passes (`simulated_only=true`; never proves live Office Mac revocation). |
| `tradelens-stage-b-revocation-check` | Home Mac / Gate | Updates Step 9 | **Gate:** Exits `0` iff Steps 2–8 passed, self-test passed, and live post-revocation evidence verified; otherwise exits `2`. |
| `tradelens-stage-b-render-launchd` | Home Mac / Mutation | Writes `deploy/launchd/*.plist` | Explicitly renders plist templates under `deploy/launchd/` (does NOT install into `~/Library/LaunchAgents`). |
| `tradelens-stage-b-launchd-lint` | Home Mac / Templates Gate | No | **Templates Only (`templates_only=true`):** Exits `0` iff rendered plists pass `plutil -lint` (exits `2` if missing/invalid; never proves installed/running). |
| `tradelens-stage-b-launchd-status` | Home Mac / Gate | No | **Gate:** Exits `0` iff Steps 2–9 passed and LaunchAgents are installed, bootstrapped, AND running; otherwise exits `2`. |
| `tradelens-stage-b-report` | Home Mac / Report | Writes `reports/phase3e_5_2_6_stage_b_provisioning_report.md` | Generates the secret-free Markdown report (`exit 0` only means report generated cleanly). |

---

## 3. Guided 10-Step Stage B Provisioning Sequence

### Security Warnings Before Running Commands
1. **Never pass `--owner-secret <plaintext>` in scripts or browser inputs.** Always use `--owner-secret-prompt` (hidden TTY prompt) or `--owner-secret-stdin`.
2. **Never commit or print `.env` values, TLS private keys, P-256 Keychain keys, bearer tokens, or nonces.**
3. **Never install or bootstrap LaunchAgents until Steps 2 through 9 are all `VERIFIED` and foreground HTTPS access works reliably.**

### Step 1 — Home Mac Preflight `[Home Mac]`
Restrict `.env` permissions cleanly and verify Home Mac readiness:

```zsh
chmod 600 .env
mise run tradelens-stage-b-home-preflight
```

Optional foreground loopback test on Home Mac:

```zsh
python3.12 -m backend.app.runtime.cli supervise --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080 --caffeinate
python3.12 -m backend.app.runtime.cli health-check --bind-host 127.0.0.1 --backend-port 8000 --frontend-port 8080
```

### Step 2 — Tailscale Installation & Sign-In `[Home + Office Mac]`
1. Install Tailscale on Home Mac and Office Mac and sign both machines into your private tailnet.
2. Collect both Tailscale IPv4 addresses:

```zsh
# Home Mac
tailscale status
tailscale ip -4

# Office Mac
tailscale ip -4
```

3. Verify locally and record explicit owner attestation after verifying both machines:

```zsh
mise run tradelens-stage-b-tailscale-check
python3.12 -m backend.app.runtime.cli stage-b --action confirm-step --step-id step_02_tailscale_home_office --confirm --note "Verified Home Mac and Office Mac signed into private tailnet"
```

### Step 3 — Tailscale ACL Verification `[Manual Tailscale Admin Console Action]`
1. In Tailscale Admin Console Access Controls, deny `autogroup:members` wildcard access to Home Mac ports `8000` and `8080`.
2. Permit ONLY the exact Office Mac tailnet identity/IP to reach Home Mac TCP ports `8000` and `8080`.
3. Record explicit owner ACL confirmation:

```zsh
python3.12 -m backend.app.runtime.cli stage-b --action confirm-step --step-id step_03_tailscale_acl_verification --confirm --note "Applied least-privilege Tailscale ACL permitting only exact Office Mac peer IP"
```

### Step 4 — TLS Certificate Setup (SAN + Fingerprint) `[Home Mac]`

```zsh
python3.12 -m backend.app.runtime.cli generate-tls-cert \
  --bind-host <HOME_TAILSCALE_IP> \
  --hostname <HOME_MAGICDNS_HOST> \
  --vpn-cidr 100.64.0.0/10 \
  --cert-out runtime_state/tls/tradelens-home.crt \
  --key-out runtime_state/tls/tradelens-home.key \
  --days 365

mise run tradelens-stage-b-tls-check
```

Record the printed `sha256_fingerprint` out-of-band so the Office Mac user can verify certificate identity on first HTTPS visit.

### Step 5 — Office Mac Device Key Generation & Registration `[Office Mac -> Home Mac]`

On **Office Mac**:

```zsh
python3.12 -m backend.app.runtime.cli generate-device-key \
  --device-id <OFFICE_DEVICE_ID> \
  --store-keychain \
  --public-key-out /tmp/<OFFICE_DEVICE_ID>.pub.pem
```

Copy only `/tmp/<OFFICE_DEVICE_ID>.pub.pem` (public key only) to Home Mac.

On **Home Mac** (preview instructions via `mise run tradelens-stage-b-register-device-guide`):

```zsh
python3.12 -m backend.app.runtime.cli init-owner --remote-access --owner-secret-prompt

python3.12 -m backend.app.runtime.cli register-device \
  --device-id <OFFICE_DEVICE_ID> \
  --device-name <OFFICE_MAC_LABEL> \
  --public-key-file /tmp/<OFFICE_DEVICE_ID>.pub.pem \
  --allowed-ip <OFFICE_TAILSCALE_IP> \
  --pending-only \
  --remote-access \
  --owner-secret-prompt
```

### Step 6 — Owner Approval & Exact IP Pin Verification `[Home Mac]`

```zsh
python3.12 -m backend.app.runtime.cli approve-device \
  --device-id <OFFICE_DEVICE_ID> \
  --allowed-ip <OFFICE_TAILSCALE_IP> \
  --remote-access \
  --owner-secret-prompt

python3.12 -m backend.app.runtime.cli list-devices --remote-access
mise run tradelens-stage-b-verify-device
```

### Step 7 — Remote HTTPS Challenge-Response Verification `[Home Mac + Office Mac]`

On **Home Mac** (foreground first!):

```zsh
python3.12 -m backend.app.runtime.cli supervise \
  --bind-host <HOME_TAILSCALE_IP> \
  --backend-port 8000 \
  --frontend-port 8080 \
  --vpn-cidr 100.64.0.0/10 \
  --allowed-peer-ip <OFFICE_TAILSCALE_IP> \
  --allowed-host <HOME_TAILSCALE_IP> \
  --allowed-host <HOME_MAGICDNS_HOST> \
  --allowed-origin https://<HOME_TAILSCALE_IP>:8080 \
  --tls-cert runtime_state/tls/tradelens-home.crt \
  --tls-key runtime_state/tls/tradelens-home.key \
  --remote-access \
  --caffeinate
```

On **Office Mac** (sign challenge from Remote Auth Modal using Path A Keychain CLI):

```zsh
python3.12 -m backend.app.runtime.cli sign-challenge --device-id <OFFICE_DEVICE_ID> --challenge-stdin
```

Then run verification on Home Mac:

```zsh
mise run tradelens-stage-b-remote-check
```

### Step 8 — Security Boundary & Negative Verification `[Home Mac]`

```zsh
mise run tradelens-stage-b-security-check
```

Verify unauthenticated curl / wrong peer IP requests return `401` / `403`, remote calls to `/api/setup/run-task` return `403 HOME_OWNER_ONLY_OPERATION`, and `.env` / secrets never appear in responses or logs.

### Step 9 — Revocation & Re-Enrolment Drill `[Home Mac]`

```zsh
python3.12 -m backend.app.runtime.cli revoke-device \
  --device-id <OFFICE_DEVICE_ID> \
  --reason STAGE_B_REVOCATION_DRILL \
  --remote-access \
  --owner-secret-prompt
```

Confirm active Office Mac bearer token immediately fails with `403` (`DEVICE_REVOKED` / `SESSION_REVOKED`), and unapproved re-enrolment is blocked. You can verify the offline cross-process mechanism at any time via `mise run tradelens-stage-b-revocation-selftest` (`simulated_only=true`), then re-approve with `--allow-re-enroll`, re-authenticate live, and record the live drill:

```zsh
python3.12 -m backend.app.runtime.cli stage-b --action revocation-check --record-live-drill --confirm --note "Verified live immediate revocation and explicit re-enrolment"
mise run tradelens-stage-b-revocation-check
```

### Step 10 — LaunchAgent Installation (Strictly Gated Until Steps 2–9 Verified) `[Home Mac]`

Render and lint fresh LaunchAgent templates **directly on the Home Mac** so `WorkingDirectory`, wrapper paths, `PYTHONPATH`, and Python executable match the Home Mac repository root (for example `/Users/abhisheksuman/Documents/GitHub/trading-assistant`) and active Python interpreter. Never copy Office-Mac-rendered plists, and never treat rendered template files as installed or running services:

```zsh
mise run tradelens-stage-b-render-launchd
mise run tradelens-stage-b-launchd-lint
```

Only **after Steps 2 through 9 all show `VERIFIED`**:

```zsh
cp deploy/launchd/com.tradelens.home-engine.plist ~/Library/LaunchAgents/
cp deploy/launchd/com.tradelens.frontend-dashboard.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradelens.home-engine.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradelens.frontend-dashboard.plist
mise run tradelens-stage-b-launchd-status
```

---

## 4. Operational Recovery Playbook

<a id="recovery-home-mac-restarted"></a>
### Home Mac Restarted
- **Cause**: macOS reboot cleared in-memory Angel One session tokens and volatile market caches.
- **Recovery**:
  1. Run `mise run tradelens-stage-b-status` and `python3.12 -m backend.app.runtime.cli status`.
  2. Start foreground supervisor or verify LaunchAgent status; run `python3.12 -m backend.app.runtime.cli health-check`.
  3. Broker authentication always remains `EXPLICIT_AUTH_REQUIRED` until manually unlocked later.

<a id="recovery-sleep-network-drop"></a>
### Home Mac Slept or Network Dropped
- **Cause**: Power outage, lid closed on battery without clamshell AC, or Wi-Fi drop.
- **Recovery**:
  1. Restore AC power/Wi-Fi and run supervisor with `--caffeinate`.
  2. Check `python3.12 -m backend.app.runtime.cli status` and `mise run tradelens-stage-b-remote-check`.

<a id="recovery-tailscale-disconnected"></a>
### Tailscale Disconnected
- **Cause**: Tailscale daemon stopped or node session expired.
- **Recovery**:
  1. Run `tailscale status` and `tailscale ip -4` on both Home and Office Macs.
  2. Re-authenticate Tailscale if needed and run `mise run tradelens-stage-b-tailscale-check`.

<a id="recovery-office-ip-changed"></a>
### Office Mac Tailscale IP Changed
- **Cause**: Office Mac assigned a new `100.x.y.z` tailnet address.
- **Recovery**:
  1. Run `tailscale ip -4` on Office Mac.
  2. On Home Mac run:
     `python3.12 -m backend.app.runtime.cli update-device-ip --device-id <OFFICE_DEVICE_ID> --allowed-ip <NEW_OFFICE_TAILSCALE_IP> --remote-access --owner-secret-prompt`
  3. Update Tailscale Admin Console ACL and `--allowed-peer-ip`, then verify via `mise run tradelens-stage-b-verify-device`.

<a id="recovery-tls-expired"></a>
### TLS Certificate Expired or SAN Mismatch
- **Cause**: Certificate passed `notAfter` or Home Mac Tailscale IP/MagicDNS name changed.
- **Recovery**:
  1. Re-run `python3.12 -m backend.app.runtime.cli generate-tls-cert ...`.
  2. Run `mise run tradelens-stage-b-tls-check` and restart Home Mac runtime cleanly.

<a id="recovery-device-revoked"></a>
### Office Mac Revoked or Permanently Retired
- **Cause**: Emergency owner revocation, compromised client, or permanently retired Office Mac workstation.
- **Recovery**:
  1. Confirm revoked state via `python3.12 -m backend.app.runtime.cli list-devices --remote-access` (existing tokens return `403`).
  2. On the Office Mac run `python3.12 -m backend.app.runtime.cli delete-device-key --device-id <OFFICE_DEVICE_ID>`.
  3. For permanent Office Mac removal, run `python3.12 -m backend.app.runtime.cli remove-device --device-id <OFFICE_DEVICE_ID> --reason PERMANENTLY_RETIRED --remote-access --owner-secret-prompt` on Home Mac, remove its Tailscale peer IP from Tailscale ACLs and `--allowed-peer-ip`, and return Home Mac to loopback (`--bind-host 127.0.0.1`) if no other remote workstation remains.

<a id="recovery-re-enrollment"></a>
### Re-Enrolling Office Mac (or Browser Key Lost on Reload)
- **Cause**: Ephemeral browser WebCrypto key lost on reload or replacement Keychain key needed.
- **Recovery**:
  1. Generate a fresh key on Office Mac (`generate-device-key` or Browser RAM key button).
  2. On Home Mac run `register-device --allow-re-enroll --remote-access --owner-secret-prompt` followed by `approve-device`.

<a id="recovery-runtime-stopped"></a>
### Home Runtime Stopped Unexpectedly
- **Cause**: Crash, stale PID file, or restart backoff exhaustion.
- **Recovery**:
  1. Inspect `python3.12 -m backend.app.runtime.cli status` and `runtime_logs/`.
  2. Run `python3.12 -m backend.app.runtime.cli restart` (refuses silent security downgrades).

<a id="recovery-launchagent-failed"></a>
### LaunchAgent Failed
- **Cause**: Missing TLS/HMAC/.env permissions or invalid plist.
- **Recovery**:
  1. Bootout immediately: `launchctl bootout gui/$(id -u)/com.tradelens.home-engine`
  2. Run `mise run tradelens-stage-b-launchd-lint` and `mise run tradelens-stage-b-home-preflight`.

<a id="recovery-owner-shutdown-resume"></a>
### Owner Shutdown & Resume
- **Cause**: Explicit `owner-shutdown` created `runtime_state/owner_shutdown.flag` (keeps auto-restart disabled).
- **Recovery**:
  1. Run `python3.12 -m backend.app.runtime.cli resume` to clear `owner_shutdown.flag`.
  2. Run `python3.12 -m backend.app.runtime.cli restart` to resume normal operation.

