#!/usr/bin/env python3
"""
canary.py - a read-only watch over the Elusion economy's invariants.

    python3 canary.py                    # check $ELUSION_DB (or elusion.db)
    python3 canary.py --db /path/elusion.db --quiet    # silent unless something broke
    python3 canary.py --db ... --status-file /path/canary_status.json --mail --env-stdin < settings

WHAT IT WATCHES, AND WHY IT IS SEPARATE FROM security_bot.py
------------------------------------------------------------
security_bot.py attacks a server BEFORE players are let onto it - a gate you run
by hand. This is the opposite watch: it reads the LIVE database on a schedule and
checks that the numbers still add up, so a slow leak, a bad deploy, or a tamper
that got past everything else shows up as an alert the same day, not as a player
noticing their gold is wrong.

It re-derives the same invariants the bot does, straight from ground truth:

  * gold conservation    SUM(gold_ledger.delta) == SUM(saves.gold)+SUM(bank_gold)
  * lusion conservation  SUM(lusion_ledger.delta) == SUM(accounts.lusions)
  * no negative balances anywhere
  * no skill above the cap (99)

Any drift here means gold or lusions entered or left the world without going
through the one function allowed to move them - which is either a bug or a
breach, and either way something to look at now.

READ-ONLY BY CONSTRUCTION. Opens the database mode=ro, so the watcher can never
be the thing that changed what it is watching. Same discipline as the bot.

EXIT CODE is 0 only if every invariant holds; non-zero otherwise. A scheduler
(cron, Task Scheduler) that checks it turns a broken number into an alert -
mail it, page it, drop it in a channel, whatever you run.

ON A TIMER, BY ITSELF (0.74.0)
------------------------------
The owner, of the first of five economy ideas: "Checking the gold totals
automatically ... You already have the checker (canary.py); it just isn't
running on a timer yet. Set up once on the server, it would tell you the same
day if gold or lusions appear or vanish without being recorded."

So deploy/update_server.sh installs /etc/cron.d/elusion-canary, and the line it
installs runs this hourly, as the service's own user, with three more flags:

  --status-file PATH   what it found, written ATOMICALLY (a temporary file in
                       the same folder, then a rename): {"checked_at", "ok",
                       "failures", "checks", ...}. The GM panel's Economy tab
                       reads it through GET /api/owner/economy/canary, so the
                       owner sees the last hourly check without a terminal.
  --mail               on a failure, mail every owner account's CONFIRMED
                       recovery address - the owner's, and a co-owner's while
                       the owner's co-owner switch is on - through the
                       server's own mail settings and SMTP conversation
                       (mailer.py, which app.py's send_mail() uses too).
  --env-stdin          read those settings from standard input, in the
                       settings file's KEY=value form. The cron line hands it
                       /etc/elusion/elusion.env: root opens the file, so it
                       stays root's alone (chmod 600), and no value is ever on
                       a command line where `ps` would show it.

NOTHING PRINTED OR WRITTEN HOLDS A CREDENTIAL OR AN ADDRESS. The status file and
the output say how many of the owner's addresses were mailed, never which; a
mail that failed says the kind of failure, never the server's reply.

IT MAILS WHEN THERE IS NEWS, not every hour of a broken day: when the books
first break, when what is broken changes, and once a day while it stays broken
(MAIL_AGAIN_SECONDS). The status file remembers when it last mailed.

NO MAIL SET UP is not an error of its own: the status file is written, the exit
is non-zero as for any failure, and --quiet stays quiet about the mail.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.request

import envfile
import mailer

# The status file's name, beside the database: on the server that is
# /var/lib/elusion, the service's own data folder, which the API (the same
# user) reads it from. One rule for both, so they cannot point at two files.
STATUS_NAME = "canary_status.json"

# While the books stay broken, how often the owner is told again.
MAIL_AGAIN_SECONDS = 24 * 3600

# The settings --env-stdin keeps from what it is handed. Everything else in
# the server's settings file is read past and dropped.
WANTED_SETTINGS = ("ELUSION_OWNER", "ELUSION_CO_OWNERS", "ELUSION_SMTP_HOST", "ELUSION_SMTP_PORT",
                   "ELUSION_SMTP_USER", "ELUSION_SMTP_PASSWORD", "ELUSION_MAIL_FROM", "ELUSION_MAIL_CONSOLE")

# The row the owner's co-owner switch lives in (app.py, CO_OWNERS_KEY).
CO_OWNERS_KEY = "coowners"


def check_all(db):
    def one(query):
        return int(db.execute(query).fetchone()[0] or 0)

    checks = []

    # GOLD CONSERVATION. The equation the whole gold_ledger exists to hold: every
    # coin held was minted through gold_delta() and recorded there.
    recorded = one("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger")
    held = (one("SELECT COALESCE(SUM(gold), 0) FROM saves")
            + one("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts"))
    checks.append((
        "gold conservation (ledger == held)",
        held == recorded,
        "recorded %d, held %d, drift %d" % (recorded, held, held - recorded),
    ))

    # LUSION CONSERVATION. gold_ledger's twin, its own currency and its own sum.
    l_recorded = one("SELECT COALESCE(SUM(delta), 0) FROM lusion_ledger")
    l_held = one("SELECT COALESCE(SUM(lusions), 0) FROM accounts")
    checks.append((
        "lusion conservation (ledger == held)",
        l_held == l_recorded,
        "recorded %d, held %d, drift %d" % (l_recorded, l_held, l_held - l_recorded),
    ))

    # NO NEGATIVE BALANCES. A negative subtracted is a positive minted; the DB
    # trigger and the gold_delta guard both forbid it, so a negative here means a
    # path found around both.
    min_gold = one("SELECT COALESCE(MIN(gold), 0) FROM saves")
    min_bank = one("SELECT COALESCE(MIN(bank_gold), 0) FROM accounts")
    min_lus = one("SELECT COALESCE(MIN(lusions), 0) FROM accounts")
    checks.append((
        "no negative balances",
        min_gold >= 0 and min_bank >= 0 and min_lus >= 0,
        "min carried %d, min banked %d, min lusions %d" % (min_gold, min_bank, min_lus),
    ))

    # NO SKILL ABOVE THE CAP. The one number a modified client most wants to lift.
    over_cap = one("SELECT COUNT(*) FROM skills WHERE level > 99")
    checks.append((
        "no skill above the cap (99)",
        over_cap == 0,
        "skill rows over the cap: %d" % over_cap,
    ))

    return checks


# =============================================================================
# THE CHECK, AS A REPORT - what the status file and the owner's route hold
# =============================================================================

def default_status_path(db_path):
    """Where the status file for `db_path` lives: beside it."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), STATUS_NAME)


def open_read_only(db_path):
    """The database, mode=ro. The path is made a URI properly, so a folder
    with a space or a '#' in its name is still the folder it names."""
    return sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(os.path.abspath(db_path)), uri=True)


def report_of(checks, now):
    """{"checked_at", "ok", "failures", "checks"} for check_all()'s answer."""
    rows = [{"name": name, "ok": bool(ok), "detail": str(detail)} for name, ok, detail in checks]
    return {
        "checked_at": int(now),
        "ok": all(row["ok"] for row in rows),
        "failures": [{"name": row["name"], "detail": row["detail"]} for row in rows if not row["ok"]],
        "checks": rows,
    }


def unreadable_report(reason, now):
    """The report when the checks could not run at all - a failure too, and
    said as one: a canary that cannot see the books has not seen them balance."""
    return {"checked_at": int(now), "ok": False,
            "failures": [{"name": "the database could be read", "detail": reason}], "checks": []}


def run_checks(db_path, now=None):
    """Every check, on a read-only connection of its own, as a report. Never
    raises: a missing or unreadable database is a failed report."""
    now = int(time.time()) if now is None else int(now)
    if not os.path.exists(db_path):
        return unreadable_report("no database at the configured path", now)
    try:
        con = open_read_only(db_path)
    except sqlite3.Error as exc:
        return unreadable_report("could not open it read-only (%s)" % type(exc).__name__, now)
    try:
        return report_of(check_all(con), now)
    except sqlite3.Error as exc:
        return unreadable_report("could not read it (%s)" % exc, now)
    finally:
        con.close()


def write_status(path, report):
    """The report, to `path`, ATOMICALLY: written to a temporary file in the
    same folder and renamed over the old one, so a reader - the owner's route,
    whenever the panel asks - sees the last whole report or this one, never
    half of either."""
    folder = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(prefix=".canary_status.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=1, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        # The service and its group: the folder is the service's (mode 750),
        # and nothing in the file is a secret.
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def read_status(path):
    """The last report written to `path`, or None when there is none or it
    cannot be read as one."""
    try:
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("checked_at"), int) \
            or isinstance(report.get("checked_at"), bool):
        return None
    return report


# =============================================================================
# TELLING THE OWNER
# =============================================================================

def settings_with_stdin(environ, text):
    """The environment, with the WANTED_SETTINGS the settings file `text`
    names filled in where the environment does not already say - envfile.py's
    rules (a real variable always wins; quotes and comments as a systemd
    EnvironmentFile reads them; a key named twice, the last)."""
    merged = dict(environ)
    from_file = {}
    for key, value in envfile.parse(text or ""):
        if key in WANTED_SETTINGS:
            from_file[key] = value
    for key, value in from_file.items():
        if key not in merged:
            merged[key] = value
    return merged


def owner_names(con, environ):
    """Every account with the owner's powers: the owner, and each co-owner
    the settings name while the owner's co-owner switch is on - app.py's
    has_owner_powers(), read from the same two places."""
    owner = str(environ.get("ELUSION_OWNER", "") or "").strip()
    names = [owner] if owner else []
    co_owners = [name.strip() for name in str(environ.get("ELUSION_CO_OWNERS", "") or "").split(",")
                 if name.strip() and not (owner and name.strip().casefold() == owner.casefold())]
    if co_owners:
        try:
            row = con.execute("SELECT value FROM server_settings WHERE key = ?", (CO_OWNERS_KEY,)).fetchone()
            switched_on = bool(row is not None and json.loads(row[0]).get("on"))
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            # Unreadable is OFF, as app.py reads it: a corrupt row must not
            # hand anyone the owner's mail.
            switched_on = False
        if switched_on:
            names.extend(co_owners)
    return names


def owner_addresses(con, environ):
    """The CONFIRMED recovery address of every owner account, each once. An
    unconfirmed address is not the owner's until it is confirmed - the same
    rule recovery itself keeps (users.email_verified)."""
    addresses = []
    seen = set()
    for name in owner_names(con, environ):
        row = con.execute(
            "SELECT email FROM users WHERE username = ? AND email_verified = 1 AND email != ''", (name,)
        ).fetchone()
        if row is None:
            continue
        address = str(row[0]).strip()
        if address and address.casefold() not in seen:
            seen.add(address.casefold())
            addresses.append(address)
    return addresses


def should_mail(previous, report, now):
    """Whether this report is news to the owner: the books broke, what broke
    changed, or a day has passed since the last mail and they are still
    broken."""
    if report.get("ok"):
        return False
    if not previous or previous.get("ok", True):
        return True
    names = sorted(str(f.get("name", "")) for f in report.get("failures", []))
    before = sorted(str(f.get("name", "")) for f in previous.get("failures", []) if isinstance(f, dict))
    if names != before:
        return True
    mailed_at = previous.get("mailed_at")
    return not isinstance(mailed_at, int) or now - mailed_at >= MAIL_AGAIN_SECONDS


def alert_text(report):
    """(subject, body) of the owner's mail: what broke, in the canary's own
    words and numbers, and where to look. No address, no setting, no path."""
    subject = "Elusion canary: the economy's books do not balance"
    lines = [
        "The hourly canary on api.elusionrpg.com found something wrong at %s UTC."
        % time.strftime("%Y-%m-%d %H:%M", time.gmtime(report["checked_at"])),
        "",
    ]
    for failure in report.get("failures", []):
        lines.append("  BROKEN  %s" % failure.get("name", ""))
        lines.append("          %s" % failure.get("detail", ""))
    lines += [
        "",
        "Gold or lusions entered or left the world without being recorded - a bug or a",
        "tamper, and either way something to look at today. The GM panel's Economy tab",
        "shows this check, and Check now runs it again; DEPLOY.md, \"Watching the live",
        "economy\", says how to run it on the server.",
        "",
        "You will hear again if what is broken changes, or in a day if it stays broken.",
    ]
    return subject, "\n".join(lines) + "\n"


def tell_the_owner(report, db_path, environ, deliver=mailer.deliver):
    """Mail every owner address. Answers {"mail": what happened, "mailed": how
    many of them were reached, "addresses": how many there were} - counts and
    words, never an address."""
    settings = mailer.settings_from(environ)
    if not mailer.can_send(settings):
        return {"mail": "not configured", "mailed": 0, "addresses": 0}
    try:
        con = open_read_only(db_path)
        try:
            addresses = owner_addresses(con, environ)
        finally:
            con.close()
    except sqlite3.Error:
        addresses = []
    if not addresses:
        return {"mail": "no confirmed owner address", "mailed": 0, "addresses": 0}
    subject, body = alert_text(report)
    if settings["console"]:
        # CONSOLE MODE (a PC): said, not sent - and said without the address,
        # which app.py's console line prints and this must not.
        print("[CANARY] mail (console mode, nothing sent) to %d owner address%s: %s"
              % (len(addresses), "" if len(addresses) == 1 else "es", subject))
        return {"mail": "console", "mailed": len(addresses), "addresses": len(addresses)}
    reached = 0
    failure = ""
    for address in addresses:
        sent, error = deliver(settings, address, subject, body)
        if sent:
            reached += 1
        elif not failure:
            failure = type(error).__name__ if error is not None else "unknown"
    if reached == len(addresses):
        return {"mail": "sent", "mailed": reached, "addresses": len(addresses)}
    return {"mail": "failed (%s)" % failure, "mailed": reached, "addresses": len(addresses)}


def record(path, report, db_path, environ, mail, now, deliver=mailer.deliver):
    """Mail if `mail` and this is news, then write the status file with what
    the mail did and when the owner was last told. Raises OSError when the
    file cannot be written."""
    previous = read_status(path)
    if report.get("ok"):
        report["mailed_at"] = None
    elif mail and should_mail(previous, report, now):
        report.update(tell_the_owner(report, db_path, environ, deliver))
        report["mailed_at"] = now if report.get("mailed") else (previous or {}).get("mailed_at")
    else:
        if mail:
            report["mail"] = "already told"
        report["mailed_at"] = (previous or {}).get("mailed_at")
    write_status(path, report)
    return report


def main(argv=None, stdin=None, environ=None, deliver=mailer.deliver, now=None):
    """The command line. Every seam a keyword, for the suite: the arguments,
    standard input, the environment, the SMTP step and the clock."""
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    ap = argparse.ArgumentParser(description="Read-only invariant watch over the Elusion economy.")
    ap.add_argument("--db", default=environ.get("ELUSION_DB", "elusion.db"),
                    help="database to check (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless an invariant broke (for cron)")
    ap.add_argument("--status-file", default="",
                    help="write what was found here, atomically, as JSON (the GM panel reads it)")
    ap.add_argument("--mail", action="store_true",
                    help="on a failure, mail every owner account's confirmed recovery address")
    ap.add_argument("--env-stdin", action="store_true",
                    help="read the server's settings (KEY=value) from standard input, for --mail")
    args = ap.parse_args(argv)
    now = int(time.time()) if now is None else int(now)

    if args.env_stdin:
        # NEVER FROM A TERMINAL: a person typing settings is not the cron line
        # handing the file over, and waiting on them would hang the check.
        text = "" if getattr(stdin, "isatty", lambda: False)() else stdin.read()
        environ = settings_with_stdin(environ, text)

    report = run_checks(args.db, now)
    if args.status_file:
        try:
            record(args.status_file, report, args.db, environ, args.mail, now, deliver)
        except OSError as exc:
            print("[CANARY] could not write the status file (%s)" % type(exc).__name__, file=sys.stderr)
    elif args.mail and not report["ok"]:
        report.update(tell_the_owner(report, args.db, environ, deliver))

    if not report["checks"]:
        # The database could not be read at all: said as it always was.
        detail = report["failures"][0]["detail"]
        if detail == "no database at the configured path":
            print("[CANARY] FAIL: no database at %s" % args.db, file=sys.stderr)
        else:
            print("[CANARY] FAIL: could not read the database: %s" % detail, file=sys.stderr)
        sys.exit(2)

    broken = report["failures"]
    if broken or not args.quiet:
        print("=" * 64)
        print("  ELUSION CANARY - %s" % args.db)
        print("=" * 64)
        for row in report["checks"]:
            print("  [ %-6s] %s" % ("hold" if row["ok"] else "BROKEN", row["name"]))
            print("           %s" % row["detail"])
        print("=" * 64)
        print("  " + ("FAIL - %d invariant(s) broke; investigate now." % len(broken)
                      if broken else "PASS - every number adds up."))
        # WHAT THE MAIL DID - said unless it is "not set up" under --quiet,
        # which the status file holds and the cron log need not repeat hourly.
        said = report.get("mail")
        if broken and said and not (args.quiet and said == "not configured"):
            print("  mail: %s (%d of %d owner address%s)" % (
                said, int(report.get("mailed", 0) or 0), int(report.get("addresses", 0) or 0),
                "" if int(report.get("addresses", 0) or 0) == 1 else "es"))
        print("=" * 64)

    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
