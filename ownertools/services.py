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
