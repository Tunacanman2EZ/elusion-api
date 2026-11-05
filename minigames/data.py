"""minigames/data.py - the SQL (game 0.93.3; the kills 0.93.4; the best times
0.120.0). Imports nothing. No commits: each route commits its own."""


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


def record_kill(db, user_id, slot, game, enemy_id, boss, now, pet=False):
    """One paid kill (pet: it won its pet) - and every one older than
    KILLS_KEPT_SECONDS out."""
    db.execute(
        "INSERT INTO minigame_kills (user_id, slot, game, enemy_id, boss, at, pet) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(user_id), int(slot), str(game), str(enemy_id), 1 if boss else 0, int(now), 1 if pet else 0),
    )


def pets_since(db, user_id, game, since):
    """How many pets the account's kills in that minigame have won since
    `since` (game 0.121.0)."""
    return db.execute(
        "SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND game = ? AND at >= ? AND pet = 1",
        (int(user_id), str(game), int(since)),
    ).fetchone()[0]
    db.execute("DELETE FROM minigame_kills WHERE at < ?", (int(now) - KILLS_KEPT_SECONDS,))


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
