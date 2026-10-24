"""serverhealth/services.py - the rules: how a pile of presence_health minutes
becomes the last minute, a day and a week; what the week adds up to in a month
of data; when one area needs sending only what is near; whether the kill
receipts are ready; which routes are slowest. Pure: no database, no request,
no clock but the one handed in."""
import json

DAY = 86400
WEEK = 7 * DAY
GIB = 1024 ** 3
MONTH_SECONDS = 30 * DAY

# PRESENCE IS QUIET when its newest row is older than three of its minutes:
# one late write is a busy disk, three is a process that stopped.
REPORTING_SECONDS = 180

# THE MONTH'S ALLOWANCE. DigitalOcean's 1 GB / 1 vCPU droplet (DEPLOY.md, "The
# live server") includes 1,000 GiB of outbound data a month, pooled across the
# account's droplets, and charges $0.01 a GiB past it (docs.digitalocean.com,
# "Bandwidth Billing", read 24 Oct 2026). Inbound is free. A bigger droplet
# comes with more: change it here with the droplet.
TRANSFER_ALLOWANCE_GIB = 1000
OVERAGE_PER_GIB = 0.01

# WHEN ONE AREA NEEDS SENDING ONLY WHAT IS NEAR. swarm.py, game 0.76.0, one
# area: 32 players sent 1.3 MB/s between them, 64 sent 4.0, 128 sent 13.4 -
# the square of the crowd. From CROWD_WATCH in one area it is worth watching;
# from CROWD_NOW (about 7.5 MB/s, 18 TiB in a month held) it is the next job.
# And the same by the bill: the week's rate held for a month past half the
# allowance is "watch", past all of it "now".
CROWD_WATCH = 32
CROWD_NOW = 96
WATCH_SHARE = 0.5

# THE TICK IS STEADY at this many beats a second or more and no late ones
# (presence.py TICK_SECONDS is 0.1; tick_loop counts the late).
TICK_STEADY = 9.5

# THE KILL RECEIPTS ARE READY (DEPLOY.md, "Kill receipts": "Near 100%, and no
# monster in the 'never seen' list with many kills, is the moment") when, over
# killwatch.py's week: the books have run the whole week, at least
# RECEIPTS_MIN_KILLS kills were paid, RECEIPTS_READY_SHARE of them had the
# books' receipt, and no one monster was never seen more than
# RECEIPTS_MONSTER_SHARE of the kills (or RECEIPTS_MONSTER_FLOOR, for a small
# week) - a monster the books cannot see is a land the map does not explain,
# and switching on would stop paying for it.
RECEIPTS_MIN_KILLS = 200
RECEIPTS_READY_SHARE = 0.98
RECEIPTS_MONSTER_SHARE = 0.01
RECEIPTS_MONSTER_FLOOR = 3
# An hour's slack on "the whole week": the books' first row is not on the hour.
RECEIPTS_YOUNG_SLACK = 3600
# How many of the never-seen monsters the answer names.
RECEIPTS_NAMED = 3

# THE ROUTES: the slowest few by p99, among those asked at least
# API_MIN_COUNT times (a route asked twice has no p99 worth a line).
API_SLOWEST = 5
API_MIN_COUNT = 10


def crowd_of(row):
    """A row's crowd, [[area, players], ...] biggest first - [] if unreadable."""
    try:
        crowd = json.loads(row.get("crowd") or "[]")
    except (TypeError, ValueError):
        return []
    return [[str(pair[0]), int(pair[1])] for pair in crowd
            if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[1], int)]


def _rate(amount, seconds):
    return round(amount / seconds, 1) if seconds > 0 else 0.0


def last_minute(row, now):
    """The newest row as the panel shows it, or None before the first."""
    if row is None:
        return None
    seconds = float(row.get("seconds") or 0.0)
    age = max(0, int(now) - int(row.get("at") or 0))
    return {
        "at": int(row.get("at") or 0),
        "age_seconds": age,
        "reporting": age <= REPORTING_SECONDS,
        "connections": int(row.get("connections") or 0),
        "players": int(row.get("players") or 0),
        "areas": int(row.get("areas") or 0),
        "crowd": crowd_of(row),
        "ticks_per_second": round(int(row.get("ticks") or 0) / seconds, 2) if seconds > 0 else 0.0,
        "late_ticks": int(row.get("late_ticks") or 0),
        "bytes_per_second": _rate(int(row.get("bytes_out") or 0), seconds),
        "net_per_second": (_rate(int(row["net_out"]), seconds) if row.get("net_out") is not None else None),
        "cpu": row.get("cpu"),
        "load1": row.get("load1"),
        "mem_free_mb": row.get("mem_free_mb"),
    }


def window(rows):
    """A day's or a week's rows summed: how long they cover, the most players
    and the biggest crowd at any minute, data out and its rate, the ticks,
    the busiest the core and the machine got and the least memory free."""
    seconds = sum(float(r.get("seconds") or 0.0) for r in rows)
    net_rows = [r for r in rows if r.get("net_out") is not None]
    net_seconds = sum(float(r.get("seconds") or 0.0) for r in net_rows)
    net_out = sum(int(r["net_out"]) for r in net_rows)
    bytes_out = sum(int(r.get("bytes_out") or 0) for r in rows)
    ticks = sum(int(r.get("ticks") or 0) for r in rows)
    peak_players, peak_area, peak_area_players = 0, "", 0
    for r in rows:
        peak_players = max(peak_players, int(r.get("players") or 0))
        crowd = crowd_of(r)
        if crowd and crowd[0][1] > peak_area_players:
            peak_area, peak_area_players = crowd[0][0], crowd[0][1]
    loads = [float(r["load1"]) for r in rows if r.get("load1") is not None]
    cpus = [float(r["cpu"]) for r in rows if r.get("cpu") is not None]
    mems = [int(r["mem_free_mb"]) for r in rows if r.get("mem_free_mb") is not None]
    return {
        "samples": len(rows),
        "seconds": round(seconds, 1),
        "peak_players": peak_players,
        "peak_area": peak_area,
        "peak_area_players": peak_area_players,
        "bytes_out": bytes_out,
        "bytes_per_second": _rate(bytes_out, seconds),
        "net_out": net_out if net_rows else None,
        "net_per_second": _rate(net_out, net_seconds) if net_rows else None,
        "ticks_per_second": round(ticks / seconds, 2) if seconds > 0 else 0.0,
        "late_ticks": sum(int(r.get("late_ticks") or 0) for r in rows),
        "peak_cpu": max(cpus) if cpus else None,
        "peak_load": max(loads) if loads else None,
        "least_mem_free_mb": min(mems) if mems else None,
    }


def month(week, allowance_gib=TRANSFER_ALLOWANCE_GIB):
    """The week's data rate held for thirty days, in GiB, against the
    allowance: the machine's own network out where it was read (everything -
    the website, the API, the browser build, the sockets), presence.py's
    frames where it was not. None before there is a week's minute to go by."""
    if not week or not week.get("samples"):
        return None
    if week.get("net_per_second") is not None:
        rate, source = float(week["net_per_second"]), "machine"
    else:
        rate, source = float(week.get("bytes_per_second") or 0.0), "presence"
    gib = rate * MONTH_SECONDS / GIB
    return {
        "source": source,
        "bytes_per_second": round(rate, 1),
        "gib": round(gib, 1),
        "allowance_gib": allowance_gib,
        "share": round(gib / allowance_gib, 3) if allowance_gib else None,
        "over_cost": round(max(0.0, gib - allowance_gib) * OVERAGE_PER_GIB, 2),
    }


def crowd_verdict(week, projected):
    """Whether one area needs sending only what is near: "unknown" before any
    minute, then "fine", "watch" or "now" by the week's biggest crowd and the
    month's data - and which of the two said so."""
    if not week or not week.get("samples"):
        return {"verdict": "unknown", "by": "", "watch_at": CROWD_WATCH, "now_at": CROWD_NOW}
    crowd = int(week.get("peak_area_players") or 0)
    share = float((projected or {}).get("share") or 0.0)
    verdict, by = "fine", ""
    if crowd >= CROWD_NOW or share >= 1.0:
        verdict, by = "now", "crowd" if crowd >= CROWD_NOW else "data"
    elif crowd >= CROWD_WATCH or share >= WATCH_SHARE:
        verdict, by = "watch", "crowd" if crowd >= CROWD_WATCH else "data"
    return {"verdict": verdict, "by": by, "watch_at": CROWD_WATCH, "now_at": CROWD_NOW}


def tick_verdict(last, day):
    """"steady" at TICK_STEADY or more a second with no late beat in the last
    minute; "late" otherwise; "unknown" with no minute to go by."""
    if last is None:
        return {"verdict": "unknown", "per_second": None, "late_today": int((day or {}).get("late_ticks") or 0)}
    steady = float(last["ticks_per_second"]) >= TICK_STEADY and int(last["late_ticks"]) == 0
    return {"verdict": "steady" if steady else "late", "per_second": last["ticks_per_second"],
            "late_today": int((day or {}).get("late_ticks") or 0)}


def receipts(backing, on, min_build, receipt_build, days, now):
    """The kill receipts as the Health tab shows them: the switch, the week
    put to the books (killwatch.py's receipts_backing()), and a verdict -
    "on", "no_books", "young", "few", "short", "monster" or "ready" - and
    whether the oldest build allowed in is still below the receipts' own."""
    out = {"on": bool(on), "days": int(days), "min_client_build": int(min_build),
           "receipt_build": int(receipt_build), "min_build_low": int(min_build) < int(receipt_build),
           "ready_share": RECEIPTS_READY_SHARE, "min_kills": RECEIPTS_MIN_KILLS}
    if backing is None:
        out.update({"verdict": "on" if on else "no_books", "paid": 0, "backed": 0, "share": None,
                    "short": 0, "not_due": 0, "never": 0, "never_by_enemy": [], "books_days": 0})
        return out
    paid = int(backing.get("paid", 0))
    backed = int(backing.get("backed", 0))
    never_by = sorted(((str(k), int(v)) for k, v in (backing.get("never_by_enemy") or {}).items()),
                      key=lambda kv: (-kv[1], kv[0]))
    since = int(backing.get("since", now))
    books_days = round(max(0, int(now) - since) / DAY, 1)
    out.update({"paid": paid, "backed": backed, "share": round(backed / paid, 4) if paid else None,
                "short": int(backing.get("short", 0)), "not_due": int(backing.get("not_due", 0)),
                "never": int(backing.get("never", 0)),
                "never_by_enemy": [[k, v] for k, v in never_by[:RECEIPTS_NAMED]], "books_days": books_days})
    worst = never_by[0][1] if never_by else 0
    if on:
        verdict = "on"
    elif since > int(now) - int(days) * DAY + RECEIPTS_YOUNG_SLACK:
        verdict = "young"
    elif paid < RECEIPTS_MIN_KILLS:
        verdict = "few"
    elif backed / paid < RECEIPTS_READY_SHARE:
        verdict = "short"
    elif worst > max(RECEIPTS_MONSTER_FLOOR, RECEIPTS_MONSTER_SHARE * paid):
        verdict = "monster"
    else:
        verdict = "ready"
    out["verdict"] = verdict
    return out


def routes(summary):
    """app.py's /api/metrics, cut to what one line says: how long this worker
    has been up, its requests and server errors, and its API_SLOWEST slowest
    routes by p99."""
    summary = summary or {}
    endpoints = summary.get("endpoints") or {}
    errors = 0
    rows = []
    for key, stats in endpoints.items():
        count = int(stats.get("count", 0))
        errors += int(round(count * float(stats.get("error_rate", 0.0))))
        if count >= API_MIN_COUNT:
            rows.append({"route": str(key), "p99_ms": float(stats.get("p99_ms", 0.0)), "count": count})
    rows.sort(key=lambda row: (-row["p99_ms"], row["route"]))
    return {"uptime_seconds": int(summary.get("uptime_seconds", 0)),
            "requests": int(summary.get("total_requests", 0)), "errors": errors, "slowest": rows[:API_SLOWEST]}


def machine(last, cores, free, total, db_bytes):
    """The machine now: its cores, the load and memory presence.py read in
    its last minute, the database disk's free and total, and the database's
    size."""
    mib = 1024 * 1024
    return {
        "cores": cores,
        "load1": (last or {}).get("load1"),
        "mem_free_mb": (last or {}).get("mem_free_mb"),
        "disk_free_gb": round(free / GIB, 1) if free is not None else None,
        "disk_total_gb": round(total / GIB, 1) if total is not None else None,
        "db_mb": round(db_bytes / mib, 1) if db_bytes is not None else None,
    }
