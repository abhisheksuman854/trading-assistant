#!/bin/sh
# TradeLens AI - Staged LaunchAgent Runtime Wrapper (macOS launchd)
# Loads repository-root .env securely using syntax-aligned non-eval parsing,
# enforces owner-only 0600/0400 permissions & non-symlink checks in remote mode,
# refuses to start in remote mode on malformed .env or missing/short HMAC secret,
# and execs the target runtime command without shell tracing or exposing secrets.

set -eu
{ set +x; } 2>/dev/null || true

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="${TRADELENS_ENV_FILE:-$REPO_ROOT/.env}"

trim_hws() {
    _s="$1"
    while :; do
        case "$_s" in
            " "*|"	"*) _s="${_s#?}" ;;
            *) break ;;
        esac
    done
    while :; do
        case "$_s" in
            *" "|*"	") _s="${_s%?}" ;;
            *) break ;;
        esac
    done
    REPLY_TRIM="$_s"
}

is_non_loopback() {
    case "$1" in
        ""|127.0.0.1|::1|localhost|LOCALHOST) return 1 ;;
        *) return 0 ;;
    esac
}

is_remote=0
if [ "${TRADELENS_REMOTE_ACCESS_ENABLED:-0}" = "1" ] || [ -n "${TRADELENS_ALLOWED_VPN_CIDRS:-}" ] || [ -n "${TRADELENS_ALLOWED_PEER_IPS:-}" ]; then
    is_remote=1
fi
if [ -n "${TRADELENS_BIND_HOST:-}" ] && is_non_loopback "${TRADELENS_BIND_HOST:-}"; then
    is_remote=1
fi

prev_arg=""
for arg in "$@"; do
    case "$arg" in
        --remote-access|--vpn-cidr|--vpn-cidr=*|--allowed-peer-ip|--allowed-peer-ip=*)
            is_remote=1
            ;;
        --bind-host=*)
            bh_val="${arg#--bind-host=}"
            if is_non_loopback "$bh_val"; then
                is_remote=1
            fi
            ;;
        *)
            if [ "$prev_arg" = "--bind-host" ] && is_non_loopback "$arg"; then
                is_remote=1
            fi
            ;;
    esac
    prev_arg="$arg"
done

check_env_perms() {
    f="$1"
    if [ -L "$f" ]; then
        printf 'ERROR: Refusing symlinked env file: %s\n' "$f" >&2
        exit 78
    fi
    if [ ! -f "$f" ]; then
        return 1
    fi
    perms=$(stat -f '%Lp' "$f" 2>/dev/null || printf '%s' "unknown")
    case "$perms" in
        600|400) ;;
        *)
            if [ "$is_remote" = "1" ]; then
                printf 'ERROR: Refusing launch in remote mode with insecure permissions (%s) on %s (expected 600).\n' "$perms" "$f" >&2
                exit 78
            fi
            ;;
    esac
    return 0
}

load_dotenv_strict() {
    f="$1"
    check_env_perms "$f" || return 1

    bom=$(printf '\357\273\277')
    cr=$(printf '\r')
    line_no=0
    exported_from_dotenv=" "

    while IFS= read -r raw_line || [ -n "$raw_line" ]; do
        line_no=$((line_no + 1))
        line="$raw_line"
        if [ "$line_no" -eq 1 ]; then
            case "$line" in
                "${bom}"*) line="${line#"$bom"}" ;;
            esac
        fi
        case "$line" in
            *"$cr") line="${line%"$cr"}" ;;
        esac

        trim_hws "$line"
        line="$REPLY_TRIM"
        case "$line" in
            ""|\#*) continue ;;
        esac

        case "$line" in
            "export "*|"export	"*)
                line="${line#export}"
                trim_hws "$line"
                line="$REPLY_TRIM"
                ;;
        esac

        case "$line" in
            *=*) ;;
            *)
                printf 'ERROR: Malformed .env syntax at line %s in %s.\n' "$line_no" "$f" >&2
                exit 78
                ;;
        esac

        raw_key="${line%%=*}"
        raw_val="${line#*=}"
        trim_hws "$raw_key"
        key="$REPLY_TRIM"
        case "$key" in
            ""|[0-9]*|*[!A-Za-z0-9_]*)
                printf 'ERROR: Malformed .env identifier at line %s in %s.\n' "$line_no" "$f" >&2
                exit 78
                ;;
        esac

        trim_hws "$raw_val"
        val_text="$REPLY_TRIM"
        parsed_val=""

        if [ -z "$val_text" ]; then
            parsed_val=""
        else
            case "$val_text" in
                \'*)
                    rest="${val_text#?}"
                    case "$rest" in
                        *\'*)
                            parsed_val="${rest%%\'*}"
                            after="${rest#*\'}"
                            trim_hws "$after"
                            after="$REPLY_TRIM"
                            case "$after" in
                                ""|\#*) ;;
                                *)
                                    printf 'ERROR: Malformed .env syntax at line %s in %s.\n' "$line_no" "$f" >&2
                                    exit 78
                                    ;;
                            esac
                            ;;
                        *)
                            printf 'ERROR: Unclosed single quote in .env at line %s in %s.\n' "$line_no" "$f" >&2
                            exit 78
                            ;;
                    esac
                    ;;
                \"*)
                    rem="${val_text#?}"
                    parsed_val=""
                    esc=0
                    closed=0
                    trailing=""
                    while [ -n "$rem" ]; do
                        rest="${rem#?}"
                        ch="${rem%"$rest"}"
                        rem="$rest"
                        if [ "$esc" = "1" ]; then
                            case "$ch" in
                                \"|\\) parsed_val="${parsed_val}${ch}" ;;
                                *) parsed_val="${parsed_val}\\${ch}" ;;
                            esac
                            esc=0
                            continue
                        fi
                        if [ "$ch" = "\\" ]; then
                            esc=1
                            continue
                        fi
                        if [ "$ch" = '"' ]; then
                            closed=1
                            trailing="$rem"
                            break
                        fi
                        parsed_val="${parsed_val}${ch}"
                    done
                    if [ "$esc" = "1" ] || [ "$closed" != "1" ]; then
                        printf 'ERROR: Unclosed double quote in .env at line %s in %s.\n' "$line_no" "$f" >&2
                        exit 78
                    fi
                    trim_hws "$trailing"
                    after="$REPLY_TRIM"
                    case "$after" in
                        ""|\#*) ;;
                        *)
                            printf 'ERROR: Malformed .env syntax at line %s in %s.\n' "$line_no" "$f" >&2
                            exit 78
                            ;;
                    esac
                    ;;
                *)
                    case "$val_text" in
                        *\"*|*\'*)
                            printf 'ERROR: Unexpected quote in .env value at line %s in %s.\n' "$line_no" "$f" >&2
                            exit 78
                            ;;
                    esac
                    rem="$val_text"
                    effective=""
                    prev=""
                    while [ -n "$rem" ]; do
                        rest="${rem#?}"
                        ch="${rem%"$rest"}"
                        rem="$rest"
                        if [ "$ch" = "#" ] && { [ "$prev" = " " ] || [ "$prev" = "	" ]; }; then
                            break
                        fi
                        effective="${effective}${ch}"
                        prev="$ch"
                    done
                    trim_hws "$effective"
                    parsed_val="$REPLY_TRIM"
                    ;;
            esac
        fi

        already_exported_here=0
        case "$exported_from_dotenv" in
            *" $key "*) already_exported_here=1 ;;
        esac
        if [ "$already_exported_here" = "1" ] || ! printenv "$key" >/dev/null 2>&1; then
            export "${key}=${parsed_val}"
            if [ "$already_exported_here" != "1" ]; then
                exported_from_dotenv="${exported_from_dotenv}${key} "
            fi
        fi
    done < "$f"
    return 0
}

env_loaded=0
if [ -n "${TRADELENS_ENV_FILE+SET}" ] && [ -z "${TRADELENS_ENV_FILE}" ]; then
    : # Explicit empty TRADELENS_ENV_FILE disables .env loading
else
    if load_dotenv_strict "$ENV_FILE"; then
        env_loaded=1
    else
        if [ "$is_remote" = "1" ] && [ -z "${TRADELENS_STATE_HMAC_SECRET:-}" ]; then
            printf 'ERROR: Missing .env (%s) and TRADELENS_STATE_HMAC_SECRET in remote mode.\n' "$ENV_FILE" >&2
            exit 78
        fi
    fi
fi

# Re-evaluate remote mode after loading .env, and re-verify .env permissions if .env itself enabled remote mode
if [ "${TRADELENS_REMOTE_ACCESS_ENABLED:-0}" = "1" ] || [ -n "${TRADELENS_ALLOWED_VPN_CIDRS:-}" ] || [ -n "${TRADELENS_ALLOWED_PEER_IPS:-}" ]; then
    is_remote=1
fi
if [ -n "${TRADELENS_BIND_HOST:-}" ] && is_non_loopback "${TRADELENS_BIND_HOST:-}"; then
    is_remote=1
fi

if [ "$env_loaded" = "1" ] && [ "$is_remote" = "1" ]; then
    check_env_perms "$ENV_FILE" || true
fi

if [ "$is_remote" = "1" ]; then
    hmac_val="${TRADELENS_STATE_HMAC_SECRET:-}"
    hmac_len=${#hmac_val}
    if [ "$hmac_len" -lt 16 ]; then
        printf 'ERROR: TRADELENS_STATE_HMAC_SECRET missing or too short (<16 chars) for remote mode.\n' >&2
        exit 78
    fi
fi

if [ "$#" -lt 1 ]; then
    printf 'Usage: %s <command> [args...]\n' "$0" >&2
    exit 64
fi

exec "$@"
