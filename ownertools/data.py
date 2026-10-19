"""ownertools/data.py - the SQL. The tables are app.py's (init_db()): the
saves, skills and carry a tool sets or a rollback puts back, the sessions a
change to somebody else's character ends, owner_gifts read for the gifts
report, the save snapshots read for the history, the pending teleports a game
acknowledges. What other areas share is not here but handed in: a snapshot
taken, a gift recorded, the gold ledger, the grants into the backpack. No
commits: the routes make them."""


def character(db, user_id, slot):
    """The character's whole row, or None for no such character."""
    return db.execute("SELECT * FROM saves WHERE user_id = ? AND slot = ?",
                      (user_id, slot)).fetchone()


def purse(db, user_id, slot):
    """The character's carried gold, as a row, or None."""
    return db.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = ?",
                      (user_id, slot)).fetchone()


def set_level(db, user_id, slot, level, xp_to_next, derived, now):
    """A level set, the way a level-up is: no XP into it, the curve's next
    step, the maxima derived for it and the pools full - one statement."""
    db.execute(
        """
        UPDATE saves
           SET level = ?, xp = 0, xp_to_next = ?,
               max_hp = ?, max_mana = ?, max_stamina = ?,
               hp = ?, mana = ?, stamina = ?, updated_at = ?
         WHERE user_id = ? AND slot = ?
        """,
        (level, xp_to_next,
         derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         now, user_id, slot),
    )


def end_sessions(db, user_id, now):
    """Sign the account out everywhere, so its game reloads what the server
    holds; how many sessions were still live, counted first, like a kick."""
    ended = int(db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND expires_at > ?",
        (user_id, now)).fetchone()["n"] or 0)
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return ended


def skill_levels(db, user_id, slot):
    """{skill_id: level} for every skill the character has a row for."""
    return {
        row["skill_id"]: int(row["level"])
        for row in db.execute("SELECT skill_id, level FROM skills"
                              " WHERE user_id = ? AND slot = ?", (user_id, slot))
    }


def set_skill(db, user_id, slot, skill_id, level):
    """One skill set to a level, with no XP into it - made if it was missing."""
    db.execute(
        """
        INSERT INTO skills (user_id, slot, skill_id, level, xp)
             VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(user_id, slot, skill_id)
          DO UPDATE SET level = excluded.level, xp = 0
        """,
        (user_id, slot, skill_id, level),
    )


def heard_lately(db, user_id, cutoff, now):
    """Whether one of the account's sessions was heard since `cutoff` and is
    still valid at `now` - whether a gift reaches them within a poll."""
    return db.execute(
        "SELECT 1 FROM sessions WHERE user_id = ? AND last_seen_at > ? AND expires_at > ? LIMIT 1",
        (user_id, cutoff, now),
    ).fetchone() is not None


def gift_sums(db, where="", params=()):
    row = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(gold), 0), COALESCE(SUM(lusions), 0),"
        " COALESCE(SUM(value), 0), COALESCE(SUM(sells_for), 0), MIN(at), MAX(at)"
        " FROM owner_gifts" + where, params).fetchone()
    return {"gifts": int(row[0]), "gold": int(row[1]), "lusions": int(row[2]),
            "item_value": int(row[3]), "item_sells_for": int(row[4]),
            "first_at": int(row[5]) if row[5] is not None else None,
            "last_at": int(row[6]) if row[6] is not None else None}


def gift_report(db, since_key, user_id=None, recent=20):
    """What the owner - and a co-owner - has given away and what it comes to,
    for GET /api/staff/gifts. `user_id` narrows it to one receiver; `since_key`
    is the server setting that says when the ledger began (app.py's
    OWNER_GIFTS_SINCE_KEY - the boot backfill writes it)."""
    one = " WHERE user_id = ?" if user_id is not None else ""
    params = (user_id,) if user_id is not None else ()
    joiner = " AND " if one else " WHERE "
    report = {
        "totals": gift_sums(db, one, params),
        "to_players": gift_sums(db, one + joiner + "(actor_id IS NULL OR user_id IS NOT actor_id)", params),
        "to_yourself": gift_sums(db, one + joiner + "user_id IS actor_id", params),
    }
    report["by_player"] = [
        {"username": r[0], "gifts": int(r[1]), "gold": int(r[2]), "lusions": int(r[3]),
         "item_value": int(r[4]), "item_sells_for": int(r[5]), "last_at": int(r[6])}
        for r in db.execute(
            "SELECT username, COUNT(*), SUM(gold), SUM(lusions), SUM(value), SUM(sells_for), MAX(at)"
            " FROM owner_gifts" + one + " GROUP BY COALESCE(user_id, -1), username"
            " ORDER BY SUM(gold) DESC, SUM(value) DESC, username LIMIT 50", params)
    ]
    # WHO GAVE IT (0.21.0): the owner, and a co-owner while the switch is on.
    report["by_giver"] = [
        {"username": r[0], "gifts": int(r[1]), "gold": int(r[2]), "lusions": int(r[3]),
         "item_value": int(r[4]), "item_sells_for": int(r[5]), "last_at": int(r[6])}
        for r in db.execute(
            "SELECT actor_name, COUNT(*), SUM(gold), SUM(lusions), SUM(value), SUM(sells_for), MAX(at)"
            " FROM owner_gifts" + one + " GROUP BY COALESCE(actor_id, -1), actor_name"
            " ORDER BY SUM(gold) DESC, SUM(value) DESC, actor_name LIMIT 20", params)
    ]
    report["by_item"] = [
        {"item_id": r[0], "kind": r[1], "quantity": int(r[2]), "gold": int(r[3]),
         "lusions": int(r[4]), "item_value": int(r[5])}
        for r in db.execute(
            "SELECT item_id, kind, SUM(quantity), SUM(gold), SUM(lusions), SUM(value)"
            " FROM owner_gifts" + one + " GROUP BY item_id, kind"
            " ORDER BY SUM(gold) + SUM(value) + SUM(lusions) DESC LIMIT 15", params)
    ]
    report["recent"] = [
        {"at": int(r[0]), "by": r[1], "username": r[2], "kind": r[3], "item_id": r[4],
         "quantity": int(r[5]), "gold": int(r[6]), "lusions": int(r[7]), "item_value": int(r[8]),
         "source": r[9]}
        for r in db.execute(
            "SELECT at, actor_name, username, kind, item_id, quantity, gold, lusions, value, source"
            # BY WHEN, not by id: the gifts read back from the logs were written
            # in one go at start-up, staff log first and gold ledger after.
            " FROM owner_gifts" + one + " ORDER BY at DESC, id DESC LIMIT ?", params + (int(recent),))
    ]
    # THE ECONOMY NOW, to set it beside: every gold in a purse or a bank, and
    # every gold the ledger says was ever made (gifts used, loot, sales).
    purses = db.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0]
    banks = db.execute("SELECT COALESCE(SUM(bank_gold), 0), COALESCE(SUM(lusions), 0) FROM accounts").fetchone()
    made = db.execute("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE delta > 0").fetchone()[0]
    gold_now = int(purses) + int(banks[0])
    report["economy"] = {"gold_now": gold_now, "lusions_now": int(banks[1]), "gold_ever_made": int(made)}
    report["share_of_gold_now"] = (round(report["totals"]["gold"] / gold_now, 4) if gold_now > 0 else None)
    since = db.execute("SELECT value FROM server_settings WHERE key = ?", (since_key,)).fetchone()
    report["ledger_since"] = int(since[0]) if since is not None and str(since[0]).isdigit() else None
    return report


def characters_of(db, user_id):
    """Every character the account has, by slot: its slot, name, class and level."""
    return [
        {"slot": int(r["slot"]), "name": r["name"], "class_id": r["class_id"],
         "level": int(r["level"] or 1)}
        for r in db.execute("SELECT slot, name, class_id, level FROM saves"
                            " WHERE user_id = ? ORDER BY slot", (user_id,))
    ]


def snapshots_of(db, user_id, slot):
    """The character's snapshots, newest first - what the history lists, never
    the JSON itself."""
    return db.execute(
        "SELECT id, taken_at, reason, level, gold, items FROM save_snapshots"
        " WHERE user_id = ? AND slot = ? ORDER BY taken_at DESC, id DESC",
        (user_id, slot)).fetchall()


def snapshot(db, snapshot_id, user_id):
    """One snapshot, BY ID AND ACCOUNT TOGETHER, or None."""
    return db.execute(
        "SELECT * FROM save_snapshots WHERE id = ? AND user_id = ?",
        (snapshot_id, user_id)).fetchone()


def restore_cells_and_skills(db, user_id, slot, carried, skills):
    """The character's carried cells and skills replaced by a snapshot's:
    `carried` is (position, item_id, quantity) rows, `skills` (skill_id,
    level, xp) rows."""
    db.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))
    if carried:
        db.executemany("INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
                       " VALUES (?, ?, ?, ?, ?)", [(user_id, slot) + tuple(c) for c in carried])
    db.execute("DELETE FROM skills WHERE user_id = ? AND slot = ?", (user_id, slot))
    if skills:
        db.executemany("INSERT INTO skills (user_id, slot, skill_id, level, xp)"
                       " VALUES (?, ?, ?, ?, ?)", [(user_id, slot) + tuple(s) for s in skills])


def put_back(db, user_id, slot, level, xp, xp_to_next, equipment, pet, now):
    """The level, XP, gear and pet a snapshot holds - and the trade or gift
    flag cleared, since the game reloads all of it anyway."""
    db.execute(
        "UPDATE saves SET level = ?, xp = ?, xp_to_next = ?, equipment = ?, active_pet_id = ?,"
        " resync_trade = NULL, updated_at = ? WHERE user_id = ? AND slot = ?",
        (level, xp, xp_to_next, equipment, pet, now, user_id, slot),
    )


def bring_pools_down(db, user_id, slot, derived, now):
    """THE MAXIMA FOLLOW THE OLD LEVEL AND GEAR; THE POOLS ONLY COME DOWN."""
    db.execute(
        "UPDATE saves SET max_hp = ?, max_mana = ?, max_stamina = ?,"
        " hp = MIN(hp, ?), mana = MIN(mana, ?), stamina = MIN(stamina, ?), pools_at = ?"
        " WHERE user_id = ? AND slot = ?",
        (derived["max_hp"], derived["max_mana"], derived["max_stamina"],
         derived["max_hp"], derived["max_mana"], derived["max_stamina"], now,
         user_id, slot),
    )


def every_account(db):
    """Every account, by id - who "everyone" moves."""
    return db.execute("SELECT id, username FROM users ORDER BY id").fetchall()


def clear_teleport(db, user_id, teleport_id):
    """The caller's own pending teleport cleared, matched on its id - whether
    there was one."""
    cursor = db.execute(
        "DELETE FROM pending_teleports WHERE user_id = ? AND id = ?",
        (user_id, teleport_id),
    )
    return cursor.rowcount > 0
