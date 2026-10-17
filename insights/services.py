"""insights/services.py - the rules of the land insights.

Pure functions: rows and a clock in, answers out, so test_insights.py can hold
each rule on its own.

THE NEXT STIR (choose()): of the lands that did not just stir, the ones where
no new player (under NEW_PLAYER_LEVEL, a kill in the last NEWCOMER_WINDOW) is
learning - or all of them, if every land has one - and of those the least
hunted in the last day; a tie settled by the caller's seeded Random, so the
same tick always picks the same land, whoever asks.

A LAND'S STATUS (status()), from its share of the week's kills against an
even share (one in seven): none at all is "ignored", under half an even share
"quiet", twice or more "busy" (overfarmed), else "healthy". Under FEW_KILLS in
the week, it is said to be a rough read.
"""

DAY = 86400
HOUR = 3600
WEEK = 7 * DAY
# A character under this level is still learning the game.
NEW_PLAYER_LEVEL = 10
# A new player with a kill in a land this recently is taken to be there now.
NEWCOMER_WINDOW = 2 * HOUR
BUSY_SHARE = 2.0
QUIET_SHARE = 0.5
FEW_KILLS = 30
# How far back the busy hours look, and how many days make them worth saying.
HOURS_DAYS = 14
FEW_DAYS = 3
SAMPLE_SECONDS = 300


def per_land(rows, element_of, lands):
    """{land: sum} from (enemy_id, count) rows, every land present."""
    out = {land: 0 for land in lands}
    for enemy, count in rows:
        land = int(element_of(enemy) or 0)
        if land in out:
            out[land] += int(count)
    return out


def people_per_land(rows, element_of, lands):
    """{land: how many different people} from (enemy_id, user_id) rows."""
    seen = {land: set() for land in lands}
    for enemy, user in rows:
        land = int(element_of(enemy) or 0)
        if land in seen:
            seen[land].add(int(user))
    return {land: len(users) for land, users in seen.items()}


def status(kills_week, total_week, land_count):
    if kills_week <= 0:
        return "ignored"
    share = kills_week / float(max(1, total_week))
    even = 1.0 / max(1, land_count)
    if share >= BUSY_SHARE * even:
        return "busy"
    if share < QUIET_SHARE * even:
        return "quiet"
    return "healthy"


def choose(candidates, hunts, newcomers, chooser):
    """(land, passed over): the next land to stir by the rule above, and the
    lands hunted less that were passed over for their new players."""
    candidates = sorted(candidates)
    calm = [land for land in candidates if not newcomers.get(land)] or candidates
    least = min(hunts.get(land, 0) for land in calm)
    land = chooser.choice([e for e in calm if hunts.get(e, 0) == least])
    passed = [e for e in candidates if e not in calm and hunts.get(e, 0) < least]
    return land, passed


def reason(land, hunts, newcomers, passed, candidates, lands):
    """Why that land, in a sentence."""
    kills = int(hunts.get(land, 0))
    said = "the least hunted in the last day (%d kill%s)" % (kills, "" if kills == 1 else "s")
    if all(newcomers.get(e) for e in candidates):
        said += " - every land has new players in it, so the least hunted of all"
    elif passed:
        names = ["%s (%d new player%s)" % (lands[e].capitalize(), newcomers[e], "" if newcomers[e] == 1 else "s")
                 for e in passed]
        said += " without new players in it - passing over %s, where they are learning" % ", ".join(names)
    return "%s: %s." % (lands[land].capitalize(), said)


def busiest_hour(rows, element_of, lands):
    """{land: the hour of the day (UTC) its monsters are killed most, or
    None} from (enemy_id, hour, kills) rows."""
    counts = {land: [0] * 24 for land in lands}
    for enemy, hour, kills in rows:
        land = int(element_of(enemy) or 0)
        if land in counts:
            counts[land][int(hour) % 24] += int(kills)
    out = {}
    for land, by_hour in counts.items():
        most = max(by_hour)
        out[land] = by_hour.index(most) if most > 0 else None
    return out


def online_by_hour(sampled, first_bucket, now):
    """{"players": [24 averages, by UTC hour], "days": how many days counted}
    - over the last HOURS_DAYS, from when the counting began. A five minutes
    with no row is nobody."""
    if first_bucket is None:
        return {"players": [0.0] * 24, "days": 0.0}
    start = max(first_bucket, (now - HOURS_DAYS * DAY) // SAMPLE_SECONDS)
    end = now // SAMPLE_SECONDS
    sums = [0] * 24
    counts = [0] * 24
    for bucket in range(start, end):
        hour = (bucket * SAMPLE_SECONDS // HOUR) % 24
        sums[hour] += sampled.get(bucket, 0)
        counts[hour] += 1
    return {"players": [round(sums[h] / float(counts[h]), 2) if counts[h] else 0.0 for h in range(24)],
            "days": round(max(0, end - start) * SAMPLE_SECONDS / float(DAY), 1)}


def last_event_for(events, lands):
    """{land: when an event last began that covered its monsters - every
    monster's, or its own; a stir event of it}."""
    out = {land: None for land in lands}
    for kind, covered, starts in events:
        hit = list(lands) if (kind in ("xp", "loot") and int(covered) == 0) else \
            [int(covered)] if int(covered) in out else []
        for land in hit:
            if out[land] is None or starts > out[land]:
                out[land] = int(starts)
    return out


def lands_report(lands, day, week, players, newcomers, stirred, events, busiest):
    """One row per land, in order: the numbers and the status."""
    total = sum(week.values())
    rows = []
    for land in sorted(lands):
        rows.append({"element": land, "name": lands[land], "kills_day": int(day.get(land, 0)),
                     "kills_week": int(week.get(land, 0)), "players_week": int(players.get(land, 0)),
                     "newcomers_now": int(newcomers.get(land, 0)), "last_stirred_at": stirred.get(land),
                     "last_event_at": events.get(land), "busiest_hour": busiest.get(land),
                     "share": round(week.get(land, 0) / float(total), 3) if total else 0.0,
                     "status": status(int(week.get(land, 0)), total, len(lands))})
    return rows
