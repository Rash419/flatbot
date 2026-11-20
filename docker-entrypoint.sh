#!/bin/sh
# Make the data directory (and the Codex sign-in folder) writable by the unprivileged
# user, then drop root and run the command as that user. A bind-mounted ./data is
# created by Docker as root (or owned by whichever host user made it), so fixing
# ownership here works no matter which UID owns it on the host.
#
# Any command goes through here, so `docker compose run --rm bot codex login --device-auth`
# signs in as the same user, into the same CODEX_HOME, that the bot uses.
set -e
data_dir=$(dirname "${DB_PATH:-/data/bot.db}")
codex_home=${CODEX_HOME:-/data/codex}
if [ "$(id -u)" = 0 ]; then
    mkdir -p "$data_dir" "$codex_home"
    find "$data_dir" "$codex_home" ! -user app -exec chown app:app {} +
    chmod 700 "$codex_home"
    export HOME=/home/app
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
mkdir -p "$codex_home" 2>/dev/null || true
exec "$@"
