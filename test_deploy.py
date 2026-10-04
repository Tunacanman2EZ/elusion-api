#!/usr/bin/env python3
"""
test_deploy.py - the two things the first live deploy found, held.

    python3 test_deploy.py

Standing the server up on a real droplet (DEPLOY.md, "The live server") turned
up two bugs that no other suite could see, because every suite sets its
settings in the environment and none of them had ever taken a backup and then
looked at the folder.

1. A .ENV WAS READ TOO LATE. app.py read its .env with a helper four hundred
   lines down, after `import gamedata` had taken ELUSION_GAMEDATA, DB_PATH had
   taken ELUSION_DB and TRUSTED_PROXY_HOPS had taken ELUSION_TRUSTED_PROXIES.
   Those three in a .env were ignored without a word - the server booted
   against elusion.db beside the code, with a proxy count of 0. wsgi.py's
   preflight read every setting before app.py was imported, so under a real
   WSGI server nothing in .env existed while it checked. The live server uses
   systemd's EnvironmentFile and was never affected; anybody following
   "or put it in a .env" was. envfile.py is the fix: one import, first.

2. EVERY BACKUP GREW TWO FILES. The live database runs in WAL mode, the online
   backup API copies the mode with the pages, and opening a WAL file at all -
   even read-only, even to verify it - creates a -wal and a -shm beside it. The
   first night's backup was three files, and prune() only knew about .db, so
   the other two would have stayed for ever. backup_db.py now switches each
   copy to journal_mode=DELETE, refuses to verify a copy still in WAL, and
   tidies older copies and leftovers.

THE SECTIONS
  A  envfile: the parsing rules and "a real variable wins"
  B  app.py and wsgi.py read the .env before anything else (the source)
  C  boot the real app.py and wsgi.py with NOTHING but a .env (subprocesses)
  D  backups are one file, the live database stays WAL
  E  prune and tidy: sidecars, leftovers, other databases' files
  F  restore_drill.py restores one of the new single-file copies

No network, no real database: everything is built in a temp directory.
"""

import ast
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import envfile      # noqa: E402
import backup_db    # noqa: E402

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
        print("  FAIL  %s" % label)
        if detail != "":
            print("        %s" % (detail,))


def section(title):
    print("\n=== %s ===\n" % title)


SCRATCH = tempfile.mkdtemp(prefix="elusion_deploy_")


def scratch(*parts):
    path = os.path.join(SCRATCH, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def write_env(directory, text, bom=False):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, ".env"), "w", encoding="utf-8-sig" if bom else "utf-8",
              newline="\n") as handle:
        handle.write(text)


def quiet_load(directory, environ):
    """envfile.load(), with what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        taken = envfile.load(directory, environ)
    return taken, out.getvalue()


# =============================================================================
section("A  envfile: the rules")
# =============================================================================

d = scratch("a1", "x")[:-2]
write_env(d, "\n".join([
    "# a comment",
    "",
    "   PLAIN = value with spaces   ",
    "QUOTED=\"Elusion RPG <someone@example.com>\"",
    "SINGLE='single quoted'",
    "UNMATCHED=\"only one",
    "EQUALS=a=b=c",
    "EMPTY=",
    "no equals sign here",
    "=no key",
    "TWICE=first",
    "TWICE=second",
    "ALREADY=from the file",
    "SECRETISH=not-a-real-secret-4471",
]) + "\n")
env = {"ALREADY": "from the environment"}
taken, said = quiet_load(d, env)

check("a plain value, spaces around it trimmed and inside it kept",
      env.get("PLAIN") == "value with spaces", env.get("PLAIN"))
check("one matching pair of double quotes is removed (the MAIL_FROM shape)",
      env.get("QUOTED") == "Elusion RPG <someone@example.com>", env.get("QUOTED"))
check("one matching pair of single quotes is removed",
      env.get("SINGLE") == "single quoted", env.get("SINGLE"))
check("a quote with no partner is part of the value",
      env.get("UNMATCHED") == "\"only one", env.get("UNMATCHED"))
check("only the first '=' splits; the rest belong to the value",
      env.get("EQUALS") == "a=b=c", env.get("EQUALS"))
check("an empty value is set, as empty", env.get("EMPTY") == "", env.get("EMPTY"))
check("comments, blank lines, lines without '=' and an empty key set nothing",
      set(env) == {"PLAIN", "QUOTED", "SINGLE", "UNMATCHED", "EQUALS", "EMPTY",
                   "TWICE", "ALREADY", "SECRETISH"}, sorted(env))
check("a key named twice takes the last value, as systemd and a shell do",
      env.get("TWICE") == "second", env.get("TWICE"))
check("A REAL ENVIRONMENT VARIABLE WINS over the file",
      env.get("ALREADY") == "from the environment", env.get("ALREADY"))
check("load() returns the keys it set, and not the one already set",
      "ALREADY" not in taken and "PLAIN" in taken and len(taken) == len(set(taken)),
      taken)
check("the boot line names what it set",
      "PLAIN" in said and "SECRETISH" in said, said)
check("...and never a value (it lands in the service log)",
      "not-a-real-secret-4471" not in said and "value with spaces" not in said, said)

d = scratch("a2", "x")[:-2]
write_env(d, "BOMKEY=yes\nNEXT=1\n", bom=True)
env = {}
quiet_load(d, env)
check("a byte-order mark is not part of the first key (PowerShell 5 writes one)",
      env.get("BOMKEY") == "yes" and not any(k.startswith("﻿") for k in env),
      sorted(env))

env = {}
taken, said = quiet_load(scratch("a3", "x")[:-2], env)
check("no .env is normal: nothing set, nothing said", taken == [] and env == {} and said == "",
      (taken, env, said))

d = scratch("a4", "x")[:-2]
os.makedirs(os.path.join(d, ".env"), exist_ok=True)       # a .env that cannot be read
env = {}
try:
    taken, said = quiet_load(d, env)
except Exception as exc:                                     # noqa: BLE001
    taken, said = None, "raised %r - a boot would have stopped here" % exc
check("an unreadable .env says so and sets nothing, rather than stopping the boot",
      taken == [] and env == {} and "could not read" in said, said)

d = scratch("a5", "x")[:-2]
write_env(d, "ELUSION_DEPLOY_TEST_ONLY=filled\n")
os.environ.pop("ELUSION_DEPLOY_TEST_ONLY", None)
with contextlib.redirect_stdout(io.StringIO()):
    envfile.load(d)
check("with no mapping given, load() fills os.environ itself",
      os.environ.pop("ELUSION_DEPLOY_TEST_ONLY", None) == "filled")


# =============================================================================
section("B  app.py and wsgi.py read the .env before anything else")
# =============================================================================

def module_body(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as handle:
        source = handle.read()
    return source, ast.parse(source).body


def is_load_call(node):
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) == "envfile.load")


def reads_settings(node, source):
    """A top-level statement that reads a setting or imports something that does."""
    text = ast.get_source_segment(source, node) or ""
    if "environ" in text or "getenv" in text:
        return True
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = [a.name for a in node.names]
        module = getattr(node, "module", None) or ""
        return any(n in ("gamedata", "app") for n in names) or module in ("gamedata", "app")
    return False


source, body = module_body("app.py")
check("app.py: the first statement is `import envfile`",
      isinstance(body[0], ast.Import) and [a.name for a in body[0].names] == ["envfile"],
      ast.unparse(body[0])[:80])
check("app.py: the second is `envfile.load()` - nothing above it can read a setting",
      len(body) > 1 and is_load_call(body[1]), ast.unparse(body[1])[:80] if len(body) > 1 else "")
check("app.py: the old late helper is gone, so there is one reader",
      "_load_dotenv" not in source)

source, body = module_body("wsgi.py")
loads = [i for i, node in enumerate(body) if is_load_call(node)]
readers = [i for i, node in enumerate(body) if reads_settings(node, source)]
check("wsgi.py calls envfile.load()", len(loads) == 1, loads)
check("wsgi.py: before the first statement that reads a setting or imports app",
      loads and readers and loads[0] < readers[0],
      "load at %s, first reader at %s" % (loads, readers[:1]))


# =============================================================================
section("C  boot the real files with nothing but a .env")
# =============================================================================
# THE CODE IS COPIED SOMEWHERE NEW, because "beside the code" is the whole
# question: a .env in this folder would be read by every suite in it. The
# gamedata file is copied under ANOTHER name, so the only way the server finds
# it is the ELUSION_GAMEDATA line in the .env. The database goes in a folder of
# its own for the same reason.

CODE = scratch("code", "x")[:-2]
for name in ("app.py", "envfile.py", "gamedata.py", "wsgi.py"):
    shutil.copy(os.path.join(HERE, name), os.path.join(CODE, name))
DATA_DIR = scratch("var", "x")[:-2]
LIVE_DB = os.path.join(DATA_DIR, "live.db")
CATALOGUE = os.path.join(DATA_DIR, "catalogue_under_another_name.json")
shutil.copy(os.path.join(HERE, "gamedata.json"), CATALOGUE)
ELSEWHERE = scratch("cwd", "x")[:-2]     # run from here, so the cwd is no help

ENV_TEXT = "\n".join([
    "# what a self-hoster would write, following the comment in app.py",
    "ELUSION_DB=%s" % LIVE_DB,
    "ELUSION_TRUSTED_PROXIES=1",
    "ELUSION_GAMEDATA=%s" % CATALOGUE,
    "ELUSION_OWNER=envfile_owner",
    "ELUSION_SMTP_PASSWORD=not-a-real-secret-4471",
]) + "\n"


def clean_env(**extra):
    """This process's environment with every server setting taken out."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ELUSION_", "FLASK_"))}
    env.update(extra)
    return env


PROBE = r"""
import json, os, sys
sys.path.insert(0, %r)
import app, gamedata
client = app.app.test_client()
r = client.post("/api/auth/register", json={"username": "drillsubject", "password": "hunter2hunter2"})
print("PROBE " + json.dumps({
    "db": app.DB_PATH,
    "hops": app.TRUSTED_PROXY_HOPS,
    "catalogue": gamedata.GAMEDATA_PATH,
    "items": len(gamedata.ITEMS),
    "owner": app.OWNER_USERNAME,
    "register": r.status_code,
}))
""" % CODE


def probe(env):
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ELSEWHERE, env=env,
                            capture_output=True, text=True, timeout=120)
    found = {}
    for line in result.stdout.splitlines():
        if line.startswith("PROBE "):
            found = json.loads(line[6:])
    return result, found


def boot_wsgi(env):
    return subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import wsgi" % CODE],
                          cwd=ELSEWHERE, env=env, capture_output=True, text=True, timeout=120)


# The control first: no .env at all. If these do not differ from the run with
# one, the checks below could not tell the fix from the bug. The catalogue comes
# from the real environment here, because without it the boot stops on
# "gamedata.json not found" - which is itself the proof that the line in the
# .env is what the boot below is reading.
result, seen = probe(clean_env(ELUSION_GAMEDATA=CATALOGUE))
check("control, no .env: the database is the default beside the code",
      seen.get("db") == os.path.join(CODE, "elusion.db"), (seen, result.stderr[-400:]))
check("control, no .env: no proxy count", seen.get("hops") == 0, seen)
for leftover in ("elusion.db", "elusion.db-wal", "elusion.db-shm"):
    if os.path.exists(os.path.join(CODE, leftover)):
        os.remove(os.path.join(CODE, leftover))

write_env(CODE, ENV_TEXT)
result, seen = probe(clean_env())
check("app.py boots with only a .env", result.returncode == 0 and seen,
      result.stderr[-600:])
check("ELUSION_DB from the .env is the database it opened",
      seen.get("db") == LIVE_DB, seen.get("db"))
check("...and that file exists now: init_db() ran there, not beside the code",
      os.path.exists(LIVE_DB) and not os.path.exists(os.path.join(CODE, "elusion.db")),
      sorted(os.listdir(CODE)))
check("ELUSION_TRUSTED_PROXIES from the .env is the proxy count", seen.get("hops") == 1, seen)
check("ELUSION_GAMEDATA from the .env is the catalogue gamedata read",
      seen.get("catalogue") == CATALOGUE and seen.get("items", 0) > 0, seen)
check("ELUSION_OWNER from the .env still works", seen.get("owner") == "envfile_owner", seen)
check("an account registers against that database", seen.get("register") in (200, 201), seen)
check("the boot log names what the .env set and shows no value from it",
      "ELUSION_DB" in result.stdout and "not-a-real-secret-4471" not in result.stdout + result.stderr,
      result.stdout[:400])

result = boot_wsgi(clean_env())
check("wsgi.py's preflight passes with only a .env", result.returncode == 0, result.stderr[-600:])
check("...and judges the proxy count the .env set",
      "preflight passed (trusted proxy hops: 1)" in result.stderr, result.stderr[-600:])
check("...and does not warn that the owner the .env names is missing",
      "ELUSION_OWNER is not set" not in result.stderr, result.stderr[-600:])

result, seen = probe(clean_env(ELUSION_TRUSTED_PROXIES="2"))
check("a real environment variable beats the .env in app.py", seen.get("hops") == 2, seen)
result = boot_wsgi(clean_env(ELUSION_TRUSTED_PROXIES="2"))
check("...and in wsgi.py's preflight",
      "preflight passed (trusted proxy hops: 2)" in result.stderr, result.stderr[-600:])


# =============================================================================
section("D  a backup is one file, and the live database stays WAL")
# =============================================================================

def wal_source(path, rows=3):
    """A database in WAL mode with rows still in its -wal, and the connection
    left open so the -wal is not folded in on close - the live server's shape."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    for i in range(rows):
        con.execute("INSERT INTO users (username) VALUES (?)", ("player%d" % i,))
    con.commit()
    return con


def sidecars_of(path):
    return [path + s for s in backup_db.SIDECARS if os.path.exists(path + s)]


SRC = scratch("d", "src", "elusion.db")
OUT = scratch("d", "out", "x")[:-2]
live = wal_source(SRC)
check("the source is a WAL database with unfolded pages in its -wal (the test's premise)",
      live.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
      and os.path.getsize(SRC + "-wal") > 0)

copy = backup_db.make_backup(SRC, OUT)
check("make_backup() writes a copy whose header says rollback journal, not WAL",
      not backup_db.is_wal_file(copy))
ok, detail = backup_db.verify(copy)
check("the copy verifies, with every row - including the ones still in the -wal",
      ok and "3 user rows" in detail, detail)
check("and verifying it left NO -wal or -shm beside it (the first night's bug)",
      sidecars_of(copy) == [], sidecars_of(copy))
check("the live database is still WAL afterwards",
      live.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
live.execute("INSERT INTO users (username) VALUES ('after')")
live.commit()
check("and still takes writes", live.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4)

wal_copy = scratch("d", "walcopy.db")
c = sqlite3.connect(wal_copy)
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE users (id INTEGER)")
c.commit()
c.close()
ok, detail = backup_db.verify(wal_copy)
check("verify() refuses a copy still in WAL mode", not ok and "WAL" in detail, detail)
check("...and refuses it WITHOUT opening it, so it grows no sidecars doing so",
      sidecars_of(wal_copy) == [], sidecars_of(wal_copy))

cli_out = scratch("d", "cli", "x")[:-2]
result = subprocess.run([sys.executable, os.path.join(HERE, "backup_db.py"), "--db", SRC,
                         "--out", cli_out, "--keep", "14"],
                        capture_output=True, text=True, timeout=60)
check("the command a cron runs exits 0 and says OK",
      result.returncode == 0 and "[BACKUP] OK" in result.stdout, result.stdout + result.stderr)
check("and leaves exactly one file in the folder",
      len(os.listdir(cli_out)) == 1 and os.listdir(cli_out)[0].endswith(".db"),
      os.listdir(cli_out))
live.close()


# =============================================================================
section("E  prune and tidy")
# =============================================================================

def make_copy(directory, name, mtime, wal=False, rows=1):
    """A backup file by name. wal=True makes the old kind and opens it the way
    the old verify() did, which is what left its sidecars behind."""
    path = os.path.join(directory, name)
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE users (id INTEGER)")
    for _ in range(rows):
        con.execute("INSERT INTO users VALUES (1)")
    con.commit()
    con.close()
    if wal:
        ro = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        ro.execute("SELECT COUNT(*) FROM users").fetchone()
        ro.close()
    os.utime(path, (mtime, mtime))
    return path


def make_copy_with_live_wal(directory, name, mtime, rows):
    """An old WAL-mode copy whose rows are ONLY in its -wal: copied while a
    writer still held it, so the main file has not had them folded in. Settling
    it has to keep them; deleting its sidecars would lose every one."""
    origin = scratch("e_origin", name)
    con = sqlite3.connect(origin)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE users (id INTEGER)")
    for _ in range(rows):
        con.execute("INSERT INTO users VALUES (1)")
    con.commit()
    path = os.path.join(directory, name)
    shutil.copy(origin, path)
    shutil.copy(origin + "-wal", path + "-wal")
    con.close()
    os.utime(path, (mtime, mtime))
    return path


def user_rows(path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return "unreadable: %s" % exc


P = scratch("e", "x")[:-2]
now = time.time()
oldest = make_copy(P, "elusion-20260101-000000.db", now - 300, wal=True)
middle = make_copy_with_live_wal(P, "elusion-20260102-000000.db", now - 200, rows=7)
middle_mtime = os.stat(middle).st_mtime_ns
check("the copy's rows are only in its -wal (the test's premise)",
      os.path.getsize(middle + "-wal") > 0 and backup_db.is_wal_file(middle))
newest = make_copy(P, "elusion-20260103-000000.db", now - 100)
check("the old kind of copy really did leave sidecars (the test's premise)",
      sidecars_of(oldest) and sidecars_of(middle), (sidecars_of(oldest), sidecars_of(middle)))

for orphan in ("elusion-20250101-000000.db-wal", "elusion-20250101-000000.db-shm"):
    open(os.path.join(P, orphan), "w").close()
# NOT THIS SCRIPT'S, and some of them look like leftovers: a -wal or -shm with
# no database beside it, under another database's name. A live elusion.db-wal
# holds committed transactions, so a sweep that matched on "-wal" alone would be
# deleting somebody's data. Only names this script writes are its business.
others = ["elusion-test-20250101-000000.db", "elusion-test-20250101-000000.db-wal",
          "elusion-test-20240101-000000.db-shm", "other-20250101-000000.db-wal",
          "elusion.db-wal", "elusion.db-shm", "notes.txt", "elusion-20260101-000000.db.bak"]
for name in others:
    with open(os.path.join(P, name), "w") as handle:
        handle.write("not this script's")
    os.utime(os.path.join(P, name), (now - 1000, now - 1000))

def untouched(name):
    try:
        with open(os.path.join(P, name)) as handle:
            return handle.read() == "not this script's"
    except OSError:
        return False


removed, kept = backup_db.prune(P, "elusion", 2)
check("prune keeps the newest `keep` and removes the oldest",
      (removed, kept) == (1, 2) and not os.path.exists(oldest)
      and os.path.exists(middle) and os.path.exists(newest), (removed, kept))
check("...and the removed copy's -wal and -shm go with it", sidecars_of(oldest) == [],
      sidecars_of(oldest))
check("prune counts only this database's backups: elusion-test-<stamp>.db is "
      "another database's and is not one of the two kept",
      untouched("elusion-test-20250101-000000.db"))

settled, swept = backup_db.tidy(P, "elusion")
check("tidy settles the kept copy that still had sidecars", settled == 1, settled)
check("...which is one file now, not WAL", sidecars_of(middle) == [] and not backup_db.is_wal_file(middle),
      sidecars_of(middle))
check("...with every row it held, the ones from its -wal folded in",
      user_rows(middle) == 7, user_rows(middle))
check("...and its mtime unchanged, so it does not jump the queue for prune or the drill",
      os.stat(middle).st_mtime_ns == middle_mtime)
check("tidy removes sidecars whose backup is gone", swept == 2 and not any(
      os.path.exists(os.path.join(P, o)) for o in
      ("elusion-20250101-000000.db-wal", "elusion-20250101-000000.db-shm")), swept)
check("nothing that is not named like this database's backups was touched - "
      "not even a -wal or -shm with no database beside it",
      all(untouched(n) for n in others),
      [n for n in others if not untouched(n)])
check("a second tidy finds nothing to do", backup_db.tidy(P, "elusion") == (0, 0))

removed, kept = backup_db.prune(P, "elusion", 0)
check("keep=0 keeps everything", removed == 0 and kept == 2, (removed, kept))


# =============================================================================
section("F  restore_drill.py restores one of the new copies")
# =============================================================================
# THE ROUND TRIP, not just "the file opens": restore_drill.py takes a fresh
# backup with backup_db.py, then serves the copy with a real server, registers,
# logs in and checks the account from section C survived. A single-file copy
# that the server could not run would fail here.

result = subprocess.run([sys.executable, os.path.join(HERE, "restore_drill.py"), "--db", LIVE_DB],
                        cwd=ELSEWHERE, env=clean_env(), capture_output=True, text=True, timeout=180)
check("restore_drill.py passes on a backup of the database section C made",
      result.returncode == 0, (result.stdout + result.stderr)[-900:])
check("...and the copy it drilled was a single file",
      "[BACKUP] OK" in result.stdout and "WAL" not in result.stderr,
      result.stdout[-400:])


shutil.rmtree(SCRATCH, ignore_errors=True)

print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
