"""
THE CONNECTION IS KEPT (0.76.0): each worker thread hands its SQLite
connection on to its next request instead of opening one a request.
Run: python3 test_dbkeep.py

WHY. The first statement on a new connection reads and parses the whole
schema - 0.8 ms, against 4 us for the query it was opened for. At 128 players
(swarm.py) that was about a third of the API's time. app.py, THE CONNECTION IS
KEPT, has the measurement and the rules.

WHAT THIS GUARDS. That a kept connection is handed on exactly as clean as a
fresh one was: no read snapshot left open by a half-read cursor (the next
request would read the past), no transaction or write lock left held, nothing
a failed request wrote, never another thread's or another process's, never a
database other than the one ELUSION_DB names now - and that it is faster, or
there is no reason for any of it.

Runs app.py in this process against a throwaway database (testdir.py).
"""
import importlib.util
import os
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_dbkeep_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "keepowner"
os.environ.pop("ELUSION_DB_FRESH", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
app = app_module.app
client = app.test_client()

passed = failed = 0
failures = []


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n=== %s ===\n" % title)


def outside():
    """A connection of the test's own, as another process would hold one."""
    con = sqlite3.connect(DB_PATH, timeout=0)
    con.row_factory = sqlite3.Row
    return con


def setting(key, value):
    con = outside()
    con.execute("INSERT OR REPLACE INTO server_settings (key, value) VALUES (?, ?)", (key, value))
    con.commit()
    con.close()


def in_request(work, method="GET", path="/api/player/status"):
    """Run work(db) inside a request context of its own; what it returns."""
    with app.test_request_context(path, method=method):
        return work(app_module.get_db())


# =============================================================================
section("K-1 A THREAD'S CONNECTION IS HANDED ON")
# =============================================================================
first = in_request(lambda db: db)
second = in_request(lambda db: db)
check("two requests on one thread are handed the same connection", first is second)
pragmas = in_request(lambda db: [db.execute("PRAGMA %s" % p).fetchone()[0]
                                 for p in ("foreign_keys", "busy_timeout", "synchronous", "journal_mode")])
check("  with what a fresh one is opened with: foreign keys, the busy wait, NORMAL, WAL",
      pragmas == [1, 5000, 1, "wal"], pragmas)
client.get("/api/status")
through_routes = app_module._KEPT.db
client.get("/api/status")
check("real requests through the routes keep theirs the same way",
      through_routes is not None and app_module._KEPT.db is through_routes)
check("  rows still come back by name", in_request(lambda db: db.execute("SELECT 1 AS one").fetchone()["one"]) == 1)

# =============================================================================
section("K-2 HANDED ON CLEAN")
# =============================================================================
setting("keep_probe", "before")
held = {}


def half_read(db):
    cursor = db.execute("SELECT key, value FROM server_settings ORDER BY key")
    cursor.fetchone()
    held["cursor"] = cursor          # still referenced after the request
    return db.execute("SELECT value FROM server_settings WHERE key = 'keep_probe'").fetchone()["value"]


check("(a request reads, leaving a cursor half read and still held)", in_request(half_read) == "before")
setting("keep_probe", "after")
seen = in_request(lambda db: db.execute("SELECT value FROM server_settings WHERE key = 'keep_probe'")
                  .fetchone()["value"])
check("the next request reads what was written since, not the snapshot the half-read cursor held",
      seen == "after", seen)
try:
    held["cursor"].fetchone()
    still_open = True
except sqlite3.ProgrammingError:
    still_open = False
check("  because every cursor the request made was closed at its end", not still_open)


def lock_and_leave(db):
    return db.in_transaction


check("(a write request holds the write lock from its first read)",
      in_request(lock_and_leave, "POST", "/api/combat/kill") is True)
check("  and lets go of it at its end, though its connection is kept",
      app_module._KEPT.db is not None and not app_module._KEPT.db.in_transaction)
probe = outside()
try:
    probe.execute("BEGIN IMMEDIATE")
    probe.rollback()
    free = True
except sqlite3.OperationalError:
    free = False
probe.close()
check("  so another connection takes the write lock at once", free)
check("a kept connection takes the lock again for the next write request",
      in_request(lock_and_leave, "PUT", "/api/save") is True)
check("  and not for a read", in_request(lock_and_leave) is False)


def write_and_fail(db):
    db.execute("INSERT OR REPLACE INTO server_settings (key, value) VALUES ('keep_ghost', 'written')")
    raise RuntimeError("the route failed before it committed")


try:
    in_request(write_and_fail, "POST", "/api/combat/kill")
except RuntimeError:
    pass
con = outside()
ghost = con.execute("SELECT value FROM server_settings WHERE key = 'keep_ghost'").fetchone()
con.close()
check("what a failed request wrote and never committed is gone, for everyone", ghost is None, ghost)
check("  and for the next request on its connection",
      in_request(lambda db: db.execute("SELECT 1 FROM server_settings WHERE key = 'keep_ghost'").fetchone()) is None)


def close_it(db):
    db.close()


in_request(close_it)
working = in_request(lambda db: db.execute("SELECT 1").fetchone()[0])
check("a connection a request closed is not handed on - the next opens a fresh one", working == 1)

# =============================================================================
section("K-3 ONLY EVER ITS OWN")
# =============================================================================
outer_db = {}


def nested(db):
    # A second app context on the same thread (a helper that makes its own):
    # a g of its own, so a get_db() of its own, and a teardown of its own.
    reading = db.execute("SELECT key FROM server_settings ORDER BY key")
    reading.fetchone()
    with app.app_context():
        inner = app_module.get_db()
        outer_db["inner_is_outer"] = inner is db
        inner.execute("SELECT 1").fetchone()
    try:
        reading.fetchone()
        outer_db["outer_untouched"] = True
    except sqlite3.ProgrammingError:
        outer_db["outer_untouched"] = False
    return db.execute("SELECT 1").fetchone()[0]


check("(a context inside a request, with a g of its own)", in_request(nested) == 1)
check("  gets a connection of its own - the kept one is out while in use", outer_db.get("inner_is_outer") is False,
      outer_db)
check("  and its end does not touch the outer one's (a cursor still mid-read)", outer_db.get("outer_untouched") is True,
      outer_db)
check("  and one of the two is kept afterwards, working",
      app_module._KEPT.db is not None and in_request(lambda db: db.execute("SELECT 2").fetchone()[0]) == 2)

mine = in_request(lambda db: db)
theirs = {}


def other_thread():
    theirs["db"] = in_request(lambda db: db)
    theirs["again"] = in_request(lambda db: db)


t = threading.Thread(target=other_thread)
t.start()
t.join()
check("another thread keeps a connection of its own, never this one's",
      theirs.get("db") is not None and theirs["db"] is theirs["again"] and theirs["db"] is not mine)

kept = app_module._KEPT.db
pid, path, dev, ino = kept.opened_as
kept.opened_as = (pid + 1, path, dev, ino)
check("one opened by another process (a worker forked from it) is not used",
      in_request(lambda db: db) is not kept)
kept = app_module._KEPT.db
kept.opened_as = (pid, path, dev, ino + 1)
check("one opened on a file since put back (a restore: a new file at the path) is not used",
      in_request(lambda db: db) is not kept)

other_path = os.path.join(testdir.PATH, "elusion_dbkeep_other.db")
src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(other_path)
src.backup(dst)
src.close()
dst.execute("INSERT OR REPLACE INTO server_settings (key, value) VALUES ('keep_probe', 'other file')")
dst.commit()
dst.close()
real_path = app_module.DB_PATH
app_module.DB_PATH = other_path
try:
    read_there = in_request(lambda db: db.execute("SELECT value FROM server_settings WHERE key = 'keep_probe'")
                            .fetchone()["value"])
finally:
    app_module.DB_PATH = real_path
check("a different ELUSION_DB opens that database, not the one kept", read_there == "other file", read_there)
read_back = in_request(lambda db: db.execute("SELECT value FROM server_settings WHERE key = 'keep_probe'")
                       .fetchone()["value"])
check("  and back again, this one", read_back == "after", read_back)

kept_before = app_module._KEPT.db
app_module.DB_FRESH = True
try:
    a, b = in_request(lambda db: db), in_request(lambda db: db)
    check("ELUSION_DB_FRESH=1: a connection a request again, and neither kept",
          a is not b and kept_before not in (a, b) and app_module._KEPT.db is kept_before)
finally:
    app_module.DB_FRESH = False
check("the switch is read from the environment", "ELUSION_DB_FRESH" in open(os.path.join(HERE, "app.py")).read())

# =============================================================================
section("K-4 AND IT IS WHY: FASTER")
# =============================================================================


def time_requests(n):
    start = time.perf_counter()
    for _ in range(n):
        in_request(lambda db: db.execute("SELECT value FROM server_settings WHERE key = 'keep_probe'").fetchone())
    return (time.perf_counter() - start) / n * 1000.0


in_request(lambda db: None)
kept_ms = time_requests(200)
app_module.DB_FRESH = True
try:
    fresh_ms = time_requests(200)
finally:
    app_module.DB_FRESH = False
print("  (a request's connection and one read: kept %.3f ms, fresh %.3f ms)" % (kept_ms, fresh_ms))
check("a request with a kept connection costs well under half one with a fresh one",
      kept_ms * 2 < fresh_ms, (round(kept_ms, 3), round(fresh_ms, 3)))

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
if failures:
    for f in failures:
        print("  - " + f)
sys.exit(1 if failed else 0)
