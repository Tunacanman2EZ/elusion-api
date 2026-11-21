"""minigames/data.py - the SQL (game 0.93.3; the kills 0.93.4; the best times
0.120.0; the top ten, game 0.169.0). Imports nothing. No commits: each route
commits its own."""


def entries_of(db, user_id):
    """{game: entered_at} for one account."""
    rows = db.execute("SELECT game, entered_at FROM minigame_entries WHERE user_id = ?",
                      (int(user_id),)).fetchall()
    return {row["game"]: int(row["entered_at"]) for row in rows}


def enter(db, user_id, game, now):
    """Write the day's going in (replacing the last)."""
    db.execute("INSERT OR REPLACE INTO minigame_entries (user_id, game, entered_at) VALUES (?, ?, ?)",
               (int(user_id), str(game), int(now)))


# How long a minigame's kill is kept: the day it counts in, and a week for the
# owner to read.
KILLS_KEPT_SECONDS = 7 * 24 * 60 * 60


def kills_since(db, user_id, game, since, boss):
    """How many paid kills the account has made in that minigame since
    `since` - bosses (boss=True) or the rest."""
    return db.execute(
        "SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND game = ? AND at >= ? AND boss = ?",
        (int(user_id), str(game), int(since), 1 if boss else 0),
    ).fetchone()[0]


def record_kill(db, user_id, slot, game, enemy_id, boss, now, pet=False, gold=0):
    """One paid kill (pet: it won its pet; gold: what its coins rolled into
    the chest are worth, 14 Nov) - and every one older than
    KILLS_KEPT_SECONDS out."""
    db.execute(
        "INSERT INTO minigame_kills (user_id, slot, game, enemy_id, boss, at, pet, gold)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(user_id), int(slot), str(game), str(enemy_id), 1 if boss else 0, int(now), 1 if pet else 0,
         int(gold)),
    )
    # THE PRUNE BELONGS HERE (20 Nov 2026). When pets_since() was added
    # (game 0.121.0) it went in between the INSERT above and this line, which
    # then sat after pets_since()'s return: never run, and `now` undefined
    # there (the CI's import check, F821). Kills were not pruned from then on.
    db.execute("DELETE FROM minigame_kills WHERE at < ?", (int(now) - KILLS_KEPT_SECONDS,))


def gold_since(db, user_id, since):
    """A DAY'S MINIGAME GOLD (services.py): what the account's minigames have
    paid since `since` - runs' gold at Leave and coins rolled into chests -
    every character and every minigame."""
    runs = db.execute("SELECT COALESCE(SUM(gold), 0) FROM minigame_runs WHERE user_id = ? AND finished_at > ?",
                      (int(user_id), int(since))).fetchone()[0]
    chests = db.execute("SELECT COALESCE(SUM(gold), 0) FROM minigame_kills WHERE user_id = ? AND at > ?",
                        (int(user_id), int(since))).fetchone()[0]
    return int(runs) + int(chests)


def pets_since(db, user_id, game, since):
    """How many pets the account's kills in that minigame have won since
    `since` (game 0.121.0)."""
    return db.execute(
        "SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND game = ? AND at >= ? AND pet = 1",
        (int(user_id), str(game), int(since)),
    ).fetchone()[0]


# THE HEALER'S RUN'S RUNS (game 0.99.0): one row a Go, finished once.

def starts_since(db, user_id, game, since):
    """When each of the account's runs of `game` started, since `since`."""
    rows = db.execute("SELECT started_at FROM minigame_runs WHERE user_id = ? AND game = ? AND started_at > ?",
                      (int(user_id), str(game), int(since))).fetchall()
    return [int(row[0]) for row in rows]


def start_run(db, user_id, slot, game, now):
    """A run begun; its id."""
    cur = db.execute("INSERT INTO minigame_runs (user_id, slot, game, started_at) VALUES (?, ?, ?, ?)",
                     (int(user_id), int(slot), str(game), int(now)))
    return int(cur.lastrowid)


def run_of(db, run_id):
    return db.execute("SELECT * FROM minigame_runs WHERE id = ?", (int(run_id),)).fetchone()


def finish_run(db, run_id, user_id, now, waves, bosses, goal, gold):
    """The run's end written - once: False when it was already finished.
    `bosses` is how many were beaten (0-3 since game 0.100.0's tiers; 0 or 1
    before), in the boss column."""
    cur = db.execute(
        "UPDATE minigame_runs SET finished_at = ?, waves = ?, boss = ?, goal = ?, gold = ? "
        "WHERE id = ? AND user_id = ? AND finished_at = 0",
        (int(now), int(waves), int(bosses), 1 if goal else 0, int(gold), int(run_id), int(user_id)))
    return cur.rowcount == 1


# THE BEST TIMES (game 0.120.0): one row an account and minigame.

def bests_of(db, user_id):
    """{game: best seconds} for one account."""
    rows = db.execute("SELECT game, seconds FROM minigame_bests WHERE user_id = ?", (int(user_id),)).fetchall()
    return {row["game"]: int(row["seconds"]) for row in rows}


def keep_best(db, user_id, game, seconds, now):
    """A run's time: kept when it beats the best. (best, whether it was new)."""
    best = bests_of(db, user_id).get(str(game), 0)
    if int(seconds) <= best:
        return best, False
    db.execute("INSERT OR REPLACE INTO minigame_bests (user_id, game, seconds, at) VALUES (?, ?, ?, ?)",
               (int(user_id), str(game), int(seconds), int(now)))
    return int(seconds), True


# =============================================================================
# THE TOP TEN (game 0.169.0)
# =============================================================================
# The owner: "i want a global highscore option ... for all minigames top 10
# on each". Two sources: the bests (one row an account and game - the
# Endless's and the Forge's time, Crownfall's maps, the Beholder's Table's
# score) and the runs (the Healer's Run's waves, Cut the Cloth's rounds: each
# account's best finished run). Ties go to whoever got there first. An
# account banned now is left out; a lifted or run-out ban counts again.

NOT_BANNED = "NOT (u.is_banned = 1 AND (u.ban_expires_at IS NULL OR u.ban_expires_at > :now))"

# Each account's best finished run of a game, ranked: waves (Cut the Cloth's
# rounds sit in the same column), then bosses, then the earliest.
_BEST_RUNS = (
    "SELECT r.user_id, u.username, r.waves AS value, r.boss AS bosses, r.finished_at AS at, "
    "ROW_NUMBER() OVER (PARTITION BY r.user_id ORDER BY r.waves DESC, r.boss DESC, r.finished_at ASC) AS nth "
    "FROM minigame_runs r JOIN users u ON u.id = r.user_id "
    "WHERE r.game = :game AND r.finished_at > 0 AND r.waves > 0 AND " + NOT_BANNED)


def top_bests(db, game, now, limit):
    """[{user_id, username, value, at}] - the best kept for `game`, highest
    first."""
    rows = db.execute(
        "SELECT b.user_id, u.username, b.seconds AS value, b.at FROM minigame_bests b "
        "JOIN users u ON u.id = b.user_id WHERE b.game = :game AND b.seconds > 0 AND " + NOT_BANNED +
        " ORDER BY b.seconds DESC, b.at ASC, b.user_id ASC LIMIT :limit",
        {"game": str(game), "now": int(now), "limit": int(limit)}).fetchall()
    return [{"user_id": int(r["user_id"]), "username": r["username"], "value": int(r["value"]),
             "at": int(r["at"])} for r in rows]


def top_runs(db, game, now, limit):
    """[{user_id, username, value, bosses, at}] - each account's best run of
    `game`, highest first."""
    rows = db.execute(
        "SELECT user_id, username, value, bosses, at FROM (" + _BEST_RUNS + ") WHERE nth = 1 "
        "ORDER BY value DESC, bosses DESC, at ASC, user_id ASC LIMIT :limit",
        {"game": str(game), "now": int(now), "limit": int(limit)}).fetchall()
    return [{"user_id": int(r["user_id"]), "username": r["username"], "value": int(r["value"]),
             "bosses": int(r["bosses"]), "at": int(r["at"])} for r in rows]


def place_in_bests(db, game, user_id, now):
    """(place, value) for one account's best of `game`, or None: 1 + how many
    accounts are ahead of it."""
    mine = db.execute("SELECT seconds, at FROM minigame_bests WHERE user_id = ? AND game = ? AND seconds > 0",
                      (int(user_id), str(game))).fetchone()
    if mine is None:
        return None
    ahead = db.execute(
        "SELECT COUNT(*) FROM minigame_bests b JOIN users u ON u.id = b.user_id "
        "WHERE b.game = :game AND b.user_id != :me AND " + NOT_BANNED +
        " AND (b.seconds > :value OR (b.seconds = :value AND (b.at < :at OR (b.at = :at AND b.user_id < :me))))",
        {"game": str(game), "me": int(user_id), "now": int(now), "value": int(mine["seconds"]),
         "at": int(mine["at"])}).fetchone()[0]
    return int(ahead) + 1, int(mine["seconds"])


def place_in_runs(db, game, user_id, now):
    """(place, value, bosses) for one account's best run of `game`, or None."""
    rows = db.execute(
        "SELECT user_id, value, bosses, at FROM (" + _BEST_RUNS + ") WHERE nth = 1",
        {"game": str(game), "now": int(now)}).fetchall()
    mine = next((r for r in rows if int(r["user_id"]) == int(user_id)), None)
    if mine is None:
        return None
    key = (-int(mine["value"]), -int(mine["bosses"]), int(mine["at"]), int(mine["user_id"]))
    ahead = sum(1 for r in rows if (-int(r["value"]), -int(r["bosses"]), int(r["at"]), int(r["user_id"])) < key)
    return ahead + 1, int(mine["value"]), int(mine["bosses"])


def players_in_bests(db, game, now):
    """How many accounts have a best kept for `game` (the owner: "this will
    help me see if anyone plays my minigames")."""
    return int(db.execute(
        "SELECT COUNT(*) FROM minigame_bests b JOIN users u ON u.id = b.user_id "
        "WHERE b.game = :game AND b.seconds > 0 AND " + NOT_BANNED,
        {"game": str(game), "now": int(now)}).fetchone()[0])


def players_in_runs(db, game, now):
    """How many accounts have finished a run of `game` that got anywhere."""
    return int(db.execute(
        "SELECT COUNT(DISTINCT r.user_id) FROM minigame_runs r JOIN users u ON u.id = r.user_id "
        "WHERE r.game = :game AND r.finished_at > 0 AND r.waves > 0 AND " + NOT_BANNED,
        {"game": str(game), "now": int(now)}).fetchone()[0])
