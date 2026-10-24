"""serverhealth/data.py - the SQL. Reads only: presence_health is
presence.py's to write (app.py's init_db() makes it), and this writes nothing.

THE WINDOW IS AN INDEX RANGE (idx_presence_health_at): a row a minute is
1,440 a day and 10,080 a week, read whole - a few hundred kilobytes at most.
A database from before the table answers with nothing.
"""
import os
import shutil
import sqlite3

_COLUMNS = ("at", "seconds", "connections", "players", "areas", "crowd", "ticks", "late_ticks", "frames_out",
            "bytes_out", "net_out", "cpu", "load1", "mem_free_mb")
_SELECT = "SELECT %s FROM presence_health" % ", ".join(_COLUMNS)


def _rows(db, sql, args=()):
    try:
        return [dict(zip(_COLUMNS, tuple(row))) for row in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def health_rows(db, since):
    """Every presence_health row from `since` on, oldest first."""
    return _rows(db, _SELECT + " WHERE at >= ? ORDER BY at, id", (int(since),))


def latest_health(db):
    """The newest presence_health row, or None."""
    rows = _rows(db, _SELECT + " ORDER BY at DESC, id DESC LIMIT 1")
    return rows[0] if rows else None


def disk(path):
    """(free bytes, total bytes) of the disk the database is on, and the
    database's own size with its WAL - or Nones where they cannot be read."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    try:
        usage = shutil.disk_usage(folder)
        free, total = int(usage.free), int(usage.total)
    except OSError:
        free = total = None
    size = 0
    found = False
    for name in (path, path + "-wal"):
        try:
            size += os.path.getsize(name)
            found = True
        except OSError:
            pass
    return free, total, (size if found else None)
