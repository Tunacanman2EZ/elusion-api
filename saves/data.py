"""saves/data.py - the SQL. saves is the table a character is (app.py's
init_db()); character_deletions keeps what a deletion took. Deleting a
character also clears its rows in the tables other areas own - its carry,
skills, heal grants, save history, kill record and loot bags - because none of
them means anything without the character. No commits: the routes make them."""


def all_of(db, user_id):
    """Every character an account has, by slot."""
    return db.execute(
        "SELECT * FROM saves WHERE user_id = ? ORDER BY slot",
        (user_id,),
    ).fetchall()


def row_of(db, user_id, slot):
    """One character's whole row, or None."""
    return db.execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot),
    ).fetchone()


def level_of(db, user_id, slot):
    """The level, or None for a character that does not exist yet."""
    return db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()


def upsert(db, user_id, slot, class_id, name, level, area, active_pet_id, equipment, explored, now,
           area_sent, pet_sent, equipment_sent, explored_sent):
    """
    Write one slot: made if it is new, updated if it is not.

    ON CONFLICT rather than DELETE-then-INSERT: an upsert is one statement,
    so there is no window where the slot exists in neither state.

    area, active_pet_id, equipment and explored use excluded.* only when the
    caller actually sent the key; otherwise each keeps whatever the row already
    holds. On a fresh INSERT there is nothing to keep, so each lands as given.
    """
    db.execute(
        """
        INSERT INTO saves (user_id, slot, class_id, name, level, area, active_pet_id,
                           equipment, explored, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, slot) DO UPDATE SET
            class_id      = excluded.class_id,
            name          = excluded.name,
            -- level is NOT taken from excluded. The row keeps the level the
            -- server granted through /api/combat/kill; the 1 in VALUES only
            -- ever applies to a brand new character.
            level         = saves.level,
            area          = CASE WHEN ? THEN excluded.area
                                 ELSE saves.area END,
            active_pet_id = CASE WHEN ? THEN excluded.active_pet_id
                                 ELSE saves.active_pet_id END,
            equipment     = CASE WHEN ? THEN excluded.equipment
                                 ELSE saves.equipment END,
            explored      = CASE WHEN ? THEN excluded.explored
                                 ELSE saves.explored END,
            updated_at    = excluded.updated_at
        """,
        (user_id, slot, class_id, name, level, area, active_pet_id,
         equipment, explored, now,
         1 if area_sent else 0,
         1 if pet_sent else 0,
         1 if equipment_sent else 0,
         1 if explored_sent else 0),
    )


def wearer(db, user_id, slot):
    """class_id, level and equipment - what the maxima are derived from."""
    return db.execute(
        "SELECT class_id, level, equipment FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def fill(db, user_id, slot, derived, xp_to_next, now):
    """A new character's pools: full, at the maxima derived for it."""
    db.execute(
        """
        UPDATE saves
           SET max_hp = ?, hp = ?, max_mana = ?, mana = ?,
               max_stamina = ?, stamina = ?, xp_to_next = ?, pools_at = ?
         WHERE user_id = ? AND slot = ?
        """,
        (derived["max_hp"], derived["max_hp"],
         derived["max_mana"], derived["max_mana"],
         derived["max_stamina"], derived["max_stamina"],
         xp_to_next, now, user_id, slot),
    )


def hold_to(db, user_id, slot, derived):
    """The maxima as derived, and every pool brought down to them - never up."""
    db.execute(
        "UPDATE saves SET max_hp = ?, max_mana = ?, max_stamina = ?, "
        "hp = MIN(hp, ?), mana = MIN(mana, ?), stamina = MIN(stamina, ?) "
        "WHERE user_id = ? AND slot = ?",
        (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         user_id, slot),
    )


def equipment_of(db, user_id, slot):
    """The equipment column as stored."""
    return db.execute(
        "SELECT equipment FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot)).fetchone()["equipment"]


def identity(db, user_id, slot):
    """What GET /api/character answers besides the vitals, the carry and the
    skills."""
    return db.execute(
        "SELECT class_id, name, area, active_pet_id, bank_gold, equipment "
        "FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def carried(db, user_id, slot):
    """The carry, cell by cell, as plain dicts - for the deletion's copy."""
    return [dict(r) for r in db.execute(
        "SELECT position, item_id, quantity FROM carry_items"
        " WHERE user_id = ? AND slot = ? ORDER BY position", (user_id, slot))]


def skills_of(db, user_id, slot):
    """The skills, as plain dicts - for the deletion's copy."""
    return [dict(r) for r in db.execute(
        "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ?"
        " ORDER BY skill_id", (user_id, slot))]


def keep_copy(db, user_id, slot, row, gold, now, snapshot, kept):
    """Keep what a deletion took, and only the account's last `kept`."""
    db.execute(
        "INSERT INTO character_deletions"
        " (user_id, slot, class_id, name, level, gold, deleted_at, snapshot)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, slot, row["class_id"], row["name"], int(row["level"] or 1), gold, now, snapshot),
    )
    db.execute(
        "DELETE FROM character_deletions WHERE user_id = ? AND id NOT IN ("
        " SELECT id FROM character_deletions WHERE user_id = ?"
        " ORDER BY deleted_at DESC, id DESC LIMIT ?)",
        (user_id, user_id, kept),
    )


def drop(db, user_id, slot):
    """
    Every row of the character's own: its carry, skills, heal grants, save
    history and kill record, its loot bags (loot_bag_items cascades), and last
    the save itself.

    AND ITS SAVE HISTORY: a snapshot names a slot, not a character, and the
    next character made in this slot is somebody else. The copy keep_copy()
    wrote is what is kept of this one. And its kill record: the next character
    in this slot starts its own.
    """
    for table in ("carry_items", "skills", "consume_grants", "save_snapshots", "kill_tally"):
        db.execute("DELETE FROM %s WHERE user_id = ? AND slot = ?" % table, (user_id, slot))
    db.execute("DELETE FROM loot_bags WHERE user_id = ? AND slot = ?", (user_id, slot))
    db.execute("DELETE FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot))
