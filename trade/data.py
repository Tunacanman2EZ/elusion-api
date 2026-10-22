"""trade/data.py - the SQL. trades, trade_items and trade_holds are this
package's tables (schema.py). Every write to them is here, and so is what
other areas ask of them: the owner's trade switch (switches/) counts the
open ones with open_count(), and a rollback and a character delete each
refuse a character with one open (find_open()). A trade also writes
the saves it moves gold between and the carry it moves items between, but
through app.py's own grants (handed in), and the gold through gold_delta()
only for the tax. No commits: the routes make them."""
import time

from . import services


# -- holds: a fresh mythic or Perfect waits before it can change hands ---------

def place_hold(db, catalogue, user_id, item_id, quantity=1, now=None):
    """A fresh find of a held kind: one hold per piece, from now. Prunes this
    account's own finished holds as it goes (no sweeper, and only its own rows,
    so no route reaches past the caller)."""
    if not services.held_kind(catalogue, item_id):
        return
    now = int(time.time()) if now is None else int(now)
    db.execute("DELETE FROM trade_holds WHERE user_id = ? AND held_until <= ?", (user_id, now))
    for _ in range(max(1, int(quantity))):
        db.execute(
            "INSERT INTO trade_holds (user_id, item_id, created_at, held_until)"
            " VALUES (?, ?, ?, ?)",
            (user_id, item_id, now, now + services.TRADE_HOLD_SECONDS),
        )


def move_hold(db, user_id, old_id, new_id, now=None):
    """
    A piece the smith tempered (0.74.0) keeps its hold under its new id: the
    hold with the most time left on `old_id`, if this account has one in
    force, is moved to `new_id` with its clock as it was. Returns whether one
    moved.

    BECAUSE THE HOLD IS ON THE ID. A tempered mythic is a new id, and without
    this the 48 hours a fresh find waits would end the moment it went on the
    anvil - a cheated kill laundered through the smith. Moving it, rather than
    holding the new id afresh, means an honest find tempered a day after it
    dropped waits one more day, not two. The latest hold moves, so a second
    piece of the same id found earlier is not held any longer than it was.
    """
    now = int(time.time()) if now is None else int(now)
    cur = db.execute(
        "UPDATE trade_holds SET item_id = ? WHERE id = ("
        " SELECT id FROM trade_holds WHERE user_id = ? AND item_id = ? AND held_until > ?"
        " ORDER BY held_until DESC, id DESC LIMIT 1) AND user_id = ?",
        (new_id, user_id, old_id, now, user_id),
    )
    return cur.rowcount > 0


def hold_refusal(db, catalogue, user_id, items, now=None):
    """None when every piece in `items` ({item_id: quantity}) may be traded by
    this account, else the sentence that says which may not and when it may.

    WHAT IS FREE IS WHAT IS OWNED LESS WHAT IS HELD, across every character's
    bag and the bank, because a held piece can be carried to another character
    through the bank and must not get out by the side door. Two pieces with the
    same id are the same piece, so it does not matter which of them is the new
    one - only how many are.

    AND A FREE PIECE NEVER (gamedata.is_free(), 0.69.0): what the shop gives
    away is the plain catalogue piece, kept by whoever took it. Asked here
    because this is the one check both the offer and the execution make."""
    now = int(time.time()) if now is None else int(now)
    for item_id in items:
        if catalogue.is_free(item_id):
            row = catalogue.item_row(item_id) or {}
            return services.free_words(row.get("display_name", item_id))
    for item_id, quantity in items.items():
        if not services.held_kind(catalogue, item_id):
            continue
        holds = db.execute(
            "SELECT COUNT(*) AS n, MIN(held_until) AS first FROM trade_holds"
            " WHERE user_id = ? AND item_id = ? AND held_until > ?",
            (user_id, item_id, now),
        ).fetchone()
        held = int(holds["n"] or 0)
        if held == 0:
            continue
        owned = int(db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM carry_items WHERE user_id = ? AND item_id = ?",
            (user_id, item_id)).fetchone()[0]) + int(db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM bank_items WHERE user_id = ? AND item_id = ?",
            (user_id, item_id)).fetchone()[0])
        if int(quantity) > owned - held:
            row = catalogue.item_row(item_id) or {}
            return services.hold_words(row.get("display_name", item_id), holds["first"], now)
    return None


# -- how many are open: the owner's switch asks --------------------------------

def open_count(db, now):
    """How many trades are open and not yet run out at `now` - what "a trade
    already open can still finish" covers when the owner stops trading."""
    return int(db.execute(
        "SELECT COUNT(*) FROM trades WHERE state = 'open' AND updated_at >= ?",
        (int(now) - services.TRADE_EXPIRY_SECONDS,),
    ).fetchone()[0])


# -- finding a trade: ALWAYS FROM THE CALLER ------------------------------------
# There is no route that lets a client name a trade (test_ownership.py, O-2):
# every one starts from the account asking.

def find_open(db, user_id):
    return db.execute(
        "SELECT * FROM trades WHERE state = 'open' AND (a_user = ? OR b_user = ?)",
        (user_id, user_id),
    ).fetchone()


def find_recent(db, user_id, limit, states=("done",)):
    """The caller's most recent trades in the given states, newest first.

    FROM THE CALLER, like find_open - there is no route that lets a client
    name a trade (test_ownership.py, O-2). The first page of find_page(),
    which holds the query."""
    return find_page(db, user_id, limit, states)


def find_page(db, user_id, limit, states=("done",), before=None):
    """
    One page of an account's trades in the given states, newest first.

    `before` is (updated_at, rowid) of the last row of the previous page, or
    None for the first. KEYSET, NEVER OFFSET: an offset re-reads every row it
    skips, so page fifty of a busy trader's history would cost fifty pages.

    ONE BOUNDED READ PER (SIDE, STATE). A player is side a of some trades and
    side b of others, and idx_trades_a_recent / idx_trades_b_recent are ordered
    by updated_at only WITHIN one state - so each (side, state) pair is its own
    branch that takes its newest `limit` straight off the index and stops. Only
    those are merged. A trader with ten thousand trades costs the same as one
    with ten; `state IN (...)` in a single branch would have sorted all of them.

    ROWID BREAKS THE TIE. updated_at is in seconds, and a trade that finished
    and the next one called off in the same second came back in either order.
    Rows are inserted as trades open, so the later rowid is the later trade;
    and every index carries the rowid as its last column, so this costs the
    index nothing.
    """
    branches = []
    params = []
    for side in ("a", "b"):
        for state in states:
            where = "%s_user = ? AND state = ?" % side
            branch_params = [user_id, state]
            if before is not None:
                where += " AND (updated_at < ? OR (updated_at = ? AND rowid < ?))"
                branch_params += [int(before[0]), int(before[0]), int(before[1])]
            branches.append(
                "SELECT * FROM (SELECT trade_id, state, updated_at, rowid AS seq FROM trades"
                " WHERE %s ORDER BY updated_at DESC, rowid DESC LIMIT ?)" % where)
            params += branch_params + [int(limit)]
    return db.execute(
        " UNION ALL ".join(branches) + " ORDER BY updated_at DESC, seq DESC LIMIT ?",
        tuple(params + [int(limit)]),
    ).fetchall()


def state_counts(db, user_id):
    """{state: trades}, an account's on both sides - off the two indexes."""
    summary = {state: 0 for state in services.STAFF_TRADE_STATES}
    for side in ("a", "b"):
        for r in db.execute(
                "SELECT state, COUNT(*) AS n FROM trades WHERE %s_user = ? GROUP BY state" % side,
                (user_id,)).fetchall():
            if r["state"] in summary:
                summary[r["state"]] += int(r["n"])
    return summary


def trade_row(db, trade_id):
    return db.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,)).fetchone()


def items_of(db, trade_id, side):
    rows = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND side = ? ORDER BY item_id",
        (trade_id, side),
    ).fetchall()
    return [{"item_id": r["item_id"], "quantity": int(r["quantity"])} for r in rows]


# -- what a trade looks like from one side --------------------------------------

def record(db, trade_id, user_id):
    """
    One finished trade as the person in it would describe it: who with, what
    you gave, what you got, and what the kingdom took from you.

    WORDED FROM THE CALLER'S SIDE, so a panel never has to work out whether it
    was "a" or "b". None for a trade the caller was not in - this is handed a
    trade_id by the server, never by a request, but it still only ever answers
    for the person asking.
    """
    trade = trade_row(db, trade_id)
    if trade is None:
        return None
    side = services.side_of(trade, user_id)
    if side is None:
        return None
    other = "b" if side == "a" else "a"
    who = db.execute(
        "SELECT u.username, s.name FROM users u"
        " LEFT JOIN saves s ON s.user_id = u.id AND s.slot = ?"
        " WHERE u.id = ?",
        (int(trade["%s_slot" % other]), int(trade["%s_user" % other])),
    ).fetchone()
    # WHICH OF THE CALLER'S OWN CHARACTERS it was. A player with four has four
    # bags, and "did that trade go through" is often "on which one".
    own = db.execute("SELECT name FROM saves WHERE user_id = ? AND slot = ?",
                     (int(user_id), int(trade["%s_slot" % side]))).fetchone()
    return {
        "trade_id": trade["trade_id"],
        "state": trade["state"],
        "at": int(trade["updated_at"]),
        "opened_at": int(trade["created_at"]),
        "character": (own["name"] or "") if own is not None else "",
        "with": who["username"] if who is not None else "?",
        "with_name": (who["name"] or "") if who is not None else "",
        "gave": items_of(db, trade["trade_id"], side),
        "got": items_of(db, trade["trade_id"], other),
        "gold_gave": int(trade["%s_gold" % side]),
        "gold_got": int(trade["%s_gold" % other]),
        "tax": int(trade["%s_tax" % side]),
    }


def summary(db, user_id):
    """
    The caller's open trade in one line, for the broadcast poll - or None.

    ENOUGH TO SAY "somebody wants to trade" AND NOTHING MORE. The items and the
    gold are the trade window's business and it reads them itself; this is
    what gets the window opened in the first place.
    """
    trade = find_open(db, user_id)
    if trade is None or services.expired(trade, time.time()):
        return None
    side = services.side_of(trade, user_id)
    other = "b" if side == "a" else "a"
    row = db.execute("SELECT username FROM users WHERE id = ?",
                     (int(trade["%s_user" % other]),)).fetchone()
    return {
        "trade_id": trade["trade_id"],
        "with": row["username"] if row is not None else "?",
        # SIDE A IS WHOEVER OPENED IT. So b is "they asked you".
        "from_them": side == "b",
        "they_accepted": bool(int(trade["%s_confirmed" % other])),
        "you_accepted": bool(int(trade["%s_confirmed" % side])),
    }


def person(db, user_id, slot):
    """The account and the character on one side of a trade."""
    return db.execute(
        "SELECT u.username, u.role, u.name_hue, s.name, s.level FROM users u"
        " LEFT JOIN saves s ON s.user_id = u.id AND s.slot = ?"
        " WHERE u.id = ?", (slot, user_id)).fetchone()


# -- opening, changing, finishing ------------------------------------------------

def account_by_name(db, username):
    """(id, username) by the name in any case (users.username is NOCASE)."""
    return db.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()


def lapse(db, trade_id, now):
    """A stale open trade, judged when it is in the way rather than by a
    sweeper."""
    db.execute("UPDATE trades SET state = 'cancelled', updated_at = ?"
               " WHERE trade_id = ?", (now, trade_id))


def open_trade(db, trade_id, a_user, a_slot, b_user, b_slot, now):
    """A new open trade - and the cancelled ones past their week dropped.

    THE PRUNE, on the request that makes rows. Cancelled trades only - a
    finished one is the record of who gave whom what. trade_items goes with
    them by ON DELETE CASCADE; idx_trades_cancelled keeps this off a table
    scan."""
    db.execute(
        "INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trade_id, a_user, a_slot, b_user, b_slot, now, now),
    )
    db.execute("DELETE FROM trades WHERE state = 'cancelled' AND updated_at < ?",
               (now - services.TRADE_CANCELLED_KEPT_SECONDS,))


def purse(db, user_id, slot):
    """The character's carried gold, or None for no such character."""
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return None if row is None else int(row["gold"])


def carried(db, user_id, slot):
    """{item_id: how many} in this character's carry."""
    held = {}
    for row in db.execute(
        "SELECT item_id, SUM(quantity) AS total FROM carry_items"
        " WHERE user_id = ? AND slot = ? GROUP BY item_id", (user_id, slot)
    ).fetchall():
        held[row["item_id"]] = int(row["total"])
    return held


def set_offer(db, trade_id, side, items, gold):
    """One side's whole offer, replaced."""
    db.execute("DELETE FROM trade_items WHERE trade_id = ? AND side = ?",
               (trade_id, side))
    for item_id, quantity in sorted(items.items()):
        db.execute(
            "INSERT INTO trade_items (trade_id, side, item_id, quantity) VALUES (?, ?, ?, ?)",
            (trade_id, side, item_id, quantity),
        )
    db.execute("UPDATE trades SET %s_gold = ? WHERE trade_id = ?" % side,
               (gold, trade_id))


def touch(db, trade_id, now):
    """Any change to an offer un-confirms BOTH sides.

    You confirm what you were shown. If one side could add an item after the
    other had agreed, 'confirmed' would mean nothing - and the version of this
    bug that ships is the one where the change is a REMOVAL.

    AND THE REVISION MOVES, which is the half that closes the race. Clearing
    the confirms only protects an accept that already happened. An accept that
    is in flight WHILE the other side swaps their sword for a stick lands after
    the swap, on the new offer, and nothing about it says the player never saw
    the stick - their window polls every second and a half. So every change
    bumps the revision, and trade_confirm() refuses an accept for any revision
    but the current one.
    """
    db.execute(
        "UPDATE trades SET a_confirmed = 0, b_confirmed = 0,"
        " revision = revision + 1, updated_at = ?"
        " WHERE trade_id = ?",
        (now, trade_id),
    )


def agree(db, trade_id, side, revision, now):
    """COMPARE-AND-SET, in the UPDATE itself: this side's accept, of THIS
    revision, of a trade still open. True if it landed."""
    return db.execute(
        "UPDATE trades SET %s_confirmed = 1, updated_at = ?"
        " WHERE trade_id = ? AND revision = ? AND state = 'open'" % side,
        (now, trade_id, revision)).rowcount == 1


def call_off(db, trade_id, now):
    db.execute("UPDATE trades SET state = 'cancelled', updated_at = ? WHERE trade_id = ?",
               (now, trade_id))


def claim(db, trade_id, now):
    """CLAIM IT. Conditional on still being open, and checked that a row
    actually changed - the compare-and-set that makes a double confirm safe."""
    return db.execute(
        "UPDATE trades SET state = 'done', updated_at = ? WHERE trade_id = ? AND state = 'open'",
        (now, trade_id),
    ).rowcount == 1


def lapse_agreement(db, trade_id, now):
    """Nobody has agreed any more - after a refused execution."""
    db.execute(
        "UPDATE trades SET a_confirmed = 0, b_confirmed = 0, updated_at = ?"
        " WHERE trade_id = ? AND state = 'open'",
        (now, trade_id),
    )


def move_gold(db, user_id, slot, gives, gets, now):
    """THE TRANSFER: a bare UPDATE on purpose, writing no ledger row - see
    routes.execute()."""
    db.execute(
        "UPDATE saves SET gold = gold - ? + ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (gives, gets, now, user_id, slot),
    )


def note_taxes(db, trade_id, a_tax, b_tax):
    db.execute("UPDATE trades SET a_tax = ?, b_tax = ? WHERE trade_id = ?",
               (a_tax, b_tax, trade_id))
