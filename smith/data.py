"""smith/data.py - the SQL. One table is this package's: smith_ranks, the Royal
Certifications a character has earned (schema.py). saves and carry_items are
app.py's (init_db()) and are only read here: the gold moves through
gold_delta(), the materials through the carry's grant and the cell through its
one-cell rewrite, all handed in. No commits: the routes make them."""


def character(db, user_id, slot):
    """The character's level and carried gold, or None for an empty slot."""
    return db.execute("SELECT level, gold FROM saves WHERE user_id = ? AND slot = ?",
                      (user_id, slot)).fetchone()


def earned(db, user_id, slot):
    """{rank: earned_at} for every certification this character holds."""
    return {int(r["rank"]): int(r["earned_at"]) for r in db.execute(
        "SELECT rank, earned_at FROM smith_ranks WHERE user_id = ? AND slot = ?", (user_id, slot))}


def earn(db, user_id, slot, rank, now):
    """Record a certification. No commit - the certify's own."""
    db.execute("INSERT INTO smith_ranks (user_id, slot, rank, earned_at) VALUES (?, ?, ?, ?)",
               (user_id, slot, int(rank), int(now)))


def held(db, user_id, slot):
    """{item_id: how many} across every carried cell, bag and hotbar - what a
    temper or a certification can take from."""
    return {r["item_id"]: int(r["n"]) for r in db.execute(
        "SELECT item_id, SUM(quantity) AS n FROM carry_items WHERE user_id = ? AND slot = ?"
        " GROUP BY item_id", (user_id, slot))}
