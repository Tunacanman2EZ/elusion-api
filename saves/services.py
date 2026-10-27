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
# A NEW CHARACTER'S STARTER POTIONS (game 0.92.0): the owner's first five
# minutes walked, a level 1 character leaving town with nothing to drink.
# On the hotbar's first keys, in this order: health for everyone, and mana
# for the two classes whose attacks spend it. Only for a slot's FIRST
# character (routes.py): a slot that has had one deleted gets none, so
# deleting and making again is not a way to mint potions.
STARTER_POTIONS = (("tinyhealthpotion", 5),)
STARTER_MANA = (("tinymanapotion", 5),)
STARTER_MANA_CLASSES = {"mage", "healer"}

MAX_EXPLORED_BYTES = 65536

# Sanity bounds on one area's dimensions, mirroring WorldMap.MAX_AREA_TILES.
MAX_EXPLORED_TILES = 262144


def starter_kit(class_id):
    """[(item_id, quantity)] a new character of class_id starts with, in hotbar
    key order."""
    kit = list(STARTER_POTIONS)
    if class_id in STARTER_MANA_CLASSES:
        kit += list(STARTER_MANA)
    return kit


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


def slot_view(row, equipment, explored_map, food_buff=None):
    """One slot as GET /api/save lists it - the row, with its two JSON columns
    already read back, and its meal in force (food_buff_view()) or None."""
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
        "food_buff": food_buff,
    }


def food_buff_view(row):
    """A food_buffs row as the character's load carries it, or None: {"item_id",
    "kind" (the name), "amount", "ends_at"}. THE SAME SHAPE AS THE CONSUME
    ANSWER'S (consuming/services.py food_buff_view(), which writes the row) -
    test_larder.py holds the two equal, since a package cannot import
    another's."""
    if row is None:
        return None
    return {"item_id": str(row["item_id"]), "kind": str(row["kind"]),
            "amount": int(row["amount"]), "ends_at": int(row["ends_at"])}


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
