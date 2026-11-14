"""ownertools/services.py - the rules: how far one gold grant reaches, the
highest level and the skills the owner's tools set, the reason a rollback's
gold is ledgered under, a snapshot as the save history lists it, and what a
snapshot puts back into a character's cells and skills. Pure: no database, no
request."""

# How much gold one debug grant may mint, and how much it may take away. A
# ceiling rather than no limit: a typo in a box should cost a re-press, not
# make the Kingdom Tax board meaningless forever.
DEBUG_GOLD_MAX = 1000000

# The highest level the owner's level tool sets - the client's sanity clamp on a
# loaded save (CharacterData.MAX_LEVEL). There is no level cap in the game; this
# only stops a typo making a character the server's curve cannot describe.
STAFF_LEVEL_MAX = 99

# The skills the owner's skill tool sets, in the order the game lists them.
# test_ownership.py holds it to VALID_SKILLS, so a seventh skill cannot be
# added to the game and quietly left out of "all".
STAFF_SKILL_ORDER = ("attack", "defense", "agility", "magic", "fishing", "cooking")

# The reason a rollback's gold goes through the ledger under, up or down - so
# the supply invariant holds and the Kingdom board counts what it took.
ROLLBACK_REASON = "staff_rollback"


def snapshot_row(row):
    """One snapshot as the save history lists it - never the JSON itself."""
    return {
        "id": int(row["id"]),
        "taken_at": int(row["taken_at"]),
        "reason": row["reason"],
        "level": int(row["level"]),
        "gold": int(row["gold"]),
        "items": int(row["items"]),
    }


def carried_rows(state, carry_cells):
    """The carried cells a snapshot puts back, as rows: (position, item_id,
    quantity), each well formed and inside the carry - a cell past it, or an
    entry that is not three things, is left out."""
    return [(int(c[0]), str(c[1]), int(c[2]))
            for c in state.get("carry", [])
            if isinstance(c, list) and len(c) == 3 and 0 <= int(c[0]) < carry_cells]


def skill_rows(state):
    """The skills a snapshot puts back, as rows: (skill_id, level, xp), a level
    of at least 1 and XP of at least 0."""
    return [(str(s[0]), max(1, int(s[1])), max(0, int(s[2])))
            for s in state.get("skills", []) if isinstance(s, list) and len(s) == 3]


# =============================================================================
# A ROLLBACK HOLDS BACK WHAT WENT ELSEWHERE (game 0.157.0)
# =============================================================================
# The owner, 14 Nov: "lets fix issues in security". A rollback put back what
# the character held at the snapshot, so a sword traded away or banked since
# came back AND stayed where it went - a second copy. Now each item the
# character has lost since is put back only as far as it did not go somewhere
# that still has it: the account's other characters or bank (the snapshot
# keeps the account's count then, `elsewhere`; the rollback counts again) or
# another account, through a finished trade since the snapshot. What it used
# up, sold or threw away comes back, as it always did - that is what a
# rollback is for; the purse goes back with a sale.
#
# Counted by item id, as the trade holds are. A rolled piece's id is its own
# roll, so for gear this is nearly one piece; for a stack of potions, another
# character's new potions can look like this one's - and then fewer come back,
# never more. In doubt, held back, and the answer says what.


def held_back(snap_held, now_held, elsewhere_then, elsewhere_now, traded_out):
    """{item_id: how many the rollback must not put back}. Each argument is an
    {item_id: count}; an id missing is 0. For each id the snapshot held: lost
    is what the character no longer has; went is how many more the rest of
    the account holds now than then, plus what the account gave away in
    trades since; held back is the smaller."""
    holds = {}
    for item_id, then in snap_held.items():
        lost = max(0, int(then) - int(now_held.get(item_id, 0)))
        went = (int(elsewhere_now.get(item_id, 0)) - int(elsewhere_then.get(item_id, 0))
                + int(traded_out.get(item_id, 0)))
        hold = min(lost, max(0, went))
        if hold > 0:
            holds[item_id] = hold
    return holds


def without_held(rows, equipment, holds):
    """(rows, equipment) with `holds` taken out: from the carried cells first,
    the last cell first, then from what is worn. Rows are (position, item_id,
    quantity)."""
    left = dict(holds)
    kept = []
    for position, item_id, quantity in reversed(list(rows)):
        take = min(quantity, left.get(item_id, 0))
        if take:
            left[item_id] -= take
        if quantity - take > 0:
            kept.append((position, item_id, quantity - take))
    worn = dict(equipment) if isinstance(equipment, dict) else {}
    for slot_name in sorted(worn):
        item_id = worn[slot_name]
        if isinstance(item_id, str) and left.get(item_id, 0) > 0:
            left[item_id] -= 1
            del worn[slot_name]
    return list(reversed(kept)), worn


def held_back_words(holds, name_of):
    """"2 x Iron Sword, Jade Helm" - what a rollback held back, for the log
    and the answer. name_of(item_id) -> its name."""
    return ", ".join(("%d x %s" % (n, name_of(i)) if n > 1 else name_of(i))
                     for i, n in sorted(holds.items()))

