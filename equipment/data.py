"""equipment/data.py - the SQL. saves is app.py's table (init_db()); this
reads a character's class, level and equipment and writes two things on it:
what is worn, and the hp and mana ceilings what is worn implies. The item
itself moves through app.py's backpack grants, handed in. No commits: the
routes make them."""
import json


def wearer(db, user_id, slot):
    """class_id, level and equipment - all three of what app.py's
    _derived_stats() reads - or None for no such character."""
    return db.execute(
        "SELECT class_id, level, equipment FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def wear(db, user_id, slot, worn, now):
    db.execute(
        "UPDATE saves SET equipment = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (json.dumps(worn), now, user_id, slot),
    )


def hold_to(db, user_id, slot, max_hp, max_mana):
    """The new ceilings, and hp and mana brought down to them if they sit
    above. NEVER RAISES THE POOLS."""
    db.execute(
        "UPDATE saves SET max_hp = ?, max_mana = ?, hp = MIN(hp, ?), mana = MIN(mana, ?) "
        "WHERE user_id = ? AND slot = ?",
        (max_hp, max_mana, max_hp, max_mana, user_id, slot),
    )


def pools(db, user_id, slot):
    row = db.execute(
        "SELECT hp, max_hp, mana, max_mana FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()
    return {"hp": int(row["hp"]), "max_hp": int(row["max_hp"]),
            "mana": int(row["mana"]), "max_mana": int(row["max_mana"])}
