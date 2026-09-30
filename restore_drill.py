"""restore_drill.py - a backup you have never restored is a rumour.

DEPLOY.md says exactly that, in the Backups section, and then asks you to do it
by hand: "copy a backup file to a scratch path, point a throwaway server at it,
and log in." A manual step described in a document is a step that happens once,
if ever, and never again after the week somebody wrote it down. This is that
paragraph as a command.

WHAT backup_db.py ALREADY PROVES, so this does not repeat it: the snapshot is
internally consistent. It reopens the file it just wrote, runs integrity_check
and reads a row before trusting it. That is a real check and it is not the same
question as the one that matters at 3am.

WHAT THIS PROVES INSTEAD: that the SERVER can serve the file. Those come apart.
A database can pass integrity_check and still be a schema this build of app.py
cannot run - a migration that ran after the backup was taken, a column a route
now selects, a table added last month. The backup is fine; the pairing is not,
and nobody finds out until the night they need it.

So the drill does the whole round trip:

    pick a backup -> copy it somewhere scratch -> read it cold ->
    start a real server against the copy -> register -> log in ->
    confirm the accounts that were in the backup are still there -> tear down

and exits non-zero the moment any of that fails.

    python restore_drill.py --dir /var/backups/elusion   # newest backup there
    python restore_drill.py --backup path/to/one.db      # that exact one
    python restore_drill.py --db elusion.db              # take a fresh one first

IT NEVER TOUCHES THE LIVE DATABASE. The source is copied, never opened for
writing, and the drill refuses outright if the file it is about to serve
resolves to the same path as a live database. Everything it creates lives in a
temp directory that is removed on the way out, including after a failure.

IT REGISTERS RATHER THAN LOGGING IN AS SOMEBODY REAL, and that is not a
shortcut. Logging in as a real account needs a real password, which this script
must never hold, never prompt for and never write to a log. Registering into a
throwaway COPY harms nothing and proves strictly more: it exercises the write
path, the scrypt hash, the session insert and the token read, where a login
would only exercise the last two.

WHAT IT DELIBERATELY DOES NOT PROVE: that your production server configuration
is right. It serves the copy with whatever WSGI server it can find, on
loopback, without TLS or a proxy, because the question here is about the FILE.
The proxy count, the TLS chain and the file permissions are checklist items in
DEPLOY.md and no drill can answer them from inside.
"""

import argparse
import contextlib
import glob
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))
    return bool(condition)


def die(message):
    print("\n[DRILL] %s" % message, file=sys.stderr)
    sys.exit(2)


# -----------------------------------------------------------------------------
# CHOOSING WHAT TO DRILL
# -----------------------------------------------------------------------------

def newest_backup(directory):
    """The most recent .db in `directory`.

    BY MTIME RATHER THAN BY NAME. backup_db.py timestamps its filenames and
    sorting those strings happens to work today, but it is a property of the
    format rather than of the files, and the one night it matters is not the
    night to find out the format changed."""
    candidates = [p for p in glob.glob(os.path.join(directory, "*.db"))
                  if os.path.isfile(p)]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def take_fresh_backup(db_path, into):
    """Run backup_db.py rather than reimplementing it.

    THE POINT OF THE DRILL IS THE REAL PATH. A snapshot taken here with a
    hand-written sqlite backup call would prove that THIS script can copy a
    database, which is not the thing anybody relies on at 3am. If backup_db.py
    is broken, the drill has to fail."""
    script = os.path.join(HERE, "backup_db.py")
    if not os.path.exists(script):
        die("no backup_db.py beside this script - cannot take a fresh backup")
    result = subprocess.run(
        [sys.executable, script, "--db", db_path, "--out", into, "--keep", "0"],
        capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        die("backup_db.py exited %d - there is nothing to drill" % result.returncode)
    made = newest_backup(into)
    if made is None:
        die("backup_db.py reported success and wrote no file")
    return made


# -----------------------------------------------------------------------------
# READING THE COPY COLD
# -----------------------------------------------------------------------------

def read_cold(path):
    """What is in the file, before any server sees it.

    mode=ro SO THE DRILL CANNOT BE WHAT REPAIRED IT. SQLite will happily roll
    back a hot journal on a read-write open, which would turn a damaged backup
    into a working one at exactly the moment we were trying to find out whether
    it was damaged."""
    # A CORRUPT FILE IS THE COMMONEST REAL FAILURE AND IT USED TO BE THE ONE
    # THIS REPORTED WORST. sqlite3 raises DatabaseError out of the first PRAGMA
    # on a truncated file or a text file with a .db name, and an uncaught raise
    # here ended the run with a traceback and no summary line - so the output
    # read as "the drill is broken" rather than "the backup is". The exit code
    # was right and nobody reads an exit code at 3am. Found by sabotage: 4000
    # zero bytes written into the middle of a good backup.
    uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return ("cannot open: %s" % exc), set(), {}
    try:
        con.row_factory = sqlite3.Row
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = set(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        counts = {}
        for t in ("users", "saves", "sessions"):
            if t in tables:
                counts[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        return integrity, tables, counts
    except sqlite3.DatabaseError as exc:
        # The message sqlite gives here is the useful one - "file is not a
        # database", "database disk image is malformed" - so it is passed
        # through as the integrity answer rather than replaced with a tidier
        # sentence that says less.
        return str(exc), set(), {}
    finally:
        con.close()


# -----------------------------------------------------------------------------
# A SERVER ON THE COPY
# -----------------------------------------------------------------------------

def free_port():
    """A port nothing is using, chosen by the kernel.

    NOT A FIXED NUMBER. A drill that hardcodes 5001 fails on the one machine
    where something else already holds it, which reads as a bad backup."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db_path, port):
    """Serve the copy through wsgi.py, the way production does.

    THROUGH wsgi.py AND NOT app.py, because wsgi.py is what a real server
    imports and it runs the preflight first. If the preflight would refuse this
    configuration, the drill should discover that too.

    The built-in server is fine HERE and would not be fine in production: this
    is one request at a time on loopback for ten seconds. The point is the file,
    not the concurrency."""
    env = dict(os.environ)
    env["ELUSION_DB"] = db_path
    # ELUSION_DEBUG WOULD MAKE wsgi.py REFUSE TO START, correctly. Cleared so a
    # dev machine's environment does not turn a good backup into a red drill.
    env.pop("ELUSION_DEBUG", None)
    env.setdefault("ELUSION_OWNER", "drill_owner_not_a_real_account")

    runner = (
        "import wsgi;"
        "wsgi.application.run(host='127.0.0.1', port=%d, debug=False, use_reloader=False)"
        % port
    )
    return subprocess.Popen(
        [sys.executable, "-c", runner],
        cwd=HERE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(url, proc, seconds=25.0):
    """Poll until the server answers, or it dies, or we give up."""
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, "the server exited before answering (code %s)" % proc.returncode
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True, ""
                last = "HTTP %d" % r.status
        except Exception as exc:          # connection refused while it boots
            last = str(exc)
        time.sleep(0.3)
    return False, "no answer in %.0fs (%s)" % (seconds, last)


def post(url, payload, token=None):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw[:200]}
    except Exception as exc:
        return 0, {"error": str(exc)}


def get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:
        return 0, {"error": str(exc)}


# -----------------------------------------------------------------------------
# THE DRILL
# -----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Restore a backup to scratch and prove a server can serve it.")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup", help="drill this exact backup file")
    source.add_argument("--dir", help="drill the newest .db in this directory")
    source.add_argument("--db", help="take a fresh backup of this database, then drill it")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="leave the scratch copy behind for inspection")
    args = ap.parse_args()

    print("\n=== restore drill - a backup you have never restored is a rumour ===\n")

    scratch = tempfile.mkdtemp(prefix="elusion-drill-")
    served = None
    proc = None

    try:
        # ---------------------------------------------------------------- pick
        if args.db:
            live = os.path.abspath(args.db)
            if not os.path.exists(live):
                die("no database at %s" % live)
            backup = take_fresh_backup(live, os.path.join(scratch, "fresh"))
        elif args.dir:
            backup = newest_backup(args.dir)
            if backup is None:
                die("no .db files in %s - nothing to drill" % args.dir)
        else:
            backup = os.path.abspath(args.backup)
            if not os.path.exists(backup):
                die("no backup at %s" % backup)

        print("  drilling: %s" % backup)
        print("  scratch:  %s\n" % scratch)

        # ------------------------------------------------- refuse the live file
        # THE GUARD THAT MATTERS. Everything below starts a server that WRITES -
        # it registers an account. Pointed at a live database that would put a
        # junk account into production, and the one command you reach for in a
        # crisis is not the one to find that out with.
        served = os.path.join(scratch, "restored.db")
        live_candidates = [
            os.environ.get("ELUSION_DB", ""),
            os.path.join(HERE, "elusion.db"),
        ]
        if args.db:
            live_candidates.append(args.db)
        for cand in live_candidates:
            if not cand:
                continue
            if os.path.exists(cand) and os.path.exists(backup) \
                    and os.path.samefile(cand, backup):
                die("refusing to drill %s - that is a LIVE database, not a backup. "
                    "The drill writes to what it serves." % backup)

        shutil.copy2(backup, served)
        check("the backup copies to scratch", os.path.exists(served),
              "could not copy %s" % backup)
        check("and the copy is the same size as the backup",
              os.path.getsize(served) == os.path.getsize(backup),
              "%d vs %d" % (os.path.getsize(served), os.path.getsize(backup)))

        # ------------------------------------------------------------ read cold
        integrity, tables, counts = read_cold(served)
        check("the restored file passes integrity_check", integrity == "ok", integrity)

        # THE SCHEMA THE SERVER WILL LOOK FOR. A backup missing one of these is
        # a backup of something else - an older build, a half-migrated file, or
        # the wrong database entirely.
        for t in ("users", "saves", "sessions"):
            check("it has a %s table" % t, t in tables,
                  "sorted(tables) = %s" % sorted(tables)[:12])

        users_before = counts.get("users", 0)
        # A RESTORE THAT BRINGS BACK AN EMPTY DATABASE IS NOT A RESTORE. It
        # would pass integrity_check, serve happily, and have lost everything -
        # which is exactly the failure a backup exists to prevent, so it is the
        # one result that must never read as success.
        check("it contains accounts (a restore of nothing is not a restore)",
              users_before > 0, "%d rows in users" % users_before)
        print("       %d accounts, %d characters in the restored copy"
              % (users_before, counts.get("saves", 0)))

        # ------------------------------------------------------- serve the copy
        port = free_port()
        base = "http://127.0.0.1:%d" % port
        proc = start_server(served, port)

        up, why = wait_for(base + "/api/status", proc)
        if not check("a server starts against the restored file", up, why):
            out = ""
            if proc.poll() is not None and proc.stdout:
                out = proc.stdout.read()[-1500:]
            if out:
                print("\n  --- what the server said ---\n%s\n  ---" % out)
            # NOT raise SystemExit. It used to, and the summary at the bottom
            # never printed - so the worst case, a backup no server can open,
            # ended with a wall of subprocess traceback and no verdict. Same
            # defect as the uncaught sqlite error above: right exit code, and
            # an output that reads as "the tool broke" rather than "the backup
            # is bad". Sabotage found both; neither was in the happy path.
            served_ok = False
        else:
            served_ok = True

        if served_ok:
            status_code, status = get(base + "/api/status")
            check("/api/status answers 200", status_code == 200, status_code)

        # ------------------------------------------- register, log in, read back
        # A NAME NOBODY COULD ALREADY HAVE, and inside USERNAME_PATTERN
        # (^[A-Za-z0-9_]{3,20}$) so a refusal means the server refused, not that
        # the drill sent something malformed.
        if served_ok:
            who = "drill_%d" % (int(time.time()) % 1000000)
            secret = "drill-only-%d" % os.getpid()   # >= MIN_PASSWORD_LENGTH (8)

            code, body = post(base + "/api/auth/register",
                              {"username": who, "password": secret})
            check("a new account registers against the restored file", code == 201,
                  "%s %s" % (code, body.get("message", "")))

            code, body = post(base + "/api/auth/login",
                              {"username": who, "password": secret})
            ok_login = check("and logs in", code == 200,
                             "%s %s" % (code, body.get("message", "")))
            token = body.get("token", "") if ok_login else ""
            check("the login returns a token", bool(token),
                  "no token in the login response")

            if token:
                code, body = get(base + "/api/auth/session", token)
                check("the token is accepted on the next request", code == 200, code)
                check("and the session names the account we made",
                      str(body.get("username", "")).lower() == who.lower(),
                      body.get("username", ""))

            # ------------------------------------ the old accounts are still there
            # THE WHOLE POINT, AND THE EASIEST THING TO FORGET TO ASK. Everything
            # above would pass on an empty database with the right schema. What a
            # restore is FOR is the rows that were in it, so they get counted
            # again after the server has opened, written and migrated the file.
            _, _, after = read_cold(served)
            users_after = after.get("users", 0)
            check("the accounts from the backup survived being served",
                  users_after == users_before + 1,
                  "%d before, %d after - expected one more (the drill's own)"
                  % (users_before, users_after))

    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if args.keep_scratch:
            print("\n  scratch kept at %s" % scratch)
        else:
            # ONLY THE SCRATCH DIRECTORY, and only one this process made with
            # mkdtemp. The backup itself is never touched: a drill that could
            # delete the thing it was testing would be worse than no drill.
            shutil.rmtree(scratch, ignore_errors=True)

    print("\n" + "=" * 70)
    print("  %d passed, %d failed" % (passed, failed))
    if failures:
        print("\n  failed:")
        for f in failures:
            print("    - %s" % f)
        print("\n  This backup is not one you can restore from. Find out why now,")
        print("  not on the night you need it.")
    else:
        print("\n  This backup restores, serves, authenticates and still has its data.")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
