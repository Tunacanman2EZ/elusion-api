"""death/data.py - the SQL. saves, carry_items and accounts are app.py's tables
(init_db()). The money a death costs moves through app.py's
ledgers, handed in - never a bare UPDATE on a balance. What is written here: the
refilled pools, a full death's lost bag and gear, and the death score. The
grant the healing check reads is written through status/'s note_refill(),
handed in. No commits: the routes make them."""


def the_dead(db, user_id, slot):
    """class_id, level and equipment (what the maxima are derived from), hp
    (dead or not) and the carried gold - or None for no such character."""
    return db.execute(
        "SELECT class_id, level, equipment, hp, gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def add_score(db, user_id, scored):
    db.execute("UPDATE accounts SET score = score + ? WHERE user_id = ?", (scored, user_id))


_REFILL = ("UPDATE saves SET hp = ?, mana = ?, stamina = ?, "
           "max_hp = ?, max_mana = ?, max_stamina = ?, updated_at = ?, pools_at = ? "
           "WHERE user_id = ? AND slot = ?")
_REFILL_BARE = ("UPDATE saves SET equipment = '{}', hp = ?, mana = ?, stamina = ?, "
                "max_hp = ?, max_mana = ?, max_stamina = ?, updated_at = ?, pools_at = ? "
                "WHERE user_id = ? AND slot = ?")


def refill(db, user_id, slot, derived, now, strip_gear=False):
    """All three pools to the maxima, and the maxima written - with nothing
    worn, for a full death."""
    db.execute(
        _REFILL_BARE if strip_gear else _REFILL,
        (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         now, now, user_id, slot),
    )


def drop_carry(db, user_id, slot):
    """THE CARRY BAG, SERVER SIDE - every cell of it, hotbar included."""
    db.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))


