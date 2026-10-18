"""death/services.py - the rules: how a revive may be paid for, where the gold
comes from, what the death score counts, and what a full death takes. Pure: no
database, no request."""
import json

# TWO WAYS TO PAY, and gold is the one everybody always has. Lusions come from
# duplicate pets, so a player who has never rolled one had no way back at all -
# which made the death screen a dead end rather than a decision. Banked gold is
# the pile death cannot touch, and spending it here is the only thing that
# makes "bound" mean anything.
PAY_WITH = ("lusions", "gold")


def gold_split(carried, cost):
    """(from the carry, from the bank). CARRY FIRST, THEN BANK. The carry pile
    is the one true death would have taken anyway, so it pays before the pile
    that survives - what is left over ends up where it is safe, which is the
    point of banking it."""
    from_carry = min(carried, cost)
    return from_carry, cost - from_carry


def gold_shortfall(cost, held):
    """The 402 for a gold revive that cannot be paid, or None. BOTH NUMBERS GO
    BACK, because the death screen draws the shortfall rather than
    recomputing it."""
    if cost <= 0:
        return {
            "error": "Payment Required",
            "message": "You have no gold to pay with.",
            "cost": 0,
            "gold": 0,
        }, 402
    if cost > held:
        # THE FLOOR BIT, and the message says so rather than reporting a
        # generic shortfall. A player who is told "reviving costs 100 gold; you
        # have 40" can work out what to do; one told "not enough gold" against
        # a percentage they cannot see is being asked to guess.
        return {
            "error": "Payment Required",
            "message": "Reviving costs at least %d gold; you have %d." % (cost, held),
            "cost": cost,
            "gold": held,
        }, 402
    return None


def lusion_shortfall(cost, have):
    """The 402 for a lusion revive that cannot be paid, or None - 402 rather
    than 403: this is not a permission, it is a price."""
    if have < cost:
        return {
            "error": "Payment Required",
            "message": "Reviving costs %d lusions; you have %d." % (cost, have),
            "cost": cost,
            "lusions": have,
        }, 402
    return None


def score_for(method, cost, lusion_gold_value):
    """What a revive adds to the death score, IN GOLD: a gold revive what it
    burned, a lusion revive its lusions at the published rate (app.py's
    LUSION_GOLD_VALUE) - never the two added 1:1, which said that dying with 20
    lusions and dying with 20 gold cost the same."""
    return int(cost) if method == "gold" else int(cost) * int(lusion_gold_value)


def gear_lost(raw_equipment):
    """Every piece worn, by id, sorted - what a full death takes off the
    character. An unreadable column is nothing worn."""
    try:
        worn = json.loads(raw_equipment) if raw_equipment else {}
    except (TypeError, ValueError):
        worn = {}
    if not isinstance(worn, dict):
        worn = {}
    return sorted(str(item_id) for item_id in worn.values() if item_id)
