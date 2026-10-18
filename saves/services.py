"""saves/services.py - the rules: which classes a character can be, what a pet
id and an explored map may hold, what a slot looks like to the game, and what
deleting a character asks first and keeps. Pure: no database, no request."""
import json

VALID_CLASSES = {"warrior", "mage", "tank", "healer"}

# The ceiling on one character's explored map. Every area in the game together
# is under 3 KB of bits before compression, and base64 of the deflated form is
# a few hundred bytes an area - so 64 KB is roughly twenty times the largest
# honest payload, and still small enough that a client cannot use the column as
# free storage.
MAX_EXPLORED_BYTES = 65536

# Sanity bounds on one area's dimensions, mirroring WorldMap.MAX_AREA_TILES.
MAX_EXPLORED_TILES = 262144


def pet_id(raw):
    """
    Validate an active_pet_id from a request body.

    Same shape as a bank item_id - any string the client's item registry knows,
    capped at 64 characters. Empty string is valid and means "no pet out".
    Returns None if the value can't be one, so the caller can 400 rather than
    store something meaningless.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, (str, int, float)):
        return None
    text = str(raw).strip()
    if len(text) > 64:
        return None
    return text


def explored(raw):
    """
    Validate an exploration map: {area_id: {w, h, ox, oy, bits}}.

    Returns a dict, or None when the value cannot be one.

    WHAT IS CHECKED IS THE SHAPE, NOT THE CONTENT. The bits are a compressed
    bitmask this server has no reason to decompress: there is no claim being
    made that anyone could benefit from. What matters is that the column
    contains a bounded amount of JSON of a known shape, so it cannot be used
    as a place to park arbitrary data and cannot make the save endpoint 500 on
    read-back.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None

    encoded = json.dumps(raw)
    if len(encoded) > MAX_EXPLORED_BYTES:
        return None

    out = {}
    for area, entry in raw.items():
        if not isinstance(area, str) or len(area) > 64:
            return None
        if not isinstance(entry, dict):
            return None

        width = entry.get("w")
        height = entry.get("h")
        if not isinstance(width, int) or not isinstance(height, int):
            return None
        if isinstance(width, bool) or isinstance(height, bool):
            return None
        if width <= 0 or height <= 0 or width * height > MAX_EXPLORED_TILES:
            return None

        for key in ("ox", "oy"):
            value = entry.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int):
                return None

        bits = entry.get("bits", "")
        if not isinstance(bits, str) or len(bits) > MAX_EXPLORED_BYTES:
            return None

        out[area] = {
            "w": width, "h": height,
            "ox": int(entry.get("ox", 0)), "oy": int(entry.get("oy", 0)),
            "bits": bits,
        }
    return out


def slot_view(row, equipment, explored_map):
    """One slot as GET /api/save lists it - the row, with its two JSON columns
    already read back."""
    return {
        "slot": row["slot"],
        "class_id": row["class_id"],
        "name": row["name"],
        "level": row["level"],
        "area": row["area"],
        "active_pet_id": row["active_pet_id"],
        "equipment": equipment,
        "explored": explored_map,
        "updated_at": row["updated_at"],
    }


def confirmed(typed, name):
    """Whether what the player typed is the character's name - any case, any
    spaces around it, and never an empty string."""
    typed = str(typed).strip().lower()
    return not (typed == "" or typed != str(name).strip().lower())


def in_trade(trade, user_id, slot):
    """Whether an open trade (or None) names this character, on either side."""
    return trade is not None and (
        (trade["a_user"] == user_id and trade["a_slot"] == slot)
        or (trade["b_user"] == user_id and trade["b_slot"] == slot))


def deletion_copy(row, carry, skills):
    """What is kept of a deleted character: its save row, its carry and its
    skills, as one JSON document."""
    return json.dumps({"save": dict(row), "carry_items": carry, "skills": skills})
