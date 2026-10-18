"""switches/data.py - the SQL: who is in the world as the server closes, and
who threw a switch and when. Both tables are the core's (init_db()) and are
only read here: a switch is written through app.py's set_server_setting(), and
a session ended through app.py's own sign-out. No commits: the routes make
them."""


def online_count(db, cutoff):
    """How many sessions a client was heard on since `cutoff`.

    WHO IS ACTUALLY IN THE WORLD, not who holds a session - a token lasts
    thirty days, so counting sessions would report a crowd that went home
    weeks ago."""
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE last_seen_at > ?",
        (cutoff,),
    ).fetchone()["n"] or 0)


def stamp(db, key):
    """The setting's row - who wrote it last (updated_by) and when
    (updated_at) - or None when it was never written."""
    return db.execute("SELECT updated_by, updated_at FROM server_settings WHERE key = ?",
                      (key,)).fetchone()
