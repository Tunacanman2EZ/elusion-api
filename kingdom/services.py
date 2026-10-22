"""kingdom/services.py - the rules: how long the board is, how long its shared
half may be stale, how a tie ranks, and what a guild's line adds up to. Pure:
no database, no request."""

# How many players the board returns. Two hundred, not ten.
#
# A TOP TEN IS NOT A SCOREBOARD FOR A GAME WITH TWO HUNDRED PLAYERS - it is a
# wall with ten names on it that nobody else ever appears on, which makes the
# whole "your contribution counts" idea into something only the richest ten
# players ever see evidence of. At 200 the board IS the population, so paying
# 80% of everything you own buys a line somebody can actually find.
#
# THE CALLER'S OWN LINE IS SEPARATE AND ALWAYS PRESENT, so this number is about
# who else you can see, never about whether you can see yourself.
#
# PAST A FEW THOUSAND PLAYERS THIS NEEDS PAGING, not a bigger constant: the
# response is one row per player and the client builds a node per row. The seam
# is here - an offset parameter and a total count - and it is worth doing the
# day the number of accounts starts with a comma.
KINGDOM_BOARD_SIZE = 200

# HOW LONG THE BOARD'S SHARED HALF IS ALLOWED TO BE STALE.
#
# THE PROBLEM THIS SOLVES IS THE MULTIPLIER, NOT THE QUERY. Even after the
# partial indexes, one board read is a pass over every destroyed-gold row in
# the ledger. The panel refreshes itself every thirty seconds, so the cost is
# paid once per open panel per thirty seconds - ten players with it open is ten
# full passes, fifty is fifty, and the work is identical every time because the
# figures are the same for everybody.
#
# So it is computed at most once per window and handed to everyone who asks
# inside it. Ten panels and one panel now cost the same.
#
# FIFTEEN, NOT THIRTY, and the reason is that the two clocks are independent: a
# panel that refreshes just after a window opens would otherwise show figures
# nearly thirty seconds older than the ones the next panel sees. Half the
# refresh interval bounds the staleness at something smaller than the refresh
# itself, which is the point where nobody can notice.
#
# WHAT IS NOT CACHED IS YOUR OWN LINE. `you` is different for every caller and
# is built per request from the cached list, so the number a player checks for
# themselves is never served from somebody else's snapshot.
KINGDOM_BOARD_CACHE_SECONDS = 15


def cached(cache, now):
    """The last board if it is still inside the window, otherwise None."""
    payload = cache.get("payload")
    if payload is None:
        return None
    if now - int(cache.get("at", 0)) >= KINGDOM_BOARD_CACHE_SECONDS:
        return None
    return payload


def standing(entry):
    """What a line is ranked by: gold, with lusions breaking the tie. Gold
    leads because it is what the coffers are measured in; lusions decide who
    sits higher among players who gave the same amount of it."""
    return (entry["contributed"], entry["lusions"])


def rank(entries):
    """Stamp each entry, already sorted by standing(), with its rank.

    COMPETITION RANKING - 1, 1, 3 - because a tie means neither player beat
    the other and printing 1 and 2 says one of them did. The gap after a tie
    is the honest consequence: two people finished ahead of whoever is third.

    The key is the same one the sort used, so the ordering and the ranking
    can never disagree about what counts as equal."""
    place = 0
    previous_key = None
    for index, entry in enumerate(entries):
        key = standing(entry)
        if key != previous_key:
            place = index + 1
            previous_key = key
        entry["rank"] = place


def guild_board(board):
    """The same board, by guild, ranked - FROM THE ROWS ALREADY IN MEMORY.

    MEMBERS HERE MEANS "MEMBERS WHO HAVE GIVEN SOMETHING", not the roster
    size, and the field is named honestly rather than being a number that
    disagrees with the guild panel. A guild of fifty where two people have
    ever spent a coin contributes what those two gave."""
    by_guild = {}
    for entry in board:
        name = entry["guild"]
        if not name:
            continue
        line = by_guild.setdefault(name, {
            "name": name, "tag": entry["guild_tag"],
            "contributed": 0, "lusions": 0, "deaths": 0, "members": 0,
        })
        line["contributed"] += entry["contributed"]
        line["lusions"] += entry["lusions"]
        line["deaths"] += entry["deaths"]
        line["members"] += 1
    guilds = sorted(by_guild.values(), key=standing, reverse=True)
    # Competition ranking, the same rule and the same reason as the player
    # board: a tie means neither beat the other.
    rank(guilds)
    return guilds


def my_line(board, username, deaths):
    """The caller's own line: their board entry, or a zero line with their
    deaths when they have never given anything.

    entry["rank"], not the position in the list: those differ the moment two
    players are tied above you, and the number a player checks for themselves
    is the one that most needs to be right."""
    for entry in board:
        if entry["username"] == username:
            return dict(entry)
    return {"username": username, "contributed": 0, "lusions": 0,
            "deaths": deaths, "lost": 0, "rank": None, "title": ""}


# =============================================================================
# PATRONAGE (0.74.0)
# =============================================================================
# The owner's third economy idea: "things players choose to spend gold on:
# cosmetics on the kingdom board". A gift to the crown is burned - reason
# donation - and buys nothing but a TITLE beside the giver's name, by what the
# ACCOUNT has given in all: Patron, Benefactor, Noble Patron, Royal Patron
# (GameConstants, through gamedata.json's patron_titles). Nothing a title does
# can be traded back into gold, which is what makes it a sink and not a shop.
#
# A GIFT IS ALSO GOLD GIVEN. The board already sums every coin destroyed, so a
# donor climbs the ranking as well as earning the title - one figure, not a
# second scoreboard to keep in step with the first.
#
# THE ACCOUNT, NOT THE CHARACTER: the board is a board of players, and a title
# earned on the warrior is worn by the mage. The gold leaves one character's
# purse; the ledger row names the account.

def patron_titles(constants):
    """[(at, title)] from the catalogue's patron_titles, lowest first. A
    catalogue from before patronage has none, and nobody has a title."""
    out = []
    for entry in (constants.get("patron_titles") or []) if isinstance(constants, dict) else []:
        try:
            at, title = int(entry["at"]), str(entry["title"])
        except (KeyError, TypeError, ValueError):
            continue
        if at > 0 and title:
            out.append((at, title))
    return sorted(out)


def donation_minimum(constants):
    """The smallest gift the crown takes, in gold - never below 1, so a gift of
    nothing is never a ledger row."""
    try:
        return max(1, int((constants or {}).get("donation_minimum", 1) or 1))
    except (TypeError, ValueError):
        return 1


def title_for(donated, titles):
    """(title, next_title, next_at) for an account that has given `donated` in
    all: the highest title reached ("" below the first), the next one and the
    lifetime total it is reached at ("" and 0 past the last)."""
    title, next_title, next_at = "", "", 0
    for at, name in titles:
        if donated >= at:
            title = name
        else:
            next_title, next_at = name, at
            break
    return title, next_title, next_at
