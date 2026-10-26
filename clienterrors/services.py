"""clienterrors/services.py - the rules, pure: what a game's report must look
like to be kept, and how the rows are added up for the owner.

Nothing here touches a database or a request; every function takes plain
values and gives plain values back, so the suite asks them directly.
"""
import re

# A POST carries at most this many errors; the rest are dropped, and said.
ERRORS_PER_POST = 20
# One account's distinct errors a day, at most. A broken build can raise one
# error in a hundred places; past this the next are dropped, the count of
# those already kept still rises. It is what stops one account filling a disk.
ROWS_PER_DAY = 60
# What one row's count can reach in a day, and what one report can add.
TIMES_MOST = 1_000_000
REPORT_TIMES_MOST = 100_000
# The longest "where" and "what" kept, after cleaning.
WHERE_MOST = 200
WHAT_MOST = 400
# Kept this many days.
DAYS_KEPT = 14
# The owner's list: the hours it looks back (default and most) and its length.
HOURS_DEFAULT = 24
HOURS_MOST = 24 * DAYS_KEPT
SHOWN = 50

DAY = 86400

# WHERE IS A SCRIPT AND A LINE in the game, "res://src/ui/x.gd:12", or the
# word the engine uses when it has no file. Anything else is not from the game.
_WHERE = re.compile(r"^(res://[A-Za-z0-9_./-]{1,180}(:[0-9]{1,6})?|<[A-Za-z0-9 _-]{1,40}>)$")
_VERSION = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$")
_AREA = re.compile(r"^[a-z0-9_]{0,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_what(raw):
    """The message as one line: control characters become spaces, runs of
    spaces one, trimmed, cut to WHAT_MOST. "" for nothing usable."""
    if not isinstance(raw, str):
        return ""
    text = _CONTROL.sub(" ", raw)
    text = re.sub(r" {2,}", " ", text).strip()
    return text[:WHAT_MOST]


def clean_report(body):
    """A game's POST body, as rows to keep: (version, area, errors, dropped),
    where errors is a list of (where_at, what, times). None when the body is
    not a report at all (the route's 400). A bad error inside a good report is
    dropped and counted, not a refusal: one odd line must not lose the rest."""
    if not isinstance(body, dict):
        return None
    version = body.get("version", "")
    area = body.get("area", "")
    errors = body.get("errors")
    if not isinstance(version, str) or not _VERSION.match(version):
        return None
    if not isinstance(area, str) or not _AREA.match(area):
        area = ""
    if not isinstance(errors, list):
        return None
    kept = []
    dropped = max(0, len(errors) - ERRORS_PER_POST)
    seen = {}
    for entry in errors[:ERRORS_PER_POST]:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        where = entry.get("where", "")
        what = clean_what(entry.get("what", ""))
        times = entry.get("count", 1)
        # Godot sends every number as a float; a whole one is fine, a bool is not.
        if isinstance(times, bool) or not isinstance(times, (int, float)) or times != int(times):
            dropped += 1
            continue
        times = int(times)
        if not isinstance(where, str) or len(where) > WHERE_MOST or not _WHERE.match(where) \
                or what == "" or times < 1:
            dropped += 1
            continue
        times = min(times, REPORT_TIMES_MOST)
        key = (where, what)
        if key in seen:
            seen[key] = min(seen[key] + times, REPORT_TIMES_MOST)
        else:
            seen[key] = times
            kept.append(key)
    return version, area, [(where, what, seen[(where, what)]) for where, what in kept], dropped


def build_of(header):
    """The game's build from X-Elusion-Build, 0 when it says none."""
    try:
        value = int(str(header or "").strip())
    except ValueError:
        return 0
    return value if 0 < value < 1_000_000 else 0


def hours_asked(raw):
    """?hours= for the owner's list: HOURS_DEFAULT when absent or unreadable,
    held between 1 and HOURS_MOST."""
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return HOURS_DEFAULT
    return max(1, min(HOURS_MOST, hours))


def group(rows):
    """The rows as the owner reads them: one entry per error (where and what),
    with how many players hit it, how many times, the versions and areas it
    was seen in and when first and last - worst first: most players, then most
    times, then the newest. No account appears in it."""
    groups = {}
    for row in rows:
        key = (row["where_at"], row["what"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"where": row["where_at"], "what": row["what"], "players": set(),
                               "times": 0, "versions": set(), "areas": set(),
                               "first_at": row["first_at"], "last_at": row["last_at"]}
        g["players"].add(row["user_id"])
        g["times"] = min(g["times"] + int(row["times"]), TIMES_MOST * 1000)
        if row["version"]:
            g["versions"].add(row["version"])
        if row["area"]:
            g["areas"].add(row["area"])
        g["first_at"] = min(g["first_at"], row["first_at"])
        g["last_at"] = max(g["last_at"], row["last_at"])
    out = []
    for g in groups.values():
        out.append({"where": g["where"], "what": g["what"], "players": len(g["players"]),
                    "times": g["times"], "versions": sorted(g["versions"], key=_version_key, reverse=True),
                    "areas": sorted(g["areas"]), "first_at": g["first_at"], "last_at": g["last_at"]})
    out.sort(key=lambda e: (-e["players"], -e["times"], -e["last_at"]))
    return out


def _version_key(version):
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)
