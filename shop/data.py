"""shop/data.py - the SQL: what a character carries in gold, read before a
purchase, and whether it already has a piece the shop gives away. Everything a
sale or a purchase writes goes through what register() is handed - the
backpack grant and the grid's cells (carry/), the gold ledger and the trade
hold (app.py) - so no table is written here."""
import json


def purse(db, user_id, slot):
    """The carried gold, or 0 for no such character."""
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return int(row["gold"]) if row is not None else 0


def where_held(db, user_id, slot, item_id):
    """
    Where this character already has one of exactly item_id: "carried" (its
    bag or its keys), "worn", or "banked" (its account's bank, which every
    character on the account reaches) - or None.

    FOR WHAT THE SHOP GIVES AWAY, one each (0.69.0). The bank counts because
    a free piece banked by one character is a piece the next one can take
    out; without it, claim, bank, claim fills a bank. The exact id, so a
    rolled drop of the same item does not stop anyone taking the free one.
    """
    if db.execute(
        "SELECT 1 FROM carry_items WHERE user_id = ? AND slot = ? AND item_id = ? LIMIT 1",
        (user_id, slot, item_id),
    ).fetchone() is not None:
        return "carried"
    row = db.execute(
        "SELECT equipment FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    try:
        worn = json.loads(row["equipment"] or "{}") if row is not None else {}
    except (TypeError, ValueError):
        worn = {}
    if isinstance(worn, dict) and item_id in [str(piece) for piece in worn.values()]:
        return "worn"
    if db.execute(
        "SELECT 1 FROM bank_items WHERE user_id = ? AND item_id = ? LIMIT 1",
        (user_id, item_id),
    ).fetchone() is not None:
        return "banked"
    return None
