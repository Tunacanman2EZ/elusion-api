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

EXIT CODE is 0 only if the backup was written AND verified. A scheduler (cron,
Task Scheduler) that checks the exit code will know the night it fails, which is
the only night it matters.
"""

import argparse
import os
import sqlite3
import sys
import time


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
    finally:
        src.close()
        dst.close()
    return dest_path


def verify(backup_path):
    """A backup you have not opened is a guess. Prove this one is sound."""
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


def prune(out_dir, base, keep):
    """Keep the newest `keep` backups for this database; delete the rest."""
    prefix = base + "-"
    backups = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir)
         if f.startswith(prefix) and f.endswith(".db")),
        key=os.path.getmtime,
    )
    removed = 0
    for path in backups[:-keep] if keep > 0 else []:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed, max(0, len(backups) - removed)


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

    print("[BACKUP] OK  %s  (%s, %s)  in %.1fs"
          % (dest, human(os.path.getsize(dest)), detail, time.time() - started))
    print("[BACKUP] retention: %d kept, %d pruned (keep=%d)" % (kept, removed, args.keep))
    sys.exit(0)


if __name__ == "__main__":
    main()
