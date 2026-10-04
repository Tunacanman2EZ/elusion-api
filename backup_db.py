#!/usr/bin/env python3
"""
backup_db.py - a consistent, self-verifying backup of the Elusion database.

    python3 backup_db.py                         # back up $ELUSION_DB (or elusion.db)
    python3 backup_db.py --db /path/elusion.db --out /var/backups/elusion --keep 30

WHY THIS EXISTS
---------------
A strong server survives a dead disk. Everything else in SECURITY_NOTES.md is
about a hostile PLAYER; this is about a hostile WORLD - a bad sector, a fat-
fingered rm, a host that evaporates. The accounts, the saves, the gold ledger:
all of it lives in one SQLite file, and one file with no copy is one accident
from gone.

HOT AND CONSISTENT, not `cp`. Copying a SQLite file while the server is writing
can capture a half-finished transaction - a torn file that restores to garbage.
This uses SQLite's ONLINE BACKUP API (Connection.backup), which takes a
transaction-consistent snapshot even while players are mid-fight. No downtime,
no "close the server first".

SELF-VERIFYING. A backup you have never restored is a rumour. Every run reopens
the copy it just wrote, runs PRAGMA integrity_check, and reads a real row out of
it - so a backup that lands corrupt is caught here, tonight, not on the worst day
of next year when you reach for it.

RETENTION. Keeps the newest --keep backups and prunes the rest, so the folder
does not grow without bound. Timestamped names sort and read chronologically.

A STANDALONE FILE. The live database runs in WAL mode, and the online backup
API copies that setting along with the pages, so every copy used to be a WAL
database too. Opening one at all - even read-only, even just to verify it -
makes SQLite create a -wal and a -shm file beside it, so the first night on the
live server left elusion-<stamp>.db-wal and .db-shm next to the backup, and
pruning, which only knew about .db, would have left those behind for ever. Each
copy is switched to journal_mode=DELETE before it is closed: one file per
backup, which is what you want to copy off the machine. The live database is
never touched; its mode stays WAL. An older copy found with sidecars is settled
the same way (its mtime kept, so it does not jump the queue), and sidecars whose
backup is gone are removed.

EXIT CODE is 0 only if the backup was written AND verified. A scheduler (cron,
Task Scheduler) that checks the exit code will know the night it fails, which is
the only night it matters.
"""

import argparse
import os
import re
import sqlite3
import sys
import time

# What SQLite may leave beside a database file. A backup is one file; any of
# these beside it is either an older copy still in WAL mode or a leftover.
SIDECARS = ("-wal", "-shm", "-journal")


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def make_backup(src_path, out_dir):
    """Write a consistent snapshot of src_path into out_dir. Returns its path."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(os.path.basename(src_path))[0] or "elusion"
    dest_path = os.path.join(out_dir, "%s-%s.db" % (base, stamp))

    # Read-only source handle: a backup must never be able to change what it is
    # copying. The online backup API streams pages under a read lock and yields
    # a transaction-consistent image even while the server writes.
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True, timeout=30)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)               # the whole DB, consistently, in one call
        # ONE FILE, NOT THREE. The copy arrives in the live database's WAL mode,
        # and a WAL database grows a -wal and a -shm beside it whenever anyone
        # opens it - verify() below, a restore, a curious sqlite3 shell. DELETE
        # is SQLite's ordinary rollback journal, which leaves nothing behind
        # once the connection closes. This changes the COPY only; src is
        # read-only and could not change mode if it tried.
        mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise sqlite3.OperationalError(
                "the copy stayed in %s mode and would grow sidecar files" % mode)
    finally:
        src.close()
        dst.close()
    return dest_path


def is_wal_file(path):
    """True if the file's header says WAL. Read from the bytes, not by opening it
    with SQLite, because opening a WAL database is what creates the sidecars."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return (len(header) == 20 and header[:16] == b"SQLite format 3\x00"
            and (header[18] == 2 or header[19] == 2))


def verify(backup_path):
    """A backup you have not opened is a guess. Prove this one is sound."""
    # Before opening it: opening a WAL copy is what would create its sidecars.
    if is_wal_file(backup_path):
        return False, ("the copy is in WAL mode, so opening it grows -wal and -shm "
                       "files beside it - make_backup() should have switched it")
    con = sqlite3.connect("file:%s?mode=ro" % backup_path, uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            return False, "integrity_check said: %s" % ok
        # Read a real row back, so a structurally-valid-but-empty file is caught
        # too. users is the table nothing else works without.
        n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return True, "integrity ok, %d user rows readable" % n
    except sqlite3.Error as exc:
        return False, "could not read the backup: %s" % exc
    finally:
        con.close()


def backup_name(base):
    """The names this script writes for `base`, and nothing else: base, a dash,
    the stamp, .db. Exact on purpose - "elusion-" as a bare prefix also matches
    the backups of a database called elusion-test, and prune() would have
    counted them as this database's and deleted them."""
    return re.compile(r"^%s-\d{8}-\d{6}\.db$" % re.escape(base))


def remove_sidecars(path):
    """Delete whatever SQLite left beside `path`. Returns how many went."""
    gone = 0
    for suffix in SIDECARS:
        try:
            os.remove(path + suffix)
            gone += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("[BACKUP] warning: could not remove %s%s: %s" % (path, suffix, exc),
                  file=sys.stderr)
    return gone


def prune(out_dir, base, keep):
    """Keep the newest `keep` backups for this database; delete the rest, and
    whatever SQLite left beside each one deleted."""
    pattern = backup_name(base)
    backups = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir) if pattern.match(f)),
        key=lambda path: (os.path.getmtime(path), path),
    )
    removed = 0
    for path in backups[:-keep] if keep > 0 else []:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            continue
        remove_sidecars(path)
    return removed, max(0, len(backups) - removed)


def settle(path):
    """Switch an older WAL-mode copy to a single file, keeping its mtime.

    Through SQLite rather than by deleting files: a -wal can hold committed
    pages, and journal_mode=DELETE checkpoints them into the file before it
    removes the sidecars. The mtime is put back so prune() and
    restore_drill.py, which both order backups by it, still see this copy as
    old."""
    stat = os.stat(path)
    con = sqlite3.connect(path, timeout=5)
    try:
        mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    finally:
        con.close()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    if str(mode).lower() != "delete":
        raise sqlite3.OperationalError("stayed in %s mode" % mode)


def tidy(out_dir, base):
    """Leave this database's backups as one file each.

    A kept copy with sidecars beside it (written before make_backup() switched
    its copies, or opened by hand since) is settled. Sidecars whose backup is
    gone - pruned by an older version of this script, which only knew about
    .db - are removed. Nothing that is not named like this database's backups
    is touched. Returns (settled, removed)."""
    pattern = backup_name(base)
    names = set(os.listdir(out_dir))
    settled = removed = 0
    for name in sorted(names):
        path = os.path.join(out_dir, name)
        if pattern.match(name):
            if any(name + suffix in names for suffix in SIDECARS):
                try:
                    settle(path)
                    settled += 1
                except (OSError, sqlite3.Error) as exc:
                    print("[BACKUP] warning: could not settle %s: %s" % (path, exc),
                          file=sys.stderr)
            continue
        for suffix in SIDECARS:
            if name.endswith(suffix) and pattern.match(name[:-len(suffix)]):
                if name[:-len(suffix)] not in names:
                    removed += remove_sidecars(path[:-len(suffix)])
                break
    return settled, removed


def main():
    ap = argparse.ArgumentParser(description="Consistent, verified backup of the Elusion DB.")
    ap.add_argument("--db", default=os.environ.get("ELUSION_DB", "elusion.db"),
                    help="database to back up (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--out", default=os.environ.get("ELUSION_BACKUP_DIR", "backups"),
                    help="directory to write backups into (default: $ELUSION_BACKUP_DIR or ./backups)")
    ap.add_argument("--keep", type=int, default=14,
                    help="how many backups to retain (default: 14; 0 = keep all)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("[BACKUP] FAIL: no database at %s" % args.db, file=sys.stderr)
        sys.exit(2)

    started = time.time()
    try:
        dest = make_backup(args.db, args.out)
    except sqlite3.Error as exc:
        print("[BACKUP] FAIL: could not write the snapshot: %s" % exc, file=sys.stderr)
        sys.exit(1)

    ok, detail = verify(dest)
    if not ok:
        print("[BACKUP] FAIL: the snapshot did not verify (%s): %s" % (dest, detail),
              file=sys.stderr)
        # A backup that does not verify is worse than none, because it lies.
        # Remove it so a restore never reaches for it.
        try:
            os.remove(dest)
        except OSError:
            pass
        sys.exit(1)

    base = os.path.splitext(os.path.basename(args.db))[0] or "elusion"
    removed, kept = prune(args.out, base, args.keep)
    settled, swept = tidy(args.out, base)

    print("[BACKUP] OK  %s  (%s, %s)  in %.1fs"
          % (dest, human(os.path.getsize(dest)), detail, time.time() - started))
    print("[BACKUP] retention: %d kept, %d pruned (keep=%d)" % (kept, removed, args.keep))
    if settled or swept:
        print("[BACKUP] tidied: older copies made single files: %d, leftover "
              "-wal/-shm files removed: %d" % (settled, swept))
    sys.exit(0)


if __name__ == "__main__":
    main()
