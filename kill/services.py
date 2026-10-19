"""kill/services.py - the rules: how fast kills may be claimed, how many of one
monster could have died lately, how long a kill's row is kept, and what the
world is told when a mythic drops. Pure: no database, no request. (What a
monster is worth and what it drops are the catalogue's - gamedata.)"""

# A TOKEN BUCKET, NOT A MINIMUM GAP.
#
# This was "one kill per 0.35 seconds", and it refused legitimate kills within
# an hour of shipping:
#
#     [KILL] bushmage refused — Kills are limited to one per 0.35s.
#     [KILL] poisonslimesmall refused — Kills are limited to one per 0.35s.
#
# A large poison slime splits into four smalls. An area attack kills several
# enemies in the same frame. Real combat is BURSTY, and no minimum gap can ever
# allow a burst - that is what a minimum gap is for. The player lost those
# rewards, which is the worst possible failure for a limit that cannot prove
# anything in the first place.
#
# A bucket separates the two shapes: a burst spends what is saved up, and
# sustained farming is held to the refill rate. (It shipped at twenty tokens
# and one a second - sixty kills a minute. SIZED FOR A GAME THAT THROWS CROWDS
# AT YOU, below, is why it is fifty and five now.)
#
# WHAT THIS IS NOT: proof. The server cannot tell a real kill from a claimed
# one; it has no idea where anything is. This caps the rate and nothing more.
# The real answer is the server owning which enemies exist and having handed
# this client that one.
#
# SIZED FOR A GAME THAT THROWS CROWDS AT YOU. Darza's Dominion - one of this
# project's stated inspirations - puts dozens of enemies on screen and expects
# you to clear them without pausing, and Elusion is aiming at the same feel. A
# bucket that a good fight can empty is a bucket that punishes playing well.
#
# Fifty covers any burst a screen can hold. Five a second sustained is three
# hundred kills a minute, comfortably above the fastest honest rate and still
# bounded: a script wanting ten thousand kills needs half an hour rather than a
# few seconds, which is the difference between an exploit and a chore.
KILL_BUCKET_CAPACITY = 50.0
KILL_TOKENS_PER_SECOND = 5.0

# THE SPAWN CEILING: how many of one enemy could physically have died by now.
#
# The token bucket above caps the RATE of claims at a number somebody chose.
# This caps the COUNT at what the world contains, which is a number nobody
# chose - it falls out of how many of that enemy are placed and how fast a
# respawner returns them.
#
# THE ARITHMETIC, because the window size is not arbitrary. One spawn point
# yields at most (W / respawn) + 1 kills in a window of W seconds: one per
# respawn, plus the one that happened at the very instant the window opened.
# That +1 is pure slack handed to a cheater, and it dilutes as W grows - so a
# LONGER window bounds tighter while still refusing nobody honest:
#
#     W = 30s    ->  8,400 kills/hour   28.0x honest play
#     W = 300s   ->  4,620 kills/hour   15.4x
#     W = 1800s  ->  4,270 kills/hour   14.2x   (the asymptote)
#
# 300 is where the curve flattens; past it the table scan grows for nothing.
# The token bucket alone allows 18,000/hour, or 60x honest play, so this is
# about four times tighter and the two compose - a claim must satisfy both.
#
# RESPAWN_FLOOR IS THE MINIMUM, NOT THE AVERAGE. enemyrespawner.gd waits
# respawn_seconds and then adds up to respawn_jitter MORE, never less. Using
# the floor means the ceiling is always at least as generous as reality, so
# jitter can only ever make an honest player use less of their allowance.
# If respawn_seconds is ever lowered in a scene, lower this with it.
KILL_WINDOW_SECONDS = 300
KILL_RESPAWN_FLOOR_SECONDS = 30.0

# How long a kill report is kept. Long enough to see a slow, patient cheat in
# the log; short enough that the table does not grow forever unattended. The
# same reasoning and the same self-pruning shape as LOGIN_LOG_RETENTION_SECONDS.
KILL_LOG_RETENTION_SECONDS = 60 * 60 * 24 * 14

# THE SERVER'S RECEIPT FOR A KILL (0.57.0; E3_SCOPE.md option C, step 2).
#
# Since 0.10.0 presence.py has kept its own count of every monster's health
# (combatbook.py) and judged every death into combat_kills - AGREED, SHORT or
# NOT DUE, one row per player who hit it - and nothing read it to decide
# anything. With the owner's switch on (RECEIPTS_KEY in app.py), a kill is paid
# only against an AGREED row for this account, this character (or a row that
# could not say which) and this monster, no older than RECEIPT_SECONDS, that
# has not paid a kill already (kill_receipts).
#
# NOT YET IS NOT NO. The books hold a death HOLD_SECONDS for its late hits and
# then write it within a quarter of a second, so the game's claim usually
# arrives first: the route answers 202 with RECEIPT_RETRY_MS, and the game asks
# again (combat.gd, up to eight seconds). Nothing is spent on a 202 - not a
# token, not a receipt - so asking again costs nothing but the request.
#
# A GAME TOO OLD TO ASK AGAIN (below RECEIPT_BUILD) would read the 202 as a
# kill paid nothing and say nothing. It is told to update instead (409).
#
# SIXTY SECONDS, not the thirty killwatch.py matches by: the game asks again
# for at most eight, and a slow link or a long death animation must not turn a
# fight the server saw into a refusal. A receipt older than that belongs to
# some other kill.
RECEIPT_SECONDS = 60
RECEIPT_RETRY_MS = 400
RECEIPT_BUILD = 7


def tokens_now(stored, last_kill_ms, now_ms):
    """The bucket as it stands at `now_ms`: what it held at the last paid kill,
    refilled for the time since, never past its capacity.

    Refill first, then spend. last_kill_at of 0 means this character has never
    reported a kill, and the elapsed time since 1970 would overfill the bucket
    - min() against the capacity handles that without a special case."""
    elapsed_seconds = max(now_ms - int(last_kill_ms), 0) / 1000.0
    return min(
        KILL_BUCKET_CAPACITY,
        float(stored) + elapsed_seconds * KILL_TOKENS_PER_SECOND,
    )


def spawn_ceiling(placed):
    """The most kills of one monster `placed` spawn points could have produced
    in the last KILL_WINDOW_SECONDS - see the arithmetic above."""
    return int(placed * (KILL_WINDOW_SECONDS / KILL_RESPAWN_FLOOR_SECONDS + 1))


# THE KIND A MYTHIC'S NOTICE IS POSTED UNDER. The game draws it as the red
# banner. The owner's broadcast route (switches/) accepts only "system" and
# "shout", so nobody can type one.
MYTHIC_BROADCAST_KIND = "mythic"


def mythic_find_text(catalogue, username, item_id, enemy_id):
    """
    "Tunacan found the Meteorite on a Dark Sprite!" The enemy is named because
    a mythic off a light slime is a story worth telling. A boss is a name
    ("The Crowned Beholder", "Fire The Crowned Beholder") and takes no article.
    The names are the catalogue's (gamedata).
    """
    item = catalogue.item_row(item_id) or {}
    enemy = catalogue.ENEMIES.get(enemy_id, {})
    what = item.get("display_name") or item_id
    who = enemy.get("display_name") or enemy_id
    if not enemy.get("slots_are_gear"):
        who = ("an " if who[:1].lower() in ("a", "e", "i", "o", "u") else "a ") + who
    return "%s found the %s on %s!" % (username, what, who)
