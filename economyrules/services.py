"""economyrules/services.py - the rules: which economy numbers the owner may
set, what each one is when nobody has set it, the bounds it must stay in, and
value(), the one place every other package reads a number from.

SKELETON WRITTEN BEFORE THE WORK WAS SPLIT (0.74.0); filled in by the economy
work. value() is the interface every other package calls, and its signature is
the skeleton's: value(db, key). What it reads is the owner's table
(economy_rules, economyrules/schema.py) through a short per-process cache, and
a key nobody has set answers its default - exactly the number the game held
before any of this existed, so a server with an empty table plays as it did.

THE ONE FUNCTION HERE THAT TOUCHES A DATABASE IS value() (and owner_value()
under it), because the skeleton put it here and other packages import it from
here. It reads through data.py like any route would; everything else in this
file is pure.
"""
import math
import sqlite3
import threading
import time

import gamedata

from . import data

# THE SHOP THE TWO SHOP RULES ARE FOR. The owner's words were "changing the
# trade tax or a price", and the general store is the shop: it is the only one
# there is, and the rule's about says so. A second vendor later keeps its own
# catalogue numbers until it is given rules of its own.
SHOP_ID = "generalstore"


def _shop_number(field, fallback):
    """The general store's own number for `field` - or, in a catalogue without
    one, the first shop's, which is what the skeleton read."""
    shops = gamedata.SHOPS if isinstance(gamedata.SHOPS, dict) else {}
    ordered = ([shops[SHOP_ID]] if SHOP_ID in shops else []) + list(shops.values())
    for shop in ordered:
        try:
            return float(shop.get(field, fallback))
        except (TypeError, ValueError):
            return float(fallback)
    return float(fallback)


# key -> default, lowest, highest, what it does. A key is never renamed: a
# row in the owner's table names it.
#
# THE TAX'S FLOOR STARTS AT 1, not 0 (the skeleton said 0). The game's
# exporter refuses a kingdom_tax_minimum below 1, for the reason trade_tax()
# gives at length: rounded up with a floor of 1, splitting a trade is strictly
# worse than not splitting it, and at 0 every small trade is free and a
# thousand dribbles launder what one honest trade would pay for. The rate may
# still go to 0 - a tax holiday is the owner's to call - and the floor of 1
# still stands under it.
RULES = {
    "trade_tax_rate": {
        "default": float(gamedata.CONSTANTS.get("kingdom_tax_rate", 0.05)), "min": 0.0, "max": 0.5,
        "about": "Share of a trade's value burned as the kingdom tax."},
    "trade_tax_minimum": {
        "default": float(gamedata.CONSTANTS.get("kingdom_tax_minimum", 1)), "min": 1.0, "max": 10000.0,
        "about": "The smallest kingdom tax a taxed trade pays, in gold."},
    "shop_price_multiplier": {
        "default": _shop_number("price_multiplier", 1.0), "min": 0.25, "max": 4.0,
        "about": "The general store's prices, times the item's value."},
    "shop_sell_multiplier": {
        "default": _shop_number("sell_multiplier", 0.05), "min": 0.0, "max": 0.5,
        "about": "What the general store pays for an item, times its value."},
    "material_drop_scale": {
        "default": 1.0, "min": 0.0, "max": 5.0,
        "about": "Times every monster's material and ingredient drop chance."},
    "temper_gold_scale": {
        "default": 1.0, "min": 0.0, "max": 10.0,
        "about": "Times the gold the smith charges to temper a piece."},
    "certification_gold_scale": {
        "default": 1.0, "min": 0.0, "max": 10.0,
        "about": "Times the gold a Royal Certification costs."},
}

# THE RULES THAT ARE WHOLE NUMBERS. A tax floor of 2.5 gold is a coin cut in
# half; the owner is told so rather than having it quietly rounded.
WHOLE = ("trade_tax_minimum",)

# WHAT EVERY PLAYER'S GAME IS TOLD (GET /api/economy/rules): the four numbers
# it draws - the shop's two multipliers and the trade tax's rate and floor.
# The scales the smith and the drops read are the owner's levers, and what
# they do reaches a player already priced (the smith's quote, a bag).
PUBLIC = ("shop_price_multiplier", "shop_sell_multiplier", "trade_tax_rate", "trade_tax_minimum")

# HOW LONG ONE PROCESS KEEPS WHAT IT READ OF THE OWNER'S TABLE.
#
# value() is asked on every price, every trade quote and - by the larder and
# the smith - every kill, and the table is the same for all of them; a query
# each time would be the kingdom board's mistake made smaller and more often.
# So it is read once a window per database, and a change CLEARS it (the
# owner's PUT, through clear_cache()).
#
# FIFTEEN SECONDS because the server is two gunicorn workers: the PUT clears
# the cache in the worker that answered it, and the other worker hears the
# change when its window runs out. Fifteen seconds is "in a minute", which is
# what the owner asked for, and is the kingdom board's window too.
CACHE_SECONDS = 15

# {database file: (read at, {key: value})}. Keyed by the file rather than one
# slot for the process, so two apps on two databases in one process (the
# suites' bare apps) never answer each other's numbers.
_cache = {}
_lock = threading.Lock()
# More files than this in one process is a test suite; the oldest goes.
_CACHE_FILES = 8


def default(key):
    """The number a rule has when nobody has set it. KeyError for a key that is
    not a rule - a typo is a bug to find, not a default to guess."""
    return float(RULES[key]["default"])


def value(db, key):
    """The number in force now for `key`. `db` may be None (defaults only).
    Never raises for a known key: a broken row reads as the default."""
    fallback = default(key)
    number = owner_value(db, key)
    return fallback if number is None else number


def owner_value(db, key):
    """The number the OWNER set for `key`, or None when nobody has - the case
    where a caller must keep its own number exactly as it was, rather than a
    default that is merely equal to it. A row out of bounds or not a number
    (written by hand, or by a build with other bounds) is no row: None.
    KeyError for a key that is not a rule."""
    default(key)
    if db is None:
        return None
    raw = _rows(db).get(key)
    if raw is None:
        return None
    number, refusal = check(key, raw)
    return None if refusal else number


def check(key, raw):
    """(number, "") when `raw` may be the rule `key`, else (None, why) in a
    sentence for the owner. Pure: no database. The bounds are RULES', the
    whole numbers WHOLE's."""
    rule = RULES.get(key)
    if rule is None:
        return None, "There is no economy rule called '%s'." % str(key)[:64]
    # A bool is an int to Python, and true is not a tax rate.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "%s must be a number." % key
    number = float(raw)
    if not math.isfinite(number):
        return None, "%s must be a number." % key
    low, high = float(rule["min"]), float(rule["max"])
    if number < low or number > high:
        return None, "%s must be from %s to %s (you sent %s)." % (key, shown(low), shown(high), shown(number))
    if key in WHOLE and number != math.floor(number):
        return None, "%s must be a whole number of gold." % key
    return number, ""


def shown(number):
    """A number as the owner typed it: 0.05, 1, 2.5 - not 1.0 or 0.050000."""
    text = ("%.6f" % float(number)).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def shop_terms(db, shop_id):
    """The owner's numbers for `shop_id`'s prices, as the keyword arguments
    gamedata.shop_price(), shop_sell_price() and vendor_pays() take - only the
    ones the owner set, so an unset one is the catalogue's own, exactly. {} for
    any shop but the general store."""
    if str(shop_id) != SHOP_ID:
        return {}
    terms = {}
    price = owner_value(db, "shop_price_multiplier")
    sell = owner_value(db, "shop_sell_multiplier")
    if price is not None:
        terms["price_multiplier"] = price
    if sell is not None:
        terms["sell_multiplier"] = sell
    return terms


def tax_terms(db):
    """The owner's trade tax, as the keyword arguments gamedata.trade_tax()
    takes - only what the owner set."""
    terms = {}
    rate = owner_value(db, "trade_tax_rate")
    minimum = owner_value(db, "trade_tax_minimum")
    if rate is not None:
        terms["rate"] = rate
    if minimum is not None:
        terms["minimum"] = int(minimum)
    return terms


def public(db):
    """{key: number} - what every player's game is told."""
    return {key: value(db, key) for key in PUBLIC}


def clear_cache():
    """Forget every table read. Called by a change, so the worker that made it
    answers the new number at once; and by the suites, which write rows by
    hand."""
    with _lock:
        _cache.clear()


def _rows(db):
    """{key: value} as the owner's table holds them, through the cache. A
    database without the table (one from before 0.74.0 that has not booted
    this code, or a bare suite's) has no rows, and so every rule its default -
    which is what a server without this feature played by."""
    try:
        where = data.database_file(db)
    except sqlite3.Error:
        return {}
    now = time.monotonic()
    with _lock:
        held = _cache.get(where)
        if held is not None and now - held[0] < CACHE_SECONDS:
            return held[1]
    try:
        rows = data.numbers(db)
    except sqlite3.Error:
        rows = {}
    with _lock:
        if len(_cache) >= _CACHE_FILES and where not in _cache:
            _cache.pop(min(_cache, key=lambda name: _cache[name][0]))
        _cache[where] = (now, rows)
    return rows


def rule_rows(stored):
    """Every rule as the owner's panel draws it, from `stored` (data.rules()):
    the number in force, its default, its bounds, what it does, and whether the
    owner set it, when and who. A stored row out of bounds reads as not set,
    as value() reads it."""
    out = []
    for key in RULES:
        rule = RULES[key]
        row = stored.get(key)
        number, refusal = check(key, row["value"]) if row is not None else (None, "")
        is_set = row is not None and not refusal
        out.append({
            "key": key,
            "value": number if is_set else default(key),
            "default": default(key),
            "min": float(rule["min"]),
            "max": float(rule["max"]),
            "whole": key in WHOLE,
            "about": rule["about"],
            "set": is_set,
            "updated_at": int(row["updated_at"]) if row is not None else None,
            "updated_by": str(row["updated_by"]) if row is not None else "",
        })
    return out


def relational_refusal(key, number, current):
    """Why `number` may not be `key` given the other rules in force
    (`current`, {key: value}), or "". The one that matters: the shop must never
    pay what it charges - ShopData's own rule ("MUST STAY BELOW
    price_multiplier"), because a vendor that buys at its price is buy, sell,
    repeat, and the gold comes from nowhere. The sell cap in vendor_pays()
    still holds for what the shop stocks; this keeps the two multipliers
    themselves honest for everything else."""
    if key not in ("shop_price_multiplier", "shop_sell_multiplier"):
        return ""
    price = number if key == "shop_price_multiplier" else current.get("shop_price_multiplier")
    sell = number if key == "shop_sell_multiplier" else current.get("shop_sell_multiplier")
    if price is not None and sell is not None and sell >= price:
        return ("The shop must pay less than it charges: shop_sell_multiplier (%s) has to stay below "
                "shop_price_multiplier (%s)." % (shown(sell), shown(price)))
    return ""
