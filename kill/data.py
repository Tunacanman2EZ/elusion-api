"""kill/data.py - the SQL: the character a kill is claimed for, how many of one
monster it was paid for lately, the level, the XP and the bucket banked in one
statement, and the kill's two records - kill_reports, kept two weeks, and
kill_tally, kept for good. (The level-up's refill is noted through status/'s
note_refill(), handed in.) The tables are this package's (schema.py). A paid
kill is the only thing that adds to either (a deleted character takes its
tally with it, in saves/, and the tally was seeded once at boot); many areas
read them - the kill record, the stats, the stirring lands, the events, the
insights, the staff desk, killwatch.py. No commits: the route makes one, for
the whole kill."""
from . import services


def character(db, user_id, slot):
    """What a kill needs to know about the character: its class, level and
    equipment, its XP, and its bucket. None for no such character."""
    return db.execute(
        "SELECT class_id, level, equipment, xp, xp_to_next, last_kill_at, kill_tokens FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def paid_recently(db, user_id, enemy_id, since):
    """How many of this monster the account was paid for since `since`.

    Out of kill_reports rather than a table of its own: it holds one row per
    PAID kill, indexed on (user_id, at), and a refused kill leaves no row."""
    recent = db.execute(
        "SELECT COUNT(*) AS n FROM kill_reports"
        " WHERE user_id = ? AND enemy_id = ? AND at >= ?",
        (user_id, enemy_id, since),
    ).fetchone()
    return int(recent["n"] or 0)


def bank(db, user_id, slot, level, xp, xp_to_next, derived, now_ms, tokens, now):
    """The level, the XP and the bucket, in ONE statement - and, when the kill
    levelled the character (`derived` is its new maxima, else None), the
    maxima with them."""
    if derived is None:
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next, now_ms, tokens, now, user_id, slot),
        )
    else:
        # One statement, so the level and the maxima it implies can never be
        # stored apart from each other.
        db.execute(
            """
            UPDATE saves
               SET level = ?, xp = ?, xp_to_next = ?,
                   max_hp = ?, max_mana = ?, max_stamina = ?,
                   last_kill_at = ?, kill_tokens = ?, updated_at = ?
             WHERE user_id = ? AND slot = ?
            """,
            (level, xp, xp_to_next,
             derived["max_hp"], derived["max_mana"], derived["max_stamina"],
             now_ms, tokens, now, user_id, slot),
        )


def record_kill(db, user_id, slot, enemy_id, rewards, level_at, now):
    """Write one row for this claimed kill, and prune the old ones - and count
    it on the kill record, which is never pruned.

    NO COMMIT - this shares combat_kill's transaction on purpose, so a kill is
    never banked without its record, and a record never exists for a kill that
    was rolled back.
    """
    db.execute(
        "INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, slot, str(enemy_id)[:64], int(rewards.get("xp", 0)),
         int(rewards.get("attack_xp", 0)), int(level_at), now),
    )
    db.execute("DELETE FROM kill_reports WHERE at < ?",
               (now - services.KILL_LOG_RETENTION_SECONDS,))
    db.execute(
        "INSERT INTO kill_tally (user_id, slot, enemy_id, kills, first_at, last_at)"
        " VALUES (?, ?, ?, 1, ?, ?)"
        " ON CONFLICT(user_id, slot, enemy_id) DO UPDATE SET"
        "   kills = kills + 1, last_at = excluded.last_at",
        (user_id, slot, str(enemy_id)[:64], now, now),
    )
