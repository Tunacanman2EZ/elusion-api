"""killrecord/data.py - the SQL, reads only. kill_tally is app.py's table
(init_db(), and _record_kill() writes it)."""


def mine(db, user_id, slot):
    """One character's rows: each monster it has killed, most first."""
    return db.execute(
        "SELECT enemy_id, kills, first_at, last_at FROM kill_tally"
        " WHERE user_id = ? AND slot = ? AND kills > 0"
        " ORDER BY kills DESC, enemy_id",
        (user_id, slot),
    ).fetchall()


def everyone(db, me):
    """Each monster's total and players, the account that has killed it most,
    and `me`'s own share.

    PER ACCOUNT, not per character: "who has killed the most" is a player,
    and four characters of one account are one person. Ties go to whoever
    started first (first_at), so the name on a monster does not flicker
    between two players level on kills."""
    return db.execute(
        "WITH per AS ("
        "  SELECT enemy_id, user_id, SUM(kills) AS k, MIN(first_at) AS f"
        "    FROM kill_tally WHERE kills > 0 GROUP BY enemy_id, user_id),"
        " ranked AS ("
        "  SELECT enemy_id, user_id, k,"
        "         ROW_NUMBER() OVER (PARTITION BY enemy_id ORDER BY k DESC, f ASC, user_id) AS n,"
        "         SUM(k) OVER (PARTITION BY enemy_id) AS total,"
        "         COUNT(*) OVER (PARTITION BY enemy_id) AS players"
        "    FROM per)"
        " SELECT r.enemy_id, r.total, r.players, r.k AS top_kills, u.username AS top,"
        "        COALESCE((SELECT k FROM per WHERE per.enemy_id = r.enemy_id AND per.user_id = ?), 0) AS yours"
        "   FROM ranked r JOIN users u ON u.id = r.user_id"
        "  WHERE r.n = 1"
        "  ORDER BY r.total DESC, r.enemy_id",
        (me,),
    ).fetchall()


def players(db):
    """How many accounts have a paid kill at all."""
    row = db.execute("SELECT COUNT(DISTINCT user_id) FROM kill_tally WHERE kills > 0").fetchone()
    return int(row[0] or 0)
