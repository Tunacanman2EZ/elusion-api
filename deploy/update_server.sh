#!/usr/bin/env bash
# update_server.sh - bring the live API up to what is on GitHub, in one command.
#
#     bash /opt/elusion/api/deploy/update_server.sh             # on the server, as root
#     ssh root@api.elusionrpg.com bash /opt/elusion/api/deploy/update_server.sh   # from the PC
#     ... update_server.sh --restart     # restart even when nothing new was pulled
#
# It is DEPLOY.md's "Updating the server", by hand, plus the checks that were
# only ever done after something went wrong:
#
#   1. git pull, as elusion (the clone is that user's), fast-forward only.
#   2. The settings file is checked for the names this server needs - NAMES,
#      never values: nothing here prints a line of elusion.env. The co-owner
#      switch shipped once with ELUSION_CO_OWNERS missing, and the only sign
#      was "This server has no co-owner switch yet" in the game.
#   3. pip install, but only when requirements.txt changed in what came down.
#   4. A backup of the database before the restart, because a new version
#      may migrate it, and a migration is not undone by checking out old code.
#   5. Both services restarted, then asked: are they up, what did the API say
#      at boot ([BOOT] and [DEPLOY] lines), and does /api/status answer.
#
# Nothing is rolled back automatically. If the new code is wrong, the last
# line says which commit was live before and where the backup went.

set -euo pipefail

API=/opt/elusion/api
VENV=/opt/elusion/venv
ENV_FILE=/etc/elusion/elusion.env
DB=/var/lib/elusion/elusion.db
BACKUPS=/var/backups/elusion
# The commit that last came up healthy. Compared against rather than "what was
# checked out before this pull", because a run that stopped half-way (a
# missing setting, a failed install) has already pulled: the next run must
# still see those commits as new, and install what they need.
DEPLOYED=/var/lib/elusion/deployed_commit

# Every name here must be in the settings file, or the update stops before
# anything restarts. The second list is only warned about.
NEEDED=(ELUSION_OWNER ELUSION_DB ELUSION_TRUSTED_PROXIES)
WANTED=(ELUSION_CO_OWNERS ELUSION_SMTP_HOST ELUSION_MAIL_FROM ELUSION_TURN_KEY_ID ELUSION_TURN_KEY_TOKEN)

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

# THE WHOLE SCRIPT IS ONE FUNCTION, CALLED ON THE LAST LINE. bash reads a
# script as it runs it, and step 1 can replace this very file; a function is
# read in full before any of it runs, so a pull that changes this script
# cannot change the steps of the run already going.
main() {
    local force_restart=0
    [ "${1:-}" = "--restart" ] && force_restart=1

    if [ "$(id -u)" -ne 0 ]; then
        say "Run it as root (it restarts the services): sudo bash $0"
        exit 1
    fi
    cd "$API"

    step "1. Pulling from GitHub"
    local before after
    before=$(cat "$DEPLOYED" 2>/dev/null || true)
    if [ -z "$before" ] || ! sudo -u elusion git cat-file -e "$before^{commit}" 2>/dev/null; then
        before=$(sudo -u elusion git rev-parse --short HEAD)
    fi
    sudo -u elusion git pull --ff-only
    after=$(sudo -u elusion git rev-parse --short HEAD)
    if [ "$before" = "$after" ] && [ "$force_restart" -eq 0 ]; then
        say "Already running $after - nothing new, so nothing restarted. (--restart restarts anyway.)"
        exit 0
    fi
    say "$before -> $after"
    if [ "$before" != "$after" ]; then
        sudo -u elusion git --no-pager log --oneline "$before..$after" | head -20 || true
    fi

    step "2. Settings ($ENV_FILE) - names only"
    local missing=0 name
    for name in "${NEEDED[@]}"; do
        if grep -q "^${name}=" "$ENV_FILE"; then
            say "  ok       $name"
        else
            say "  MISSING  $name"
            missing=1
        fi
    done
    for name in "${WANTED[@]}"; do
        if grep -q "^${name}=" "$ENV_FILE"; then
            say "  ok       $name"
        else
            say "  not set  $name"
        fi
    done
    if [ "$missing" -ne 0 ]; then
        say ""
        say "Stopped before restarting: a setting this server needs is not in $ENV_FILE."
        say "The new code is pulled but NOT running - the old process is still serving."
        exit 1
    fi

    step "3. Requirements"
    if [ "$before" != "$after" ] && sudo -u elusion git diff --name-only "$before" "$after" | grep -qx "requirements.txt"; then
        sudo -u elusion "$VENV/bin/pip" install -q -r requirements.txt
        say "requirements.txt changed - installed."
    else
        say "unchanged."
    fi

    step "4. Backing up the database"
    sudo -u elusion /usr/bin/python3 "$API/backup_db.py" --db "$DB" --out "$BACKUPS" --keep 14

    step "5. Restarting"
    local started
    started=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart elusion-api
    systemctl restart elusion-presence

    local build status="" _
    build=$(grep -oP '^CURRENT_CLIENT_BUILD\s*=\s*\K[0-9]+' app.py || echo 4)
    for _ in $(seq 1 20); do
        status=$(curl -s -m 3 -H "X-Elusion-Build: $build" http://127.0.0.1:5000/api/status || true)
        [ -n "$status" ] && break
        sleep 1
    done

    say ""
    say "elusion-api:      $(systemctl is-active elusion-api || true)"
    say "elusion-presence: $(systemctl is-active elusion-presence || true)"
    journalctl -u elusion-api --since "$started" --no-pager -o cat | grep -E '^\[(BOOT|DEPLOY)\]' || true
    say ""
    if [ -n "$status" ]; then
        say "/api/status: $status"
        say ""
        echo "$after" > "$DEPLOYED"
        say "Updated $before -> $after."
    else
        say "/api/status did not answer in 20 seconds. Read: journalctl -u elusion-api -n 50 --no-pager"
        say "Live before this: $before. The backup from step 4 is the newest file in $BACKUPS."
        exit 1
    fi
}

main "$@"; exit $?
