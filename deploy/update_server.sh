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
#   6. The canary on its timer (0.74.0): /etc/cron.d/elusion-canary written
#      as it should be, every run - including a run with nothing new to pull,
#      so a deleted or edited line is put back by the next update. Only
#      rewritten when it differs, and said either way.
#
#     ... update_server.sh --canary-cron           # print that file, change nothing
#     ... update_server.sh --install-canary-cron   # write only that file, then stop
#   7. A NEW VERSION THAT DOES NOT COME UP IS TAKEN BACK (0.88.0). Healthy is
#      /api/status answering AND the presence server saying it is listening.
#      If either fails after a pull, the code goes back to the last commit
#      that came up healthy, the requirements with it when they changed, both
#      services restart, and the same checks run again. The database is NEVER
#      put back: every save since step 4 would go with it, and the backup is
#      there for a person to decide. The run still exits 1, and says what
#      happened. --no-rollback leaves the new code in place, as before.
#
#     ... update_server.sh --no-rollback   # a failed update stays, for reading

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

# THE CANARY'S TIMER (0.74.0). The owner: "You already have the checker
# (canary.py); it just isn't running on a timer yet. Set up once on the
# server, it would tell you the same day if gold or lusions appear or vanish
# without being recorded." Hourly, at a minute nothing else here uses; the
# status file beside the database, where the API (the same user) reads it for
# the GM panel's Economy tab. ELUSION_CRON_D is only ever set by test_canary.py,
# which installs into a folder of its own.
CRON_D=${ELUSION_CRON_D:-/etc/cron.d}
CANARY_CRON=elusion-canary
CANARY_STATUS=$(dirname "$DB")/canary_status.json
CANARY_LOG=/var/log/elusion-canary.log

# HOW LONG A RESTART IS GIVEN to answer, in seconds, each of the two checks.
WAIT_SECONDS=20

# A SANDBOX FOR test_deploy.py ONLY. The run is the real main(), in a folder
# of the test's making, with stand-ins for systemctl, curl, sudo and journalctl
# on its PATH - which is how a broken update is shown to be taken back without
# a server. Nothing on the live box sets it.
if [ -n "${ELUSION_DEPLOY_SANDBOX:-}" ]; then
    API=$ELUSION_DEPLOY_SANDBOX/api
    VENV=$ELUSION_DEPLOY_SANDBOX/venv
    ENV_FILE=$ELUSION_DEPLOY_SANDBOX/elusion.env
    DB=$ELUSION_DEPLOY_SANDBOX/db/elusion.db
    BACKUPS=$ELUSION_DEPLOY_SANDBOX/backups
    DEPLOYED=$ELUSION_DEPLOY_SANDBOX/deployed_commit
    CANARY_STATUS=$(dirname "$DB")/canary_status.json
    WAIT_SECONDS=2
fi

# Every name here must be in the settings file, or the update stops before
# anything restarts. The second list is only warned about.
NEEDED=(ELUSION_OWNER ELUSION_DB ELUSION_TRUSTED_PROXIES)
WANTED=(ELUSION_CO_OWNERS ELUSION_SMTP_HOST ELUSION_MAIL_FROM ELUSION_TURN_KEY_ID ELUSION_TURN_KEY_TOKEN)

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

# THE CANARY'S CRON FILE, as it should be. Printed rather than kept in a file
# of its own, so this script is the one place it is written down.
#
# RUN AS ROOT, CHECKED AS elusion. The line's user is root only so that root
# can open the settings file - which is root's alone, chmod 600 - and hand it
# to the canary on standard input; runuser then drops to the service's user
# before Python starts, and the canary opens the database read-only. No value
# from the settings file is ever on a command line, where `ps` would show it.
# --quiet, so the log only grows when something is wrong.
canary_cron_text() {
    cat <<CRON
# /etc/cron.d/$CANARY_CRON - written by deploy/update_server.sh, which puts it
# back as it is there on every run. Change the script, not this file.
#
# Every hour, canary.py checks that the economy's books balance (gold and
# lusions conserved, no negative balance, no skill past the cap), writes what
# it found to $CANARY_STATUS for the GM panel's Economy
# tab, and on a failure mails the owner's confirmed recovery address with the
# server's own mail settings, handed over on standard input.
SHELL=/bin/sh
PATH=/usr/sbin:/usr/bin:/sbin:/bin
41 * * * * root cd $API && /usr/sbin/runuser -u elusion -- $VENV/bin/python $API/canary.py --db $DB --status-file $CANARY_STATUS --mail --env-stdin --quiet < $ENV_FILE >> $CANARY_LOG 2>&1
CRON
}

# Written only when it differs, through a temporary name in the same folder
# and a rename, so cron never reads half a file. The temporary name has a dot
# in it, and cron skips any file in cron.d whose name does.
install_canary_cron() {
    local target="$CRON_D/$CANARY_CRON" temporary
    if [ ! -d "$CRON_D" ] || [ ! -w "$CRON_D" ]; then
        say "  cannot write $CRON_D - run it as root."
        return 1
    fi
    # Each step checked by hand: called as `install_canary_cron || true`, the
    # function runs with set -e off, and a half-done install must not go on.
    temporary=$(mktemp "$CRON_D/.$CANARY_CRON.XXXXXX") || { say "  could not make a file in $CRON_D."; return 1; }
    if ! canary_cron_text > "$temporary"; then
        rm -f "$temporary"
        say "  could not write $temporary."
        return 1
    fi
    if [ -f "$target" ] && cmp -s "$temporary" "$target"; then
        rm -f "$temporary"
        say "  $target is as it should be - unchanged."
        return 0
    fi
    # cron reads a cron.d file only when root's and not writable by others.
    if ! chmod 644 "$temporary" || ! mv -f "$temporary" "$target"; then
        rm -f "$temporary"
        say "  could not put $target in place."
        return 1
    fi
    say "  $target written: hourly at :41, as elusion, status in $CANARY_STATUS."
}

# RESTART BOTH, THEN ASK BOTH (0.88.0). Prints what it found and returns 0
# only when the API answers /api/status and the presence server has said, since
# this restart, that it is listening. The presence half used to be nobody's
# check: a presence.py that would not start left the API up, the update
# "healthy", and every player alone in an empty world.
restart_and_check() {
    local started build _ status="" presence_ok=0
    started=$(date '+%Y-%m-%d %H:%M:%S')
    systemctl restart elusion-api
    systemctl restart elusion-presence

    build=$(grep -oP '^CURRENT_CLIENT_BUILD\s*=\s*\K[0-9]+' "$API/app.py" || echo 4)
    for _ in $(seq 1 "$WAIT_SECONDS"); do
        status=$(curl -s -m 3 -H "X-Elusion-Build: $build" http://127.0.0.1:5000/api/status || true)
        [ -n "$status" ] && break
        sleep 1
    done
    for _ in $(seq 1 "$WAIT_SECONDS"); do
        if [ "$(systemctl is-active elusion-presence || true)" = "active" ] \
                && journalctl -u elusion-presence --since "$started" --no-pager -o cat 2>/dev/null \
                    | grep -q '^\[PRESENCE\] listening'; then
            presence_ok=1
            break
        fi
        sleep 1
    done

    say ""
    say "elusion-api:      $(systemctl is-active elusion-api || true)"
    say "elusion-presence: $(systemctl is-active elusion-presence || true)"
    journalctl -u elusion-api --since "$started" --no-pager -o cat | grep -E '^\[(BOOT|DEPLOY)\]' || true
    say ""
    if [ -n "$status" ]; then
        say "/api/status: $status"
    else
        say "/api/status did not answer in $WAIT_SECONDS seconds."
    fi
    if [ "$presence_ok" -eq 1 ]; then
        say "presence: listening."
    else
        say "presence: did not say it was listening in $WAIT_SECONDS seconds."
    fi
    [ -n "$status" ] && [ "$presence_ok" -eq 1 ]
}

# THE WHOLE SCRIPT IS ONE FUNCTION, CALLED ON THE LAST LINE. bash reads a
# script as it runs it, and step 1 can replace this very file; a function is
# read in full before any of it runs, so a pull that changes this script
# cannot change the steps of the run already going.
main() {
    local force_restart=0 rollback=1 arg
    for arg in "$@"; do
        case "$arg" in
            --restart) force_restart=1 ;;
            --no-rollback) rollback=0 ;;
        esac
    done

    # THE CANARY'S TWO OWN MODES: print the file (anybody, nothing changes),
    # or write only it. Before the root check, so the first needs no sudo;
    # the second finds out for itself whether it may write.
    if [ "${1:-}" = "--canary-cron" ]; then
        canary_cron_text
        exit 0
    fi
    if [ "${1:-}" = "--install-canary-cron" ]; then
        step "The canary on its timer ($CRON_D/$CANARY_CRON)"
        install_canary_cron
        exit $?
    fi

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
        # THE CANARY'S LINE ALL THE SAME: nothing new to run does not mean the
        # timer is still there.
        step "6. The canary on its timer"
        install_canary_cron || true
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
    if restart_and_check; then
        say ""
        echo "$after" > "$DEPLOYED"
        step "6. The canary on its timer"
        install_canary_cron || true
        say ""
        say "Updated $before -> $after."
        exit 0
    fi

    say ""
    say "Read: journalctl -u elusion-api -n 50 --no-pager"
    say "  and journalctl -u elusion-presence -n 50 --no-pager"
    if [ "$before" = "$after" ] || [ "$rollback" -eq 0 ]; then
        [ "$rollback" -eq 0 ] && say "Left as it is (--no-rollback)."
        say "Live before this: $before. The backup from step 4 is the newest file in $BACKUPS."
        exit 1
    fi

    step "7. Taking it back to $before"
    # The code only. The database stays as it is: the backup is the newest
    # file in $BACKUPS, and putting it back is a person's decision.
    sudo -u elusion git reset --hard "$before"
    if sudo -u elusion git diff --name-only "$before" "$after" | grep -qx "requirements.txt"; then
        sudo -u elusion "$VENV/bin/pip" install -q -r requirements.txt
        say "requirements.txt put back - installed."
    fi
    if restart_and_check; then
        say ""
        say "TAKEN BACK: $after did not come up, so $before is running again."
        say "Players are back on the old version. Nothing in the database was undone."
        say "Fix what the log above says, push, and run this again."
    else
        say ""
        say "TAKEN BACK TO $before, AND IT DID NOT COME UP EITHER."
        say "Something outside the code is wrong (the settings, the disk, the database)."
        say "The backup from step 4 is the newest file in $BACKUPS."
    fi
    exit 1
}

main "$@"; exit $?
