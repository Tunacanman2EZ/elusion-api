"""status/services.py - the rules: which stats a status write may carry, which
the server owns or derives, and what a status read says. Pure: no database,
no request. (How much healing is explained is reconcile_heals() in routes.py:
it reads the grants, and its tunables are app.py's.)"""

# Every stat the client is allowed to push, and the ceiling each one is
# checked against. The client proposes; the server decides. A value that
# cannot legally exist is refused rather than clamped, because clamping
# hides the bug that produced it.
STATUS_FIELDS = {
    "level":       None,        # no paired maximum
    "hp":          "max_hp",
    "mana":        "max_mana",
    "stamina":     "max_stamina",
    "gold":        None,
    "xp":          None,
    "xp_to_next":  None,
    "max_hp":      None,
    "max_mana":    None,
    "max_stamina": None,
}

# FIELDS THE SERVER OWNS. A client may send them; the server ignores the values
# and keeps its own.
#
# level, xp and xp_to_next are granted by /api/combat/kill, which applies the
# level-up loop itself and commits the result. The server therefore already
# knows what they should be, and an assertion from the client is not evidence
# of anything.
#
# THIS IS THE HALF THAT MAKES THE OTHER HALF MEAN SOMETHING. Moving the loot
# roll to the server closed the "give myself a pet" hole, but while
# /api/player/status accepted a level, a modified client could simply declare
# itself level 60 and skip the game entirely. It could not do that until there
# was a server-side record to contradict it - which is why this change had to
# come second, not first.
#
# GOLD IS IN THIS LIST AND ITS ABSENCE WAS A HOLE STRAIGHT THROUGH THE ECONOMY.
#
# PUT /api/save has always refused a client's gold. PUT /api/player/status never
# did - `gold` is one of STATUS_FIELDS and was not one of these, so a single
# request set any balance an authenticated player liked:
#
#     PUT /api/player/status  {"slot": 0, "gold": 1000000}   -> 200
#
# and the invariant the whole gold_ledger exists to hold up
#
#     SUM(gold_ledger.delta) == SUM(saves.gold) + SUM(accounts.bank_gold)
#
# broke on that one call: nothing was minted through gold_delta(), so the left
# side stayed at 0 while the right side went to a million. The vendor sink, the
# kingdom tax and every trade valuation sit on top of that equation.
#
# NOTHING HONEST NEEDS TO PUSH A GOLD FIGURE, which is why this was safe to
# close rather than shadow-log first. /api/loot/take resolves a CURRENCY drop
# straight into a balance through gold_delta(); shop purchases, bank moves,
# trades and cashing a pile all write their own rows.
#
# Ignored rather than refused, like every other owned field: an honest client
# sends its whole status block and has no way to know which fields the server
# has taken over. It is named in `ignored` so the client can see it is being
# corrected rather than wonder why its gold did not stick.
#
# NOT IN THIS LIST: hp, mana and stamina - the result of combat and regen the
# server does not simulate. They are held to their derived maxima and to what
# regeneration and the server's grants explain (reconcile_heals()), which is
# all the server can honestly say about them.
SERVER_OWNED_STATS = ("level", "xp", "xp_to_next", "gold")

# DERIVED, not merely owned. These are not stored from a request at all - they
# are recomputed from the character's class, level and what it is wearing every
# time the status is written, using the same curve the client runs:
#
#     max_hp = hp_base + (level - 1) * hp_per_lvl
#
# That curve used to be four literals inside warrior.gd's _set_stat_curve(), so
# the server knew your level AND your class and still could not work out your
# maximum health. ClassData moved it into data/classes/*.tres and the exporter
# carries it here - the same route EnemyData took.
DERIVED_STATS = ("max_hp", "max_mana", "max_stamina")


def view(row):
    """A character's live stats, as the HUD reads them and every route that
    changes them answers with."""
    return {
        "slot": row["slot"],
        "level": row["level"],
        "hp": row["hp"], "max_hp": row["max_hp"],
        "mana": row["mana"], "max_mana": row["max_mana"],
        "stamina": row["stamina"], "max_stamina": row["max_stamina"],
        "gold": row["gold"],
        "xp": row["xp"], "xp_to_next": row["xp_to_next"],
    }
