"""trade/services.py - the rules: how big an offer may be, how long one
lives, how long a fresh find waits, what one side is worth, and whose side is
whose. Pure: no database, no request. `catalogue` is gamedata, handed in by
app.py and asked on every call (item_row, mythic_tier, stack_value,
trade_tax, has_item)."""

# How many distinct item types one side may put up. The backpack is
# INVENTORY_CAPACITY cells and both sides need room to RECEIVE, so this is
# deliberately well under it rather than equal to it.
TRADE_MAX_ITEM_TYPES = 8

# An offer nobody has touched for this long is dead. Trades are not swept by a
# background job - there isn't one - so staleness is judged when somebody asks,
# which means an abandoned trade costs nothing until it is in the way.
TRADE_EXPIRY_SECONDS = 600

# How long a CANCELLED trade is kept. A finished trade is kept for good - it is
# the record a moderator needs when somebody says they were scammed - but a
# cancelled one is two people who talked and walked away, and nothing reads it
# after a week. Pruned when a new trade is opened, like loot bags on a kill:
# the thing making rows is the thing that clears them, and there is no sweeper.
TRADE_CANCELLED_KEPT_SECONDS = 7 * 24 * 60 * 60

# HOW LONG A FRESH MYTHIC OR PERFECT WAITS BEFORE IT CAN BE TRADED. See
# trade_holds in schema.py. Long enough for killwatch.py's daily look and a
# person to read it; short enough that an honest find is tradeable the day
# after tomorrow. The owner chose the two days, 6 Oct.
TRADE_HOLD_SECONDS = 48 * 60 * 60

# How many finished trades GET /api/trade/history returns. Enough to answer
# "what did I just do" and "did that trade yesterday go through"; a full
# ledger is a moderation question, not a panel.
TRADE_HISTORY_SHOWN = 10

# The states a staff member may page through. Open ones too: a scam is often
# reported while it is still being attempted.
STAFF_TRADE_STATES = ("open", "done", "cancelled")


def held_kind(catalogue, item_id):
    """True for the pieces a fresh find holds: mythic tier, or a Perfect roll.
    Those are where a cheated kill's value concentrates."""
    row = catalogue.item_row(item_id)
    if not row:
        return False
    return int(row.get("tier", 0)) >= catalogue.mythic_tier() or bool(row.get("perfect"))


def hold_words(display_name, first_free_at, now):
    """The sentence for a held piece: what, and when it may change hands."""
    wait = max(0, int(first_free_at) - now)
    hours = (wait + 3599) // 3600
    return ("%s was found less than %d hours ago and can be traded in %s. "
            "Mythic and Perfect finds wait %d hours before they can change hands."
            % (display_name, TRADE_HOLD_SECONDS // 3600,
               ("%d hour%s" % (hours, "" if hours == 1 else "s")) if hours > 0 else "a moment",
               TRADE_HOLD_SECONDS // 3600))


def expired(trade, now):
    return (int(now) - int(trade["updated_at"])) > TRADE_EXPIRY_SECONDS


def side_of(trade, user_id):
    """'a', 'b', or None for somebody else's trade."""
    if int(trade["a_user"]) == int(user_id):
        return "a"
    if int(trade["b_user"]) == int(user_id):
        return "b"
    return None


def value(catalogue, items, gold):
    """
    What one side is putting up, in gold. None when an item is not in the
    catalogue - which is a refusal, not a zero: valuing an unknown item at
    nothing would make it the tax-free way to move anything.
    """
    total = int(gold)
    for entry in items:
        worth = catalogue.stack_value(entry["item_id"], entry["quantity"])
        if worth is None:
            return None
        total += worth
    return total
