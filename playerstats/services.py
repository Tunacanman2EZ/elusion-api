"""playerstats/services.py - the player stats' arithmetic.

Pure functions: plain rows in (from data.py), plain numbers out, and the clock
passed in - so test_stats.py can lay down any history it likes and know the
answer to the second. Nothing here touches a database or a request.

WHAT IS COUNTED, AND WHO.
- PLAYERS ONLY. Staff (mod, dev) and the owner and co-owners are left out, by
  name and by role: the owner testing all day would be half of a small game's
  numbers. How many were left out is said, so the totals can be checked.
- SEEN is the latest thing the server kept for an account: a save, a login, a
  heartbeat, a kill (data.last_seen()), or its signup.
- DAYS ARE THE OWNER'S, not the server's: the game sends its UTC offset, and a
  day starts at the owner's midnight.

WHAT IT CANNOT SAY. Logins and per-kill rows are kept two weeks (app.py
LOGIN_LOG_RETENTION_SECONDS, KILL_LOG_RETENTION_SECONDS), so "played on this
day" and the monster counts reach back DAILY_DAYS at most. And "came back" is
read from the LAST time an account was seen: someone who came back once and
then left is counted, someone who never left but has not been seen since is
too - it is a floor, not a diary.
"""
import datetime

DAY = 86400
# The window a "new player" signed up in.
WINDOW_DAYS = 30
# Not seen for this long: they stopped.
QUIET_DAYS = 3
# How far back the day-by-day table goes - no further than the logs reach.
DAILY_DAYS = 14
# How long they played, from signing up to last seen, for those who stopped.
STAYED_BANDS = [("under 10 minutes", 10 * 60), ("10 to 60 minutes", 3600),
                ("1 to 24 hours", DAY), ("1 to 7 days", 7 * DAY), ("over a week", None)]
# The highest level any of their characters reached.
LEVEL_BANDS = [("no character", 0, 0), ("level 1", 1, 1), ("levels 2-4", 2, 4), ("levels 5-9", 5, 9),
               ("levels 10-19", 10, 19), ("level 20+", 20, None)]
MOST_MONSTERS = 5
MAX_OFFSET_MINUTES = 14 * 60


def clamp_offset(minutes):
    """The owner's offset from UTC in minutes, held to the world's (-14 h to
    +14 h); anything that is not a whole number is 0 (UTC)."""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return 0
    return max(-MAX_OFFSET_MINUTES, min(MAX_OFFSET_MINUTES, value))


def day_start(at, offset_minutes):
    """The owner's midnight that `at` falls after, as a unix time."""
    shift = offset_minutes * 60
    return (int(at) + shift) // DAY * DAY - shift


def day_label(start, offset_minutes):
    """'2026-10-15' for the owner's day starting at `start`."""
    local = datetime.datetime.fromtimestamp(int(start) + offset_minutes * 60, tz=datetime.timezone.utc)
    return local.strftime("%Y-%m-%d")


def players(accounts, left_out_names):
    """(the accounts that are players, how many were left out). Left out:
    any role but player, and the names given (the owner and co-owners)."""
    names = {str(name).casefold() for name in left_out_names if name}
    kept = [a for a in accounts if a["role"] == "player" and a["username"].casefold() not in names]
    return kept, len(accounts) - len(kept)


def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _band(value, bands):
    for label, top in bands:
        if top is None or value < top:
            return label
    return bands[-1][0]


def _level_band(level):
    for label, low, high in LEVEL_BANDS:
        if level >= low and (high is None or level <= high):
            return label
    return LEVEL_BANDS[-1][0]


def build_report(accounts, characters, last_seen, kill_totals, kills_recent, logins_recent,
                 enemies, now, offset_minutes=0, left_out_names=(), window_days=WINDOW_DAYS):
    """The whole report, as the GM panel's Player stats window reads it."""
    offset = clamp_offset(offset_minutes)
    now = int(now)
    window_days = max(1, min(365, int(window_days)))
    kept, left_out = players(accounts, left_out_names)
    ids = {a["id"] for a in kept}
    by_name = {a["username"].casefold(): a["id"] for a in kept}

    def seen(account):
        return max(account["created_at"], last_seen.get(account["id"], 0))

    def top_level(uid):
        return max((c["level"] for c in characters.get(uid, [])), default=0)

    today = day_start(now, offset)

    # ---- the players --------------------------------------------------------
    overview = {
        "total": len(kept),
        "staff_left_out": left_out,
        "new_today": sum(1 for a in kept if a["created_at"] >= today),
        "new_7d": sum(1 for a in kept if a["created_at"] >= now - 7 * DAY),
        "new_30d": sum(1 for a in kept if a["created_at"] >= now - 30 * DAY),
        "seen_today": sum(1 for a in kept if seen(a) >= today),
        "seen_7d": sum(1 for a in kept if seen(a) >= now - 7 * DAY),
        "seen_30d": sum(1 for a in kept if seen(a) >= now - 30 * DAY),
    }

    # ---- the new players' road ------------------------------------------------
    new = [a for a in kept if a["created_at"] >= now - window_days * DAY]
    day_old = [a for a in new if a["created_at"] <= now - DAY]
    week_old = [a for a in new if a["created_at"] <= now - 7 * DAY]
    first_kill_minutes = [
        (kill_totals[a["id"]][1] - a["created_at"]) / 60.0 for a in new
        if kill_totals.get(a["id"], (0, 0))[0] > 0 and kill_totals[a["id"]][1] >= a["created_at"]]
    road = {
        "days": window_days,
        "signed_up": len(new),
        "made_character": sum(1 for a in new if characters.get(a["id"])),
        "first_kill": sum(1 for a in new if kill_totals.get(a["id"], (0, 0))[0] > 0),
        "level_5": sum(1 for a in new if top_level(a["id"]) >= 5),
        "level_10": sum(1 for a in new if top_level(a["id"]) >= 10),
        "came_back": sum(1 for a in day_old if seen(a) >= a["created_at"] + DAY),
        "came_back_of": len(day_old),
        "stayed_week": sum(1 for a in week_old if seen(a) >= a["created_at"] + 7 * DAY),
        "stayed_week_of": len(week_old),
        "minutes_to_first_kill": None if not first_kill_minutes else round(median(first_kill_minutes), 1),
    }

    # ---- where the ones who stopped, stopped ---------------------------------
    stopped = [a for a in kept if seen(a) < now - QUIET_DAYS * DAY]
    stayed = {label: 0 for label, _ in STAYED_BANDS}
    levels = {label: 0 for label, _, _ in LEVEL_BANDS}
    areas = {}
    never_killed = 0
    for a in stopped:
        stayed[_band(seen(a) - a["created_at"], STAYED_BANDS)] += 1
        levels[_level_band(top_level(a["id"]))] += 1
        chars = characters.get(a["id"], [])
        if chars:
            last = max(chars, key=lambda c: c["updated_at"])
            areas[last["area"] or "?"] = areas.get(last["area"] or "?", 0) + 1
        if kill_totals.get(a["id"], (0, 0))[0] <= 0:
            never_killed += 1
    stopped_report = {
        "quiet_days": QUIET_DAYS,
        "players": len(stopped),
        "stayed": [{"label": label, "players": stayed[label]} for label, _ in STAYED_BANDS],
        "levels": [{"label": label, "players": levels[label]} for label, _, _ in LEVEL_BANDS],
        "areas": [{"area": area, "players": n} for area, n in sorted(areas.items(), key=lambda kv: (-kv[1], kv[0]))],
        "never_killed": never_killed,
    }

    # ---- day by day ------------------------------------------------------------
    first_day = today - (DAILY_DAYS - 1) * DAY
    played = {}
    for uid, _enemy, at in kills_recent:
        if uid in ids and at >= first_day:
            played.setdefault(day_start(at, offset), set()).add(uid)
    for name, at in logins_recent:
        uid = by_name.get(str(name).casefold())
        if uid is not None and at >= first_day:
            played.setdefault(day_start(at, offset), set()).add(uid)
    signed = {}
    for a in kept:
        if a["created_at"] >= first_day:
            start = day_start(a["created_at"], offset)
            signed[start] = signed.get(start, 0) + 1
            played.setdefault(start, set()).add(a["id"])
    daily = [{"day": day_label(start, offset), "new": signed.get(start, 0), "played": len(played.get(start, ()))}
             for start in (today - k * DAY for k in range(DAILY_DAYS))]

    # ---- the monsters ----------------------------------------------------------
    counts = {}
    for uid, enemy, at in kills_recent:
        if uid in ids and at >= now - DAILY_DAYS * DAY:
            entry = counts.setdefault(enemy, [0, set()])
            entry[0] += 1
            entry[1].add(uid)
    huntable = {eid: row for eid, row in enemies.items()
                if row.get("grants_rewards", True) and int(row.get("placed_count", 1) or 0) > 0}

    def monster(eid):
        row = enemies.get(eid) or {}
        kills, who = counts.get(eid, [0, set()])
        return {"enemy": eid, "name": str(row.get("display_name") or eid), "kills": kills, "players": len(who)}

    hunted = sorted(counts, key=lambda eid: (-counts[eid][0], eid))
    least = sorted(huntable, key=lambda eid: (counts.get(eid, [0])[0], eid))
    monsters = {
        "days": DAILY_DAYS,
        "kills": sum(c[0] for c in counts.values()),
        "most": [monster(eid) for eid in hunted[:MOST_MONSTERS]],
        "least": [monster(eid) for eid in least[:MOST_MONSTERS]],
        "never": sum(1 for eid in huntable if eid not in counts),
        "kinds": len(huntable),
    }

    # ---- the classes -----------------------------------------------------------
    classes = {}
    for uid in ids:
        for c in characters.get(uid, []):
            entry = classes.setdefault(c["class_id"] or "?", [0, 0])
            entry[0] += 1
            entry[1] = max(entry[1], c["level"])
    class_rows = [{"class": name, "characters": n, "top_level": top}
                  for name, (n, top) in sorted(classes.items(), key=lambda kv: (-kv[1][0], kv[0]))]

    return {
        "now": now,
        "offset": offset,
        "players": overview,
        "road": road,
        "quit": stopped_report,
        "daily": daily,
        "monsters": monsters,
        "classes": class_rows,
    }
