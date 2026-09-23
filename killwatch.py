#!/usr/bin/env python3
"""
killwatch.py - a read-only watch over what players claim to have killed.

    python3 killwatch.py                      # review $ELUSION_DB (or elusion.db)
    python3 killwatch.py --db /path/elusion.db --quiet   # cron: speak only on an ALARM

WHERE THIS SITS, AND WHY IT IS THE THIRD TOOL AND NOT THE FIRST TWO
------------------------------------------------------------------
There are now three watchers, and each answers a different question:

  * security_bot.py  - attacks the server BEFORE players are let on. A gate you
                       run by hand: "can the wall be climbed at all."
  * canary.py        - reads the LIVE database on a schedule and checks the
                       economy's INVARIANTS: "do the numbers still add up."
  * killwatch.py     - reads the LIVE database on a schedule and checks player
                       BEHAVIOUR against what the world can physically produce:
                       "is anyone claiming kills that could not have happened."

E-3 is the reason this one exists. `/api/combat/kill` never sees the fight - it
rolls the rewards, rate-limits with a token bucket, and refuses more kills of an
enemy than the world contains (the spawn ceiling), but it cannot tell a real
kill from a claimed one. SECURITY_NOTES.md is blunt about this: the next real
move on E-3 is the server OBSERVING combat, which is an architecture change, not
a refinement. Until that day, the honest posture is not to pretend the fraud is
prevented but to make it VISIBLE - to turn a claim nobody could see into an
account somebody can review and, if it is what it looks like, ban.

`kill_reports` is the groundwork that made this possible: every paid kill leaves
one row (enemy, rewards as paid, the character level it was claimed at), and a
refused kill leaves none, so the log and the payouts cannot drift apart. This
tool reads that log and nothing else about combat.

TWO KINDS OF FINDING, AND THE DIFFERENCE MATTERS
------------------------------------------------
IMPOSSIBLE (an ALARM). A claim the SERVER'S OWN RULES should have refused: more
kills in a window than the spawn ceiling allows, or a kill of an enemy the
handler marks reward-less and 400s before it ever writes a row. A row like this
does not mean "a player is fast". It means a defence that is supposed to be
running was NOT - the ceiling was disarmed by a stale gamedata.json (this has
happened, and went unnoticed for 46 hours), or a validation path was bypassed.
This is the thing to be paged about, so it sets a non-zero exit code and speaks
even under --quiet. It is also FALSE-POSITIVE FREE BY CONSTRUCTION: it flags
only what the live gate itself would have refused, so it can never fire on an
honest player - only on a broken wall.

SUSPICIOUS (a REVIEW, not an alarm). A pattern that is legal under today's
bounds but far outside honest play: a sustained rate many times the ~300
kills/hour honest baseline, a boss farmed faster than anyone should, a character
farming only the one enemy the ceiling cannot see, a boss killed at a level
nobody reaches it at. None of these is proof - the spawn ceiling permits up to
~5,200 kills/hour, which is 17x honest and still legal - so this tool does NOT
ban and does NOT pick a threshold and enforce it. It RANKS accounts and SHOWS
THE NUMBERS, because a threshold picked without data is exactly how honest
players get clamped (the interim skill bound under E-2 was that mistake). You
read the dossier and decide. Under --quiet these stay silent; run it without
--quiet - interactively, or as a weekly digest - to see the review list.

READ-ONLY BY CONSTRUCTION. Opens the database mode=ro, the same discipline as
canary.py: a watcher must never be able to become the thing it is watching.

EXIT CODE is 0 unless an IMPOSSIBLE finding exists, in which case it is 1 - so a
scheduler turns "a wall came down" into a page. A server thick with SUSPICIOUS
accounts and no IMPOSSIBLE ones still exits 0: those are for you to read, not for
a pager to scream about at 3am.
"""

import argparse
import os
import sqlite3
import sys

# gamedata carries placed_count (how many of each enemy exist), max_hp (which
# enemies are bosses) and grants_rewards (which cannot be killed for profit). It
# reads gamedata.json at import and touches no database, so importing it here is
# safe and side-effect-free. If it cannot load, the IMPOSSIBLE checks that need
# the world go dark and we say so loudly - a fraud watch that silently stopped
# checking would be worse than none.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import gamedata
    _GAMEDATA_OK = True
    _GAMEDATA_ERR = None
except Exception as exc:                      # GameDataError, ImportError, ...
    gamedata = None
    _GAMEDATA_OK = False
    _GAMEDATA_ERR = str(exc)


# THE LIVE GATE'S OWN NUMBERS, so the retrospective check flags exactly what the
# live check would have refused and never anything else. If these move in app.py,
# move them here in lockstep - a mismatch is how this tool would start crying
# wolf on honest kills or missing real breaches.
KILL_WINDOW_SECONDS = 300
KILL_RESPAWN_FLOOR_SECONDS = 30.0

# The honest baseline SECURITY_NOTES.md measured: roughly 300 kills/hour flat
# out. Everything SUSPICIOUS is expressed as a multiple of this so the numbers
# stay legible and the thresholds stay honest about being multiples of a
# measured thing, not magic.
HONEST_KILLS_PER_HOUR = 300

# SUSPICIOUS thresholds. Deliberately generous - this ranks accounts for a human,
# it does not clamp them, so the cost of setting these loose is only that a
# borderline account waits for the next, tighter pass, never that an honest
# player is refused. Every flag prints the raw number it tripped on, so you can
# see for yourself whether the line is in the right place and move it with data.
SUSPICIOUS_RATE_MULTIPLE = 4          # overall kills/hr above 4x honest (1,200/hr)
MIN_KILLS_FOR_RATE = 150              # ...but only judge a rate off a real sample
MIN_SPAN_SECONDS_FOR_RATE = 300       # ...measured over at least this long a span

# A boss is where fraud pays best and where honest rates are LOWEST: two placed
# instances on a 30s+ respawner, each taking 30-40s to actually kill, so an
# honest player clears well under two a minute. A per-boss rate this far above
# that is worth a look on its own, separate from the overall rate.
BOSS_HP_THRESHOLD = 1000              # max_hp at or above this counts as a boss
BOSS_SUSPICIOUS_PER_HOUR = 90         # boss-family kills/hr above this -> review
MIN_BOSS_KILLS_FOR_RATE = 20

# The known blind spot named in the notes: poisonslimesmall (and any placed==0
# enemy) is spawned by script, appears in no scene, and so is EXEMPT from the
# spawn ceiling. A real player does not farm only the lowest-value, ceiling-
# invisible enemy in the game; a character whose kills are mostly that is working
# the exemption on purpose.
EXEMPT_DOMINANCE_FRACTION = 0.80
MIN_KILLS_FOR_DOMINANCE = 100

# A boss killed far below the level anyone reaches it at. Compared to the MEDIAN
# level of everyone who has killed that boss, so the bar is set by the players
# themselves and not by a number picked here. Flag a claim below half the median
# and below an absolute floor, so a low-but-plausible level never trips it.
LOWLEVEL_BOSS_MEDIAN_FRACTION = 0.5
LOWLEVEL_BOSS_ABSOLUTE_FLOOR = 10
MIN_BOSS_KILLERS_FOR_MEDIAN = 5


# -- a finding is (tier, signal, detail); tier is "IMPOSSIBLE" or "SUSPICIOUS" --

def _ceiling_for(placed, window, respawn):
    """The live spawn ceiling for a window: placed x (W/respawn + 1). Mirrors
    /api/combat/kill exactly."""
    return int(placed * (window / respawn + 1))


def _worst_window_count(times, window):
    """The most kills that ever fall inside one `window`-second span, by the same
    [t-W, t] the live server uses. Two pointers over sorted times: O(n)."""
    times.sort()
    worst = 0
    j = 0
    for i, t in enumerate(times):
        while times[j] < t - window:
            j += 1
        worst = max(worst, i - j + 1)
    return worst


def analyze(con, window=KILL_WINDOW_SECONDS, respawn=KILL_RESPAWN_FLOOR_SECONDS):
    """
    Read kill_reports and return {user_id: {"username": str, "findings": [...]}}.

    Pure and read-through: it runs SELECTs and computes in Python, so a test can
    hand it a scratch connection and a cron can hand it the live one, and neither
    can change what it read.
    """
    # Names make a dossier readable; a missing users row (a deleted account with
    # kills still logged) degrades to the id, it does not break the pass.
    names = {}
    try:
        for r in con.execute("SELECT id, username FROM users"):
            names[r[0]] = r[1]
    except sqlite3.Error:
        pass

    rows = con.execute(
        "SELECT user_id, enemy_id, slot, level_at, at FROM kill_reports"
    ).fetchall()

    dossier = {}

    def note(user_id, tier, signal, detail):
        d = dossier.setdefault(user_id, {"username": names.get(user_id), "findings": []})
        d["findings"].append((tier, signal, detail))

    # Group once; everything below reads these groupings rather than re-querying.
    by_user = {}                    # user_id -> [at, ...]  (all kills, for rate)
    by_user_enemy = {}             # (user_id, enemy_id) -> [at, ...]
    boss_levels = {}               # enemy_id -> [level_at, ...]  (for the median)
    for user_id, enemy_id, _slot, level_at, at in rows:
        by_user.setdefault(user_id, []).append(at)
        by_user_enemy.setdefault((user_id, enemy_id), []).append(at)

    # --------------------------------------------------------------------- #
    # IMPOSSIBLE checks. These need the world; without gamedata they cannot
    # run, and a fraud watch that quietly stopped checking is a lie, so the
    # caller is warned and only the SUSPICIOUS behavioural checks proceed.
    # --------------------------------------------------------------------- #
    if _GAMEDATA_OK:
        for (user_id, enemy_id), times in by_user_enemy.items():
            enemy = gamedata.ENEMIES.get(enemy_id)

            # H2 - a reward-less enemy was paid for. The handler refuses these
            # with a 400 BEFORE writing a row (the slime-split exploit's fix), so
            # a row for one cannot exist on a correct server. Its presence means
            # that guard was skipped.
            if enemy is not None and not enemy.get("grants_rewards", True):
                note(user_id, "IMPOSSIBLE", "reward-less enemy paid",
                     "%d kill(s) of '%s', which the server marks grants_rewards=false "
                     "and refuses before writing. This row should not exist."
                     % (len(times), enemy_id))
                continue

            # H1 - the spawn ceiling, retrospectively. placed==0 is EXEMPT (the
            # runtime-spawned enemies the export cannot see); judging them here
            # would refuse a real fight, exactly as it would live.
            placed = gamedata.spawn_count_for(enemy_id) if gamedata.SPAWNS_EXPORTED else 0
            if placed > 0:
                ceiling = _ceiling_for(placed, window, respawn)
                worst = _worst_window_count(list(times), window)
                if worst > ceiling:
                    note(user_id, "IMPOSSIBLE", "spawn ceiling breached",
                         "%d kills of '%s' inside one %ds window; only %d exist, so "
                         "at most %d could have died. The live ceiling would have "
                         "refused this - it was not running."
                         % (worst, enemy_id, window, placed, ceiling))

    # --------------------------------------------------------------------- #
    # SUSPICIOUS checks. Legal today, far outside honest play. Ranked, never
    # enforced. Each prints the number it tripped on.
    # --------------------------------------------------------------------- #

    # S1 - overall sustained rate.
    for user_id, times in by_user.items():
        if len(times) < MIN_KILLS_FOR_RATE:
            continue
        span = max(times) - min(times)
        if span < MIN_SPAN_SECONDS_FOR_RATE:
            continue
        rate = len(times) / (span / 3600.0)
        if rate > HONEST_KILLS_PER_HOUR * SUSPICIOUS_RATE_MULTIPLE:
            note(user_id, "SUSPICIOUS", "sustained kill rate",
                 "%d kills over %.1fh = %.0f/hr, %.1fx the ~%d/hr honest baseline."
                 % (len(times), span / 3600.0, rate, rate / HONEST_KILLS_PER_HOUR,
                    HONEST_KILLS_PER_HOUR))

    # S2 - boss-family rate, and S4's median needs the same grouping, so gather
    # boss level_at here too.
    if _GAMEDATA_OK:
        for user_id, enemy_id, _slot, level_at, at in rows:
            enemy = gamedata.ENEMIES.get(enemy_id)
            if enemy is not None and int(enemy.get("max_hp", 0)) >= BOSS_HP_THRESHOLD:
                boss_levels.setdefault(enemy_id, []).append((user_id, level_at))

        for (user_id, enemy_id), times in by_user_enemy.items():
            enemy = gamedata.ENEMIES.get(enemy_id)
            if enemy is None or int(enemy.get("max_hp", 0)) < BOSS_HP_THRESHOLD:
                continue
            if len(times) < MIN_BOSS_KILLS_FOR_RATE:
                continue
            span = max(times) - min(times)
            if span <= 0:
                continue
            rate = len(times) / (span / 3600.0)
            if rate > BOSS_SUSPICIOUS_PER_HOUR:
                note(user_id, "SUSPICIOUS", "boss farmed fast",
                     "%d kills of '%s' (%d hp) over %.2fh = %.0f/hr; honest boss "
                     "clears sit well under %d/hr."
                     % (len(times), enemy_id, int(enemy.get("max_hp", 0)),
                        span / 3600.0, rate, BOSS_SUSPICIOUS_PER_HOUR))

        # S4 - a boss killed far below the level players reach it at.
        #
        # The bar is set by the KILLERS, one representative level each - each
        # player's LOWEST level_at, "the level they first beat it at" - and NOT
        # by raw kill rows. That distinction is the whole point: a cheat with a
        # thousand boss kills at level 4 would drag a row-weighted median down to
        # 4 and then look normal against it. One vote per player denies them that.
        rep = {}                              # enemy_id -> {user_id: min level_at}
        for enemy_id, pairs in boss_levels.items():
            per_killer = {}
            for user_id, level_at in pairs:
                per_killer[user_id] = min(level_at, per_killer.get(user_id, level_at))
            rep[enemy_id] = per_killer
        for enemy_id, per_killer in rep.items():
            if len(per_killer) < MIN_BOSS_KILLERS_FOR_MEDIAN:
                continue
            levels = sorted(per_killer.values())
            median = levels[len(levels) // 2]
            floor = max(LOWLEVEL_BOSS_ABSOLUTE_FLOOR, int(median * LOWLEVEL_BOSS_MEDIAN_FRACTION))
            for user_id, low in per_killer.items():
                if low < floor:
                    note(user_id, "SUSPICIOUS", "boss killed under-levelled",
                         "first killed '%s' at level %d; its killers reach it at a "
                         "median of level %d." % (enemy_id, low, median))

    # S3 - farming the ceiling-exempt enemy almost exclusively. One pass tallies
    # exempt kills per user; which enemies are exempt does not change within a
    # run, so it is decided once per enemy, not once per (user, enemy).
    if _GAMEDATA_OK:
        exempt_enemy = {}                     # enemy_id -> is it ceiling-exempt
        def _is_exempt(enemy_id):
            if enemy_id not in exempt_enemy:
                placed = gamedata.spawn_count_for(enemy_id) if gamedata.SPAWNS_EXPORTED else 0
                enemy = gamedata.ENEMIES.get(enemy_id)
                grants = enemy is None or enemy.get("grants_rewards", True)
                exempt_enemy[enemy_id] = (placed == 0 and grants)
            return exempt_enemy[enemy_id]

        exempt_by_user = {}
        for (u, enemy_id), t in by_user_enemy.items():
            if _is_exempt(enemy_id):
                exempt_by_user[u] = exempt_by_user.get(u, 0) + len(t)

        for user_id, times in by_user.items():
            total = len(times)
            if total < MIN_KILLS_FOR_DOMINANCE:
                continue
            exempt = exempt_by_user.get(user_id, 0)
            frac = exempt / float(total)
            if frac >= EXEMPT_DOMINANCE_FRACTION:
                note(user_id, "SUSPICIOUS", "farms the ceiling blind spot",
                     "%d of %d kills (%.0f%%) are ceiling-exempt enemies - the one "
                     "class of enemy the spawn ceiling cannot bound."
                     % (exempt, total, frac * 100))

    return dossier


def _rank(dossier):
    """Worst first: any IMPOSSIBLE outranks all SUSPICIOUS; then more findings
    before fewer. So the account a defence broke on is always at the top."""
    def key(item):
        _uid, d = item
        hard = sum(1 for t, _s, _x in d["findings"] if t == "IMPOSSIBLE")
        soft = sum(1 for t, _s, _x in d["findings"] if t == "SUSPICIOUS")
        return (-hard, -soft)
    return sorted(dossier.items(), key=key)


def main():
    ap = argparse.ArgumentParser(description="Read-only watch over claimed kills (E-3).")
    ap.add_argument("--db", default=os.environ.get("ELUSION_DB", "elusion.db"),
                    help="database to read (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--window", type=int, default=KILL_WINDOW_SECONDS,
                    help="spawn-ceiling window in seconds (must match the server)")
    ap.add_argument("--respawn", type=float, default=KILL_RESPAWN_FLOOR_SECONDS,
                    help="respawn floor in seconds (must match the server)")
    ap.add_argument("--limit", type=int, default=20,
                    help="how many flagged accounts to print (0 = all)")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless an IMPOSSIBLE (alarm) finding exists")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("[KILLWATCH] FAIL: no database at %s" % args.db, file=sys.stderr)
        sys.exit(2)

    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    try:
        dossier = analyze(con, window=args.window, respawn=args.respawn)
    except sqlite3.Error as exc:
        print("[KILLWATCH] FAIL: could not read the database: %s" % exc, file=sys.stderr)
        sys.exit(2)
    finally:
        con.close()

    ranked = _rank(dossier)
    hard_accounts = [(u, d) for u, d in ranked
                     if any(t == "IMPOSSIBLE" for t, _s, _x in d["findings"])]

    # --quiet is the cron voice: silent unless a wall came down. The SUSPICIOUS
    # list is a review, not a page, so it waits for a run you are watching.
    if args.quiet and not hard_accounts:
        sys.exit(0)

    shown = ranked if args.limit == 0 else ranked[:args.limit]

    print("=" * 68)
    print("  ELUSION KILLWATCH - %s" % args.db)
    if not _GAMEDATA_OK:
        print("  !! gamedata did not load (%s)" % _GAMEDATA_ERR)
        print("  !! IMPOSSIBLE checks are DARK; only behavioural review ran.")
    print("=" * 68)

    if not dossier:
        print("  clean - no account tripped a check.")
        print("=" * 68)
        sys.exit(0)

    for user_id, d in shown:
        who = d["username"] or "(deleted)"
        print("  #%-6s %s" % (user_id, who))
        for tier, signal, detail in d["findings"]:
            print("     [%-10s] %s" % (tier, signal))
            print("                  %s" % detail)
    if args.limit and len(ranked) > len(shown):
        print("  ... and %d more flagged account(s); raise --limit to see them."
              % (len(ranked) - len(shown)))

    print("=" * 68)
    print("  %d IMPOSSIBLE (a defence was not running - fix now), "
          "%d account(s) to review."
          % (len(hard_accounts), len(ranked) - len(hard_accounts)))
    print("=" * 68)

    # Non-zero ONLY on an alarm. A review pile is not a page.
    sys.exit(1 if hard_accounts else 0)


if __name__ == "__main__":
    main()
