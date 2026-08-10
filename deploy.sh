#!/usr/bin/env bash
#
# Renders the homeserver config templates from environment variables and brings
# the stack up. Safe to run by hand on the server; also what CI invokes.
#
#   ./deploy.sh
#
# Secrets are read from the environment. CI writes homeserver/.env from the vault
# first; run by hand, the .env already on the server is used. If a secret is
# missing we refuse to deploy rather than render an empty password into a config.
#
# This deliberately does NOT touch the vault stack - that lives in vault/ and is
# brought up separately, so a homeserver deploy can never take the vault down.

set -euo pipefail
REPO="$(dirname "$(readlink -f "$0")")"
cd "$REPO/homeserver"

# Must be present and non-empty.
REQUIRED=(
    POSTGRES_PASSWORD
    DENDRITE_DB_CONNECTION_STRING
    DENDRITE_REGISTRATION_SHARED_SECRET
    DENDRITE_METRICS_PASSWORD
    RECAPTCHA_PUBLIC_KEY
    RECAPTCHA_PRIVATE_KEY
    TURN_SHARED_SECRET
    LIVEKIT_KEY
    LIVEKIT_SECRET
    MATRIX_DOMAIN
    CALL_DOMAIN
    TURN_EXTERNAL_IP
    TURN_LISTENING_IP
)

# Substituted, but legitimately empty - captcha is disabled, so the bypass
# secret is blank in the live config and must stay blank.
OPTIONAL=(
    RECAPTCHA_BYPASS_SECRET
)

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Guard: envsubst substitutes an empty string for an unset variable, which would
# blank out the DB password and break the server. Never allow that.
missing=""
for v in "${REQUIRED[@]}"; do
    [ -n "${!v:-}" ] || missing="$missing $v"
done
if [ -n "$missing" ]; then
    echo "refusing to deploy - these secrets are not set:$missing" >&2
    exit 1
fi

VARS=$(printf '$%s ' "${REQUIRED[@]}" "${OPTIONAL[@]}")

# Which service reads which file. A rendered file only takes effect when the
# process reading it restarts: `docker compose up -d` leaves a container alone
# when only a bind-mounted file changed, so a config-only deploy would otherwise
# report success while the old configuration is still live.
service_for() {
    case "$1" in
        config/dendrite.yaml)     echo monolith ;;
        livekit/livekit.yaml)     echo livekit ;;
        coturn/turnserver.conf)   echo coturn ;;
        element-call/config.json) echo element-call ;;
    esac
}

restart_services=""
for f in config/dendrite.yaml livekit/livekit.yaml coturn/turnserver.conf \
         element-call/config.json; do
    [ -f "$f.tmpl" ] || { echo "missing template: $f.tmpl" >&2; exit 1; }
    tmp=$(mktemp)
    envsubst "$VARS" < "$f.tmpl" > "$tmp"
    if cmp -s "$tmp" "$f"; then
        rm -f "$tmp"
    else
        [ -f "$f" ] && cp -p "$f" "$f.prev"   # keep the previous render
        mv "$tmp" "$f"
        chmod 644 "$f"
        echo "rendered $f"
        restart_services="$restart_services $(service_for "$f")"
    fi
done

# Images are :latest by choice. Pull only when explicitly asked, so a routine
# config deploy cannot silently upgrade Dendrite and migrate the database:
#   PULL=1 ./deploy.sh
if [ "${PULL:-0}" = "1" ]; then
    echo "pulling images (this may upgrade Dendrite - migrations are one-way)"
    docker compose pull
fi

docker compose up -d

# Pick up any config rendered above. up -d already recreated anything whose
# compose definition changed; restarting those again is harmless.
if [ -n "$restart_services" ]; then
    echo "restarting for changed config:$restart_services"
    # shellcheck disable=SC2086
    docker compose restart $restart_services
fi

echo "waiting for the homeserver to answer..."
for _ in $(seq 1 30); do
    if curl -fsS -m 5 http://127.0.0.1:8008/_matrix/client/versions >/dev/null 2>&1; then
        echo "healthy"
        exit 0
    fi
    sleep 2
done

echo "HEALTH CHECK FAILED - no response within 60s" >&2
docker compose ps
docker compose logs --tail=40 monolith
exit 1
