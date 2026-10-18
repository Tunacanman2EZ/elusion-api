"""The feature packages stand on their own (game 0.50.0).

Every feature package (ARCHITECTURE.md) - playerstats/, events/, insights/,
and killrecord/, friends/, gathering/, loot/, guilds/, parties/, chat/, trade/, kingdom/, world/, moderation/,
staffdesk/, equipment/, consuming/, death/, bank/, carry/, skills/ and saves/, taken out of app.py - is built the
BE105 way: each has a
register(app, ...) that is HANDED the app, its login checks and its database,
and never imports app.py. The source of each is checked for that elsewhere
(test_stats.py); this RUNS them without it - the "application factory"
lesson's "two app instances from the same code", where it pays off here:

  P-1  nothing of app.py comes along: each package's source never imports
       it, and this file never loads it
  P-2  all three registered on a bare Flask app - stand-in login checks and a
       throwaway database - answer as they do on the real server: the owner
       gets in, a player gets the same bare 404 as no route, nobody without a
       login gets a 401
  P-3  two bare apps from the same code, each with its own database: an event
       set on one is on that one alone, and only that one's announcer and
       staff log hear of it
  P-4  EVERY PACKAGE IS HERE: the list is the code map's (a folder app.py
       imports, with a routes.py), not one kept by hand - a package added
       without a bare-app run of its own fails this file

Run: python test_packages.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from flask import Flask, g, request  # noqa: E402

import codemap  # noqa: E402
import events  # noqa: E402
import friends  # noqa: E402
import gathering  # noqa: E402
import guilds  # noqa: E402
import parties  # noqa: E402
import bank as bank_package  # noqa: E402
import carry as carry_package  # noqa: E402
import chat  # noqa: E402
import consuming  # noqa: E402
import death  # noqa: E402
import equipment  # noqa: E402
import loot  # noqa: E402
import moderation  # noqa: E402
import insights  # noqa: E402
import killrecord  # noqa: E402
import kingdom  # noqa: E402
import playerstats  # noqa: E402
import saves as saves_package  # noqa: E402
import skills as skills_package  # noqa: E402
import staffdesk  # noqa: E402
import trade  # noqa: E402
import world  # noqa: E402

# Found, not listed: what app.py imports that has a routes.py (codemap.py).
PACKAGES = tuple(name for name, _rows, _layers in codemap.feature_packages())
# The ones this file registers on its bare apps, below.
RUN_HERE = ("playerstats", "events", "insights", "killrecord", "friends", "gathering", "loot", "guilds", "parties",
            "chat", "trade", "kingdom", "world", "moderation", "staffdesk", "equipment", "consuming",
            "death", "bank", "carry", "skills", "saves")
LANDS = {1: "dark", 2: "light", 3: "ice", 4: "wind", 5: "earth", 6: "fire", 7: "water"}

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


# =============================================================================
# A BARE APP: what app.py hands a package, stood in for.
# =============================================================================
USERS = {"boss": {"id": 1, "username": "boss", "role": "player"},
         "ann": {"id": 2, "username": "ann", "role": "player"},
         "bo": {"id": 3, "username": "bo", "role": "player"}}
OWNER = "boss"
RANKS = ("player", "mod", "dev", "coowner", "owner")


def require_auth(view):
    """Who you are, from a header - the stand-in for a bearer token."""
    def wrapped(*args, **kwargs):
        user = USERS.get(request.headers.get("X-Test-User", ""))
        if user is None:
            return {"error": "Unauthorized", "message": "Missing, invalid or expired token."}, 401
        g.user = user
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def require_owner(view):
    """The owner only - the same bare 404 as no route to anybody else."""
    def wrapped(*args, **kwargs):
        if g.user["username"] != OWNER:
            return {"error": "Not Found", "message": "The requested URL was not found on the server."}, 404
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def require_fresh_password(view):
    return view


def bare_app(name):
    """A Flask app with nothing on it, a database of its own, and all three
    packages registered. Hands back (app, what was announced, what was logged,
    the database's path)."""
    path = os.path.join(tempfile.gettempdir(), "elusion_packages_%s.db" % name)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT COLLATE NOCASE, role TEXT, created_at INTEGER, name_hue INTEGER,"
        " deaths INTEGER NOT NULL DEFAULT 0, is_banned INTEGER NOT NULL DEFAULT 0, ban_expires_at INTEGER,"
        " ban_reason TEXT NOT NULL DEFAULT '', banned_by TEXT NOT NULL DEFAULT '', banned_at INTEGER NOT NULL DEFAULT 0,"
        " chat_muted_until INTEGER NOT NULL DEFAULT 0, chat_mute_reason TEXT NOT NULL DEFAULT '',"
        " chat_muted_by TEXT NOT NULL DEFAULT '',"
        " chat_tokens REAL NOT NULL DEFAULT 8.0, last_chat_at INTEGER NOT NULL DEFAULT 0,"
        " last_image_at INTEGER NOT NULL DEFAULT 0, last_world_image_at INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, role TEXT,"
        " body TEXT, created_at INTEGER, channel TEXT, target_id INTEGER, image_id TEXT, guild TEXT, name_hue INTEGER);"
        "CREATE TABLE chat_deletions (message_id INTEGER PRIMARY KEY, channel TEXT, user_id INTEGER, target_id INTEGER,"
        " deleted_at INTEGER);"
        "CREATE TABLE chat_images (id TEXT PRIMARY KEY, kind TEXT, width INTEGER, height INTEGER, frames INTEGER,"
        " columns INTEGER, frame_ms INTEGER, bytes INTEGER, source TEXT, created_by TEXT, created_at INTEGER,"
        " format TEXT, data BLOB);"
        "CREATE TABLE chat_image_holders (image_id TEXT, user_id INTEGER, created_at INTEGER,"
        " PRIMARY KEY (image_id, user_id));"
        "CREATE TABLE ignores (user_id INTEGER, ignored_id INTEGER, created_at INTEGER);"
        "CREATE TABLE chat_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,"
        " reporter_id INTEGER NOT NULL, reporter_name TEXT NOT NULL, reported_id INTEGER NOT NULL,"
        " reported_name TEXT NOT NULL, channel TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',"
        " image_id TEXT NOT NULL DEFAULT '', said_at INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL,"
        " created_at INTEGER NOT NULL, resolved_at INTEGER NOT NULL DEFAULT 0, resolved_by TEXT NOT NULL DEFAULT '',"
        " outcome TEXT NOT NULL DEFAULT '', UNIQUE (message_id, reporter_id));"
        "CREATE TABLE saves (user_id INTEGER, slot INTEGER, class_id TEXT, level INTEGER, area TEXT, updated_at INTEGER,"
        " last_cast_at INTEGER NOT NULL DEFAULT 0, cast_tokens REAL NOT NULL DEFAULT 6.0,"
        " gold INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '', hp INTEGER NOT NULL DEFAULT 0,"
        " max_hp INTEGER NOT NULL DEFAULT 0, mana INTEGER NOT NULL DEFAULT 0, max_mana INTEGER NOT NULL DEFAULT 0,"
        " equipment TEXT NOT NULL DEFAULT '{}', stamina INTEGER NOT NULL DEFAULT 0,"
        " max_stamina INTEGER NOT NULL DEFAULT 0, pools_at INTEGER NOT NULL DEFAULT 0,"
        " last_train_at INTEGER NOT NULL DEFAULT 0, active_pet_id TEXT NOT NULL DEFAULT '',"
        " explored TEXT NOT NULL DEFAULT '{}', xp INTEGER NOT NULL DEFAULT 0,"
        " xp_to_next INTEGER NOT NULL DEFAULT 100, bank_gold INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (user_id, slot));"
        "CREATE TABLE character_deletions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, slot INTEGER,"
        " class_id TEXT, name TEXT, level INTEGER, gold INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER,"
        " snapshot TEXT);"
        "CREATE TABLE save_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, slot INTEGER);"
        "CREATE TABLE consume_grants (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, slot INTEGER, item_id TEXT,"
        " target TEXT NOT NULL DEFAULT '', amount INTEGER NOT NULL DEFAULT 0, at INTEGER);"
        "CREATE TABLE parties (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER);"
        "CREATE TABLE party_members (user_id INTEGER PRIMARY KEY, party_id INTEGER, is_leader INTEGER DEFAULT 0,"
        " joined_at INTEGER);"
        "CREATE TABLE party_invites (user_id INTEGER, from_id INTEGER, created_at INTEGER, PRIMARY KEY (user_id, from_id));"
        "CREATE TABLE sessions (user_id INTEGER, last_seen_at INTEGER, expires_at INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE guilds (id INTEGER PRIMARY KEY, name TEXT, folded TEXT UNIQUE, founded_by TEXT, created_at INTEGER);"
        "CREATE TABLE guild_members (user_id INTEGER PRIMARY KEY, guild_id INTEGER, rank TEXT, joined_at INTEGER);"
        "CREATE TABLE guild_invites (guild_id INTEGER, user_id INTEGER, invited_by TEXT, created_at INTEGER,"
        " UNIQUE (guild_id, user_id));"
        "CREATE TABLE guild_events (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, at INTEGER, kind TEXT,"
        " actor TEXT, target TEXT, detail TEXT);"
        "CREATE TABLE carry_items (user_id INTEGER, slot INTEGER, position INTEGER, item_id TEXT, quantity INTEGER,"
        " PRIMARY KEY (user_id, slot, position));"
        "CREATE TABLE skills (user_id INTEGER, slot INTEGER, skill_id TEXT, level INTEGER, xp INTEGER,"
        " PRIMARY KEY (user_id, slot, skill_id));"
        "CREATE TABLE loot_bags (bag_id TEXT PRIMARY KEY, user_id INTEGER, slot INTEGER, enemy_id TEXT, created_at INTEGER);"
        "CREATE TABLE loot_bag_items (bag_id TEXT, position INTEGER, item_id TEXT, quantity INTEGER);"
        "CREATE TABLE kill_tally (user_id INTEGER, slot INTEGER, enemy_id TEXT, kills INTEGER, first_at INTEGER,"
        " last_at INTEGER);"
        "CREATE TABLE friends (requester_id INTEGER, addressee_id INTEGER, state TEXT DEFAULT 'pending',"
        " created_at INTEGER, decided_at INTEGER);"
        "CREATE TABLE trades (trade_id TEXT PRIMARY KEY, a_user INTEGER, a_slot INTEGER, a_gold INTEGER NOT NULL DEFAULT 0,"
        " a_confirmed INTEGER NOT NULL DEFAULT 0, b_user INTEGER, b_slot INTEGER, b_gold INTEGER NOT NULL DEFAULT 0,"
        " b_confirmed INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open', created_at INTEGER, updated_at INTEGER,"
        " revision INTEGER NOT NULL DEFAULT 0, a_tax INTEGER NOT NULL DEFAULT 0, b_tax INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE trade_items (trade_id TEXT, side TEXT, item_id TEXT, quantity INTEGER,"
        " PRIMARY KEY (trade_id, side, item_id));"
        "CREATE TABLE trade_holds (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_id TEXT, created_at INTEGER,"
        " held_until INTEGER);"
        "CREATE TABLE bank_items (user_id INTEGER, position INTEGER, item_id TEXT, quantity INTEGER,"
        " PRIMARY KEY (user_id, position));"
        "CREATE TABLE gold_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, user_id INTEGER, slot INTEGER,"
        " delta INTEGER, reason TEXT, detail TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE lusion_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, user_id INTEGER, slot INTEGER,"
        " delta INTEGER, reason TEXT, detail TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE staff_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, actor_name TEXT,"
        " action TEXT, target_id INTEGER, target_name TEXT, detail TEXT, created_at INTEGER);"
        "CREATE TABLE login_attempts (username TEXT, ok INTEGER, reason TEXT, ip TEXT, at INTEGER);"
        "CREATE TABLE accounts (user_id INTEGER PRIMARY KEY, lusions INTEGER NOT NULL DEFAULT 0,"
        " bank_gold INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE world_ticks (tick INTEGER PRIMARY KEY, element INTEGER NOT NULL, started_at INTEGER NOT NULL,"
        " ends_at INTEGER NOT NULL, chosen_by TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE kill_reports (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, slot INTEGER, enemy_id TEXT,"
        " xp INTEGER NOT NULL DEFAULT 0, at INTEGER);")
    now = int(time.time())
    for user in USERS.values():
        conn.execute("INSERT INTO users (id, username, role, created_at) VALUES (?, ?, ?, ?)",
                     (user["id"], user["username"], user["role"], now - 3600))
    conn.execute("INSERT INTO saves (user_id, slot, class_id, level, area, updated_at) VALUES (2, 0, 'warrior', 4, 'field', ?)",
                 (now,))
    for user_id, slot, enemy, kills in ((2, 0, "slime", 5), (2, 1, "slime", 2), (3, 0, "slime", 6), (2, 0, "bat", 1)):
        conn.execute("INSERT INTO kill_tally VALUES (?, ?, ?, ?, ?, ?)", (user_id, slot, enemy, kills, now - 600, now))
    conn.commit()
    conn.close()

    app = Flask("bare_" + name)
    said = []
    logged = []

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(path)
            g.db.row_factory = sqlite3.Row
        return g.db

    app.config["world_db"] = get_db

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    playerstats.register(app, require_auth=require_auth, require_owner=require_owner, get_db=get_db,
                         left_out_names=lambda: (OWNER,))
    events.register(app, require_auth=require_auth, require_owner=require_owner,
                    require_fresh_password=require_fresh_password, get_db=get_db,
                    announce=lambda body, by="": said.append(body), log_action=logged.append,
                    clean_text=lambda raw, limit: str(raw).strip()[:limit], lands=LANDS,
                    left_out_names=lambda: (OWNER,))
    insights.register(app, require_auth=require_auth, require_owner=require_owner, get_db=get_db, lands=LANDS,
                      element_of=lambda _enemy: 0, left_out_names=lambda: (OWNER,),
                      next_stir=lambda _db: {"element": 3, "name": "ice", "at": 0, "reason": "Ice: handed in."})
    killrecord.register(app, require_auth=require_auth, get_db=get_db,
                        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None,
                        max_slot=3, since=lambda: 1234)

    def find_user(name):
        return next((u for u in USERS.values() if u["username"].casefold() == name.casefold()), None)

    friends.register(app, require_auth=require_auth, get_db=get_db, find_user=find_user,
                     bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                     # bo ignores ann.
                     ignores=lambda _db, a, b: (a, b) == (3, 2),
                     presence=lambda _db, ids: {2: int(time.time())} if 2 in ids else {},
                     person=lambda row, seen, now: {"username": row["username"], "online": 2 in seen and row["id"] == 2},
                     limits=lambda: (100, 1))

    # gathering/: the grants are app.py's, so here they are four-cell stand-ins
    # over this database - writing without committing, as app.py's do, so a
    # refusal after a take leaves nothing behind.
    def carried(user_id, slot):
        return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in get_db().execute(
            "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))}

    def take(user_id, slot, item_id, quantity):
        cells = [(p, q) for p, (i, q) in sorted(carried(user_id, slot).items()) if i == item_id]
        if sum(q for _p, q in cells) < quantity:
            return False
        for position, have in cells:
            gone = min(have, quantity)
            quantity -= gone
            get_db().execute("UPDATE carry_items SET quantity = quantity - ? WHERE user_id = ? AND slot = ? AND position = ?",
                             (gone, user_id, slot, position))
        get_db().execute("DELETE FROM carry_items WHERE quantity <= 0")
        return True

    def add(user_id, slot, item_id, quantity):
        free = [p for p in range(GATHER_CELLS) if p not in carried(user_id, slot)]
        if not free:
            return None
        get_db().execute("INSERT INTO carry_items VALUES (?, ?, ?, ?, ?)", (user_id, slot, free[0], item_id, quantity))
        return [free[0]]

    def skills_of(user_id, slot):
        return {r["skill_id"]: {"level": int(r["level"]), "xp": int(r["xp"])} for r in get_db().execute(
            "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ?", (user_id, slot))}

    def grant(user_id, slot, skill_id, gained):
        had = skills_of(user_id, slot).get(skill_id, {"level": 1, "xp": 0})
        xp = had["xp"] + int(gained)
        level = 1 + xp // 100
        get_db().execute("DELETE FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?", (user_id, slot, skill_id))
        get_db().execute("INSERT INTO skills VALUES (?, ?, ?, ?, ?)", (user_id, slot, skill_id, level, xp))
        return level, xp, level - had["level"]

    def roll_catch(rod_tier, level):
        rolled.append((rod_tier, level))
        return {"item_id": "rawmudfish", "quantity": 1, "xp": 150}

    def roll_cook(item_id, level):
        if item_id == "rawshark":
            return {"ok": False, "reason": "level", "needs": 40}
        if item_id != "rawmudfish":
            return {"ok": False, "reason": "recipe"}
        return {"ok": True, "burnt": False, "output": "cookedmudfish", "xp": 30}

    rolled = app.config.setdefault("rolled", [])
    gathering.register(app, require_auth=require_auth, get_db=get_db,
                       parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
                       bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                       slot_exists=lambda user_id, slot: get_db().execute(
                           "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
                       item_row={"bronzefishingrod": {"tier": 2}}.get, roll_catch=roll_catch, roll_cook=roll_cook,
                       skills_of=skills_of, take_from_backpack=take, add_to_backpack=add, grant_skill_xp=grant,
                       status_of=lambda user_id, slot: {"slot": slot},
                       inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                                           for p, (i, q) in sorted(carried(user_id, slot).items())],
                       bag_cells=GATHER_CELLS)

    # loot/: the money and the backpack are app.py's, so here the ledgers are
    # lists on the app (what was minted, and why), the lusions a total, and
    # the backpack the same four cells.
    minted = app.config.setdefault("minted", {"gold": [], "lusions": [], "holds": []})

    def gold_delta(_db, user_id, slot, delta, reason, _detail=""):
        minted["gold"].append((user_id, slot, delta, reason))

    def lusion_delta(_db, user_id, slot, delta, reason, _detail=""):
        minted["lusions"].append((user_id, slot, delta, reason))

    def parse_stat(raw):
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None

    loot.register(app, require_auth=require_auth, get_db=get_db,
                  bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                  parse_stat=parse_stat,
                  item_row={"smallhealthpotion": {"type_name": "CONSUMABLE"}, "silvercoin": {"type_name": "CURRENCY"},
                            "petmage": {"type_name": "PET"}}.get,
                  owns_item=lambda user_id, slot, item_id: any(i == item_id for i, _q in carried(user_id, slot).values()),
                  lusions_item_id="lusions", dupe_pet_lusions=lambda: 20,
                  gold_item_value=lambda item_id: {"silvercoin": 50}.get(item_id, 0),
                  gold_delta=gold_delta, lusion_delta=lusion_delta,
                  ensure_account=lambda user_id: {"lusions": sum(d for u, _s, d, _r in minted["lusions"] if u == user_id)},
                  add_to_backpack=add,
                  place_trade_hold=lambda _db, user_id, item_id, quantity: minted["holds"].append((user_id, item_id, quantity)),
                  status_of=lambda user_id, slot: {"slot": slot},
                  inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                                      for p, (i, q) in sorted(carried(user_id, slot).items())],
                  bag_cells=GATHER_CELLS)

    # guilds/: the purse and the bank are a column and a dict here, through
    # ledgers that list what was burned and why; the moderation log is a list
    # of its own; staff are the owner alone.
    bank = app.config.setdefault("bank", {})
    staff_log = app.config.setdefault("staff_log", [])

    class Short(Exception):
        pass

    def purse_delta(db, user_id, slot, delta, reason, _detail=""):
        have = db.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone()[0]
        if have + delta < 0:
            raise Short()
        db.execute("UPDATE saves SET gold = ? WHERE user_id = ? AND slot = ?", (have + delta, user_id, slot))
        minted["gold"].append((user_id, slot, delta, reason))
        return have + delta

    def bank_delta(_db, user_id, delta, reason, _detail=""):
        if bank.get(user_id, 0) + delta < 0:
            raise Short()
        bank[user_id] = bank.get(user_id, 0) + delta
        minted["gold"].append((user_id, "bank", delta, reason))
        return bank[user_id]

    def owner_only(_minimum):
        def decorator(view):
            def wrapped(*args, **kwargs):
                if g.user["username"] != OWNER:
                    return {"error": "Not Found", "message": "The requested URL was not found on the server."}, 404
                return view(*args, **kwargs)
            wrapped.__name__ = view.__name__
            return wrapped
        return decorator

    guilds.register(app, require_auth=require_auth, require_role=owner_only, get_db=get_db,
                    bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                    parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None,
                    find_user=find_user, ensure_account=lambda user_id: {"bank_gold": bank.get(user_id, 0)},
                    gold_delta=purse_delta, bank_gold_delta=bank_delta, insufficient_gold=Short,
                    log_staff_action=lambda user, action, target, detail="": staff_log.append(
                        "%s %s %s: %s" % (user["username"], action, target, detail)),
                    has_owner_powers=lambda name: name == OWNER,
                    role_for=lambda row: row["role"], name_hue_of=lambda row: row["name_hue"],
                    # The character an account plays: its lowest slot, for any
                    # account seen within the window - the two ? are app.py's.
                    playing_slot_sql="(SELECT MIN(slot) FROM saves s2 WHERE s2.user_id = {user} AND ? < ?)",
                    online_window=45)

    # parties/: the relay is a stand-in that counts how often it is asked; the
    # TURN key comes from app.config, so a check can take it away.
    relay = app.config.setdefault("relay", {"mode": "relay", "asked": 0})

    def turn_fetch(_key_id, _token, ttl):
        relay["asked"] += 1
        relay["ttl"] = ttl
        return [{"urls": ["stun:stun.example.com:3478", "stun:stun.example.com:53"]},
                {"urls": ["turn:turn.example.com:3478?transport=udp"], "username": "u", "credential": "c"}]

    import threading
    parties.register(app, require_auth=require_auth, get_db=get_db,
                     bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                     find_user=find_user,
                     # bo ignores ann, as for friends.
                     ignores=lambda _db, a, b: (a, b) == (3, 2),
                     playing_slot_sql="(SELECT MIN(slot) FROM saves s2 WHERE s2.user_id = {user} AND ? < ?)",
                     online_window=45,
                     voice_settings=lambda: (relay["mode"], "keyid", "token") if relay["mode"] else ("", "", ""),
                     turn_fetch=turn_fetch, voice_issued={}, voice_asks={}, voice_lock=threading.Lock())

    # chat/: staff are the owner alone, as for guilds; who is muted, the ignore
    # limit and the kept tail are app.config's, so a check can change them; a
    # linked picture is a stand-in fetch handing back a small PNG it makes.
    talk = app.config.setdefault("talk", {"muted": set(), "ignore_limit": 200, "keep": 3, "fetched": []})

    def no_write_lock(view):
        view._elusion_no_write_lock = True
        return view

    def fetch_image(url):
        talk["fetched"].append(url)
        if not chat.services.PILLOW_AVAILABLE:
            return None, "no Pillow here"
        import io
        buffer = io.BytesIO()
        chat.services.Image.new("RGBA", (4, 3), (200, 40, 40, 255)).save(buffer, format="PNG")
        return buffer.getvalue(), None

    app.config["chat"] = chat.register(
        app, require_auth=require_auth, require_role=owner_only, no_write_lock=no_write_lock, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400), find_user=find_user,
        clean_text=lambda raw, limit: " ".join(str(raw or "").split())[:limit],
        role_for=lambda row: row["role"], guild_tag=lambda name: name.upper(),
        name_hue_of=lambda row: row["name_hue"] if "name_hue" in row.keys() else None,
        role_at_least=lambda row, _rank: row["username"] == OWNER,
        can_act_on=lambda actor, _target: actor["username"] == OWNER,
        has_owner_powers=lambda name: name == OWNER,
        log_staff_action=lambda user, action, target, target_id=0, detail="": staff_log.append(
            "%s %s %s: %s" % (user["username"], action, target, detail)),
        mute_state=lambda row: ({"until": 1, "seconds_left": 60, "reason": "a test"}
                                if row["username"] in talk["muted"] else None),
        describe_mute=lambda state: "Muted: %s" % state["reason"],
        friend_ids=lambda _db, _user_id: set(),
        guild_membership=lambda user_id: guilds.membership(get_db(), user_id),
        party_membership=parties.membership,
        keep_per_channel=lambda: talk["keep"], ignore_limit=lambda: talk["ignore_limit"],
        fetch_image=fetch_image)

    # trade/: the catalogue is a stand-in with three things in it and a tenth
    # for the kingdom; the gold ledger writes the purse AND gold_ledger, as
    # app.py's does, so the board below reads what a trade burned. Everybody
    # is online but the owner; the switch is app.config's.
    app.config["trading"] = True
    took = app.config.setdefault("took", [])
    resynced = app.config.setdefault("resynced", [])

    def ledger_delta(db, user_id, slot, delta, reason, detail=""):
        db.execute("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (delta, user_id, slot))
        db.execute("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
                   (int(time.time()), user_id, slot, delta, reason, detail))

    def take_for_trade(user_id, slot, item_id, quantity, keys_last=False):
        took.append(keys_last)
        return take(user_id, slot, item_id, quantity)

    def moderation_target(payload):
        found = find_user(str(payload.get("username", "")))
        if found is None:
            return None, ({"error": "Not Found", "message": "No such account."}, 404)
        return found, None

    def query_int(name, default, low, high):
        raw = request.args.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if low <= value <= high else None

    def playing_slot(db, user_id):
        row = db.execute("SELECT MIN(slot) AS slot FROM saves WHERE user_id = ?", (user_id,)).fetchone()
        return None if row["slot"] is None else int(row["slot"])

    trade.register(app, require_auth=require_auth, require_role=owner_only, get_db=get_db,
                   bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                   parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
                   slot_exists=lambda user_id, slot: get_db().execute(
                       "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
                   # bo ignores ann, as for friends and parties.
                   ignores=lambda _db, a, b: (a, b) == (3, 2),
                   presence_for=lambda _db, ids: {i: int(time.time()) for i in ids if i != 1}, online_window=45,
                   playing_slot=playing_slot, trades_enabled=lambda: app.config["trading"],
                   take_from_backpack=take_for_trade, add_to_backpack=add, gold_delta=ledger_delta,
                   inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                                       for p, (i, q) in sorted(carried(user_id, slot).items())],
                   mark_resync=lambda _db, row: resynced.append(row["trade_id"]),
                   take_resync=lambda _db, _user_id: None,
                   name_hue_of=lambda row: row["name_hue"], moderation_target=moderation_target,
                   query_int=query_int, staff_page=(50, 200), catalogue=TradeCatalogue)

    # kingdom/: the board, over the same ledgers; register() hands back the
    # snapshot's cache_clear().
    app.config["kingdom"] = kingdom.register(app, require_auth=require_auth, get_db=get_db,
                                             guild_tag=lambda name: name.upper())

    # world/: the settings row is a dict on the app, the world's voice a list
    # of its own (the events' announcer is P-3's), the moderation log the one
    # above. Monster "e1".."e7" is of land 1..7. The events and the insights
    # are app.config's, None until a check hands one in - asked on every use.
    stored = app.config.setdefault("settings", {})
    world_said = app.config.setdefault("world_said", [])
    app.config["world_events"] = None
    app.config["world_insights"] = None
    app.config["world"] = world.register(
        app, require_auth=require_auth, require_owner=require_owner, require_fresh_password=require_fresh_password,
        get_db=get_db, bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        get_setting=stored.get, set_setting=lambda key, value, by="": stored.__setitem__(key, value),
        announce=lambda body, by="": world_said.append(body),
        log_staff_action=lambda user, action, target, target_id=0, detail="": staff_log.append(
            "%s %s %s: %s" % (user["username"], action, target, detail)),
        element_of=lambda enemy_id: int(enemy_id[1:]) if re.fullmatch(r"e[1-7]", enemy_id) else 0,
        events=lambda: app.config["world_events"], insights=lambda: app.config["world_insights"])

    # moderation/: the accounts are the database's rows; boss is staff, and how
    # far up is app.config's ("boss_rank"), so a check can make him a mod. Who
    # may read a line is chat/'s own answer, handed back above. The three
    # tunables are app.config's, asked on every use.
    app.config["boss_rank"] = "owner"
    limits = app.config.setdefault("report_limits", {"per_hour": 20, "per_card": 10, "mod_mute": 24 * 60})

    def account(name):
        return get_db().execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()

    def rank_of(row):
        return app.config["boss_rank"] if row["username"] == OWNER else row["role"]

    def outranks(actor, target):
        return RANKS.index(rank_of(actor)) > RANKS.index(rank_of(target))

    def within_reach(payload):
        name = str(payload.get("username", "")).strip()
        if not name:
            return None, ({"error": "Bad Request", "message": "username is required"}, 400)
        row = account(name)
        if row is None or not outranks(g.user, row):
            return None, ({"error": "Not Found", "message": "No such account."}, 404)
        return row, None

    moderation.register(
        app, require_auth=require_auth, require_role=owner_only, require_fresh_password=require_fresh_password,
        get_db=get_db, bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_stat=parse_stat, moderation_target=within_reach, find_user=account, can_act_on=outranks,
        role_for=rank_of, role_at_least=lambda row, rank: RANKS.index(rank_of(row)) >= RANKS.index(rank),
        roles=RANKS, settable_roles=("player", "mod", "dev"),
        log_staff_action=lambda user, action, target, target_id=0, detail="": staff_log.append(
            "%s %s %s: %s" % (user["username"], action, target, detail)),
        can_read_row=lambda db, user_id, row: app.config["chat"].can_read_row(db, user_id, row),
        wait_words=lambda seconds: "%d minutes" % (seconds // 60), default_channel="world",
        reports_per_hour=lambda: limits["per_hour"], report_lines_per_player=lambda: limits["per_card"],
        mute_max_minutes_mod=lambda: limits["mod_mute"])

    # staffdesk/: the moderation log is the database's staff_actions, written
    # by a stand-in for app.py's log_staff_action(); role_for() as SQL knows
    # the owner by name and the rest by their column.
    def write_log(user, action, target, target_id=None, detail=""):
        get_db().execute("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail,"
                         " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (user["id"], user["username"], action, target_id, target, detail, int(time.time())))

    def rank_sql(alias):
        return ("(CASE WHEN {a}.id IS NULL THEN NULL WHEN {a}.username = ? THEN 4 WHEN {a}.role = 'mod' THEN 1"
                " WHEN {a}.role = 'dev' THEN 2 ELSE 0 END)".format(a=alias), [OWNER])

    app.config["staffdesk"] = staffdesk.register(
        app, require_auth=require_auth, require_role=owner_only, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400), query_int=query_int,
        moderation_target=within_reach, can_act_on=outranks, role_for=rank_of, rank_sql=rank_sql, roles=RANKS,
        default_role="player",
        ban_state=lambda row: {"reason": row["ban_reason"]} if row["is_banned"] else None,
        mute_state=lambda row, now=None: ({"until": row["chat_muted_until"]}
                                          if row["chat_muted_until"] > (now or time.time()) else None),
        log_staff_action=write_log, online_window=45, playing_slot=playing_slot,
        linked_accounts=lambda _db, user_id: ([{"username": "bo", "strength": "strong"}], False),
        crowded_at=6, row_int=lambda row, column: int(row[column] or 0) if column in row.keys() else 0)

    # equipment/ and consuming/: the bag is the same four cells; the catalogue
    # is a stand-in that knows a robe, a sword, a crown that needs level 9, and
    # the potions; a character's maxima are 100 hp, and 20 more in the robe.
    def take_from(user_id, slot, item_id, quantity, prefer=None):
        app.config.setdefault("preferred", []).append(prefer)
        return take(user_id, slot, item_id, quantity)

    def maxima(row):
        worn = row["equipment"]
        return {"max_hp": 120 if "cobaltrobe" in worn else 100, "max_mana": 50, "max_stamina": 30}

    cell = lambda payload, capacity: (payload.get("position"), None)  # noqa: E731
    equipment.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        parse_cell=cell, carry_capacity=GATHER_CELLS, equip_slots=("chest", "head", "weapon"),
        catalogue=GearCatalogue, derived_stats=maxima, take_from_backpack=take_from, add_to_backpack=add,
        inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                            for p, (i, q) in sorted(carried(user_id, slot).items())])
    consuming.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        parse_cell=cell, carry_capacity=GATHER_CELLS, catalogue=GearCatalogue,
        skills_of=lambda user_id, slot: {"cooking": {"level": 2, "xp": 0}}, valid_skills={"cooking"},
        take_from_backpack=take_from,
        inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                            for p, (i, q) in sorted(carried(user_id, slot).items())])

    # death/: the account row, the bank and the lusions are the accounts
    # table, and every burn writes its ledger, as app.py's do; the carried gold
    # goes through the same ledger_delta() trade's tax does.
    def ensure_account(user_id):
        get_db().execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
        return get_db().execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()

    def bank_delta_ledger(db, user_id, delta, reason, detail=""):
        db.execute("UPDATE accounts SET bank_gold = bank_gold + ? WHERE user_id = ?", (delta, user_id))
        db.execute("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, NULL, ?, ?, ?)",
                   (int(time.time()), user_id, delta, reason, detail))

    def lusion_delta_ledger(db, user_id, slot, delta, reason, detail=""):
        db.execute("UPDATE accounts SET lusions = lusions + ? WHERE user_id = ?", (delta, user_id))
        db.execute("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
                   (int(time.time()), user_id, slot, delta, reason, detail))

    def status_of(user_id, slot):
        row = get_db().execute("SELECT hp, max_hp, gold FROM saves WHERE user_id = ? AND slot = ?",
                               (user_id, slot)).fetchone()
        return {"hp": row["hp"], "max_hp": row["max_hp"], "gold": row["gold"]}

    death.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        ensure_account=ensure_account, catalogue=DeathCatalogue, derived_stats=maxima,
        gold_delta=ledger_delta, bank_gold_delta=bank_delta_ledger, lusion_delta=lusion_delta_ledger,
        lusion_gold_value=1000, revive_grant_id="__revive__", respawn_grant_id="__respawn__",
        status_of=status_of)

    # carry/: four bag cells and two keys. A cell is named the way app.py's
    # parse_grid_cell() does; the owners are app.config's, so a check can make
    # ann one; a trade's resync is app.config's too, handed out once and gone.
    # The pile's worth is ten gold a coin pile, one lusion a lusion; the money
    # goes through the same ledgers death's does.
    def grid_cell(payload, key, capacity):
        raw = payload.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw < capacity:
            return raw, None
        return None, ({"error": "Bad Request", "message": "%s must be an integer 0-%d" % (key, capacity - 1)}, 400)

    def item_named(payload):
        item_id = str(payload.get("item_id", "")).strip()
        if not item_id:
            return None, ({"error": "Bad Request", "message": "item_id must be 1-64 characters"}, 400)
        return item_id, None

    owners = app.config.setdefault("owners", {OWNER})
    trade_resyncs = app.config.setdefault("trade_resyncs", {})
    app.config["carry"] = carry_package.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        slot_exists=lambda user_id, slot: get_db().execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
        parse_grid_cell=grid_cell, parse_item_id=item_named, has_owner_powers=lambda name: name in owners,
        catalogue=CarryCatalogue, bag_cells=GATHER_CELLS, carry_cells=GATHER_CELLS + 2, quantity_ceiling=99,
        take_resync=lambda _db, user_id, slot: trade_resyncs.pop((user_id, slot), None),
        money_item_worth=lambda item_id: {"goldpile": ("gold", 10), "lusions": ("lusions", 1)}.get(item_id, (None, 0)),
        lusions_item_id=lambda: "lusions", ensure_account=ensure_account, gold_delta=ledger_delta,
        lusion_delta=lusion_delta_ledger, account_of=lambda user_id: dict(ensure_account(user_id)))
    carry = app.config["carry"]

    # skills/: a level every ten XP, five at most; cooking is the one skill
    # the client may still write here, so the write's clamp has something to
    # hold; training is two agility, five defense and one magic a second, ten
    # seconds a report at most, and a magic specialty that doubles it.
    app.config["skills_api"] = skills_package.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        parse_stat=parse_stat,
        slot_exists=lambda user_id, slot: get_db().execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
        has_owner_powers=lambda name: name in owners, catalogue=SkillCatalogue,
        valid_skills={"attack", "magic", "agility", "defense", "fishing", "cooking"},
        server_owned={"attack", "magic", "agility", "defense", "fishing"}, trainable=("agility", "defense", "magic"),
        max_level=5, max_xp=1000, max_train_seconds=10.0,
        max_train_xp_per_sec={"agility": 2.0, "defense": 5.0, "magic": 1.0},
        proficient_amount=lambda class_id, skill_id, raw: int(raw * (2 if skill_id == "magic" else 1)))

    # saves/: names are one line of at most the limit; the first level costs
    # 1250; the save history is a list on the app that keeps nothing; the
    # carry, the skills and whether a pet is held are the packages' above,
    # the open trade trade/'s own lookup, and an account keeps two deleted
    # characters.
    snapshots = app.config.setdefault("snapshots", [])
    saves_package.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        clean_text=lambda raw, limit: " ".join(str(raw or "").split())[:limit],
        owns_item=carry.owns_item, stored_json=lambda raw, fallback: json.loads(raw) if raw else fallback,
        derived_stats=lambda row, class_id=None, level=None: maxima(row), catalogue=SavesCatalogue,
        take_snapshot=lambda _db, user_id, slot, reason: snapshots.append((user_id, slot, reason)),
        status_of=lambda user_id, slot: status_of(user_id, slot) if get_db().execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() else None,
        inventory_of=carry.inventory_of, skills_of=app.config["skills_api"].skills_of,
        find_open_trade=trade.data.find_open, gold_delta=ledger_delta, deletions_kept=2,
        delete_reason="character-deleted")

    # bank/: a four-cell bank, handed the grid steps the carry hands back -
    # as app.py hands them - and the bank's own catalogue.
    app.config["bank_api"] = bank_package.register(
        app, require_auth=require_auth, get_db=get_db,
        bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
        parse_stat=parse_stat, parse_cell=cell, parse_grid_cell=grid_cell,
        parse_item_id=lambda payload: (str(payload.get("item_id", "")), None),
        parse_positional_items=carry.parse_positional_items,
        slot_exists=lambda user_id, slot: get_db().execute(
            "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
        ensure_account=ensure_account, has_owner_powers=lambda name: name == OWNER, catalogue=BankCatalogue,
        capacity=4, carry_capacity=GATHER_CELLS, backpack_capacity=GATHER_CELLS, quantity_ceiling=99,
        take_from_cells=carry.take_from_cells, apply_cell_changes=carry.apply_cell_changes,
        grid_move=carry.grid_move, grid_discard=carry.grid_discard, add_to_backpack=add,
        inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                            for p, (i, q) in sorted(carried(user_id, slot).items())])
    return app, said, logged, path


# A four-cell bag for gathering/'s stand-ins.
GATHER_CELLS = 4


class BankCatalogue:
    """What bank/ asks of gamedata: how high an item stacks. Potions in fives,
    anything else one to a cell."""
    @staticmethod
    def item_row(item_id):
        return {"stackable": True, "max_stack": 5} if item_id.endswith("potion") else {}


class CarryCatalogue:
    """What carry/ asks of gamedata: how high an item stacks - potions in
    fives, as the bank's catalogue, piles in ninety-nines, a pebble and gear
    one to a cell, anything else unknown - and no lusion pile id of its own,
    so the one handed in is used."""
    CONSTANTS = {}

    @staticmethod
    def item_row(item_id):
        if item_id.endswith("potion"):
            return {"stackable": True, "max_stack": 5}
        if item_id in ("goldpile", "lusions"):
            return {"stackable": True, "max_stack": 99}
        return {"stackable": False} if item_id in ("pebble", "cobaltrobe", "ironsword", "crownhelm") else None


class SavesCatalogue:
    """What saves/ asks of gamedata: what the first level costs."""
    @staticmethod
    def xp_needed_for_level(_level):
        return 1250


class SkillCatalogue:
    """What skills/ asks of gamedata: the curve, ten XP a level at every level,
    and the level-up loop - the shape of gamedata.apply_xp()."""
    @staticmethod
    def xp_needed_for_skill_level(_skill_id, _level):
        return 10

    @staticmethod
    def apply_xp(level, xp, _xp_to_next, gained, need=None):
        xp += gained
        levels = 0
        while xp >= need(level):
            xp -= need(level)
            level += 1
            levels += 1
        return level, xp, need(level), levels


class DeathCatalogue:
    """What death/ asks of gamedata: the gold a revive costs - eight tenths of
    what you hold, at least 100 - and the lusions."""
    CONSTANTS = {"revive_cost": 20}

    @staticmethod
    def revive_gold_cost(held):
        return 0 if held <= 0 else max(100, held * 8 // 10)


class GearCatalogue:
    """What equipment/ and consuming/ ask of gamedata: where a piece goes,
    whether this character may wear it, whether they may use a consumable,
    and what it restores."""
    SLOTS = {"cobaltrobe": "chest", "ironsword": "weapon", "crownhelm": "head"}
    NEEDS = {"crownhelm": 9}
    KNOWN = set(SLOTS) | {"pebble", "smallhealthpotion", "elixir", "chefsoup"}

    @staticmethod
    def equip_slot_for(item_id):
        return GearCatalogue.SLOTS.get(item_id, "")

    @staticmethod
    def has_item(item_id):
        return item_id in GearCatalogue.KNOWN

    @staticmethod
    def equip_check(item_id, target, class_id, level):
        if level < GearCatalogue.NEEDS.get(item_id, 0):
            return {"ok": False, "reason": "level", "needs": GearCatalogue.NEEDS[item_id]}
        return {"ok": True}

    @staticmethod
    def consume_check(item_id, level, levels, known_skills=None):
        if item_id == "elixir":
            return {"ok": False, "reason": "level", "needs": 30}
        if item_id == "chefsoup":
            return {"ok": levels.get("cooking", 1) >= 5, "reason": "skill", "skill": "cooking", "needs": 5}
        return {"ok": item_id == "smallhealthpotion", "reason": "nottype"}

    @staticmethod
    def restore_for(item_id):
        return "hp", 25


class TradeCatalogue:
    """What trade/ asks of gamedata, for three things: a pebble, a raw shark,
    and an old crown of the mythic tier. The kingdom's cut is a tenth."""
    ITEMS = {"pebble": {"tier": 0, "value": 2, "display_name": "Pebble"},
             "rawshark": {"tier": 1, "value": 30, "display_name": "Raw Shark"},
             "crown": {"tier": 9, "value": 1000, "display_name": "Old Crown"}}

    @staticmethod
    def item_row(item_id):
        return TradeCatalogue.ITEMS.get(item_id)

    @staticmethod
    def has_item(item_id):
        return item_id in TradeCatalogue.ITEMS

    @staticmethod
    def mythic_tier():
        return 9

    @staticmethod
    def stack_value(item_id, quantity):
        row = TradeCatalogue.ITEMS.get(item_id)
        return None if row is None else row["value"] * int(quantity)

    @staticmethod
    def trade_tax(value):
        return int(value) // 10


def snapshots_of(app):
    return list(app.config["snapshots"])


def who(name):
    return {"X-Test-User": name}


# =============================================================================
section("P-1  nothing of app.py comes along")
for name in PACKAGES:
    source = ""
    for root, _dirs, files in os.walk(os.path.join(HERE, name)):
        for file in files:
            if file.endswith(".py"):
                source += open(os.path.join(root, file), encoding="utf-8").read()
    check("%s/ never imports app.py" % name,
          source != "" and not re.search(r"^\s*(import app\b|from app\b|import elusion_app|from elusion_app)", source, re.M))
app_a, said_a, logged_a, path_a = bare_app("a")
app_b, said_b, logged_b, path_b = bare_app("b")
check("and this file never loaded it: no app.py module in the process",
      not any(m in sys.modules for m in ("app", "elusion_app", "wsgi")), [m for m in ("app", "elusion_app", "wsgi") if m in sys.modules])
rules = sorted(r.rule for r in app_a.url_map.iter_rules() if r.rule.startswith("/api/"))
check("a bare app has the packages' routes and nothing else",
      rules == sorted(["/api/server/stats", "/api/world/events/active", "/api/world/events/schedule", "/api/world/events",
                       "/api/world/events/cancel", "/api/world/events/preview", "/api/world/events/report",
                       "/api/server/insights", "/api/kills", "/api/kills/everyone", "/api/friends",
                       "/api/friends/request", "/api/friends/respond", "/api/friends/remove",
                       "/api/fishing/catch", "/api/cooking/cook", "/api/loot/take", "/api/loot/bag",
                       "/api/guild", "/api/guild/create", "/api/guild/invite", "/api/guild/respond", "/api/guild/leave",
                       "/api/guild/kick", "/api/guild/rank", "/api/guild/disband", "/api/staff/guild",
                       "/api/guild/list", "/api/party", "/api/party/invite", "/api/party/respond",
                       "/api/party/leave", "/api/party/kick", "/api/party/lead", "/api/party/voice",
                       "/api/chat/send", "/api/chat", "/api/chat/image", "/api/chat/upload", "/api/chat/image/upload",
                       "/api/chat/image/<image_id>", "/api/chat/delete", "/api/ignores", "/api/ignores",
                       "/api/ignores/remove", "/api/trade/offer", "/api/trade", "/api/trade/update",
                       "/api/trade/confirm", "/api/trade/cancel", "/api/staff/trades", "/api/trade/history",
                       "/api/economy/kingdom", "/api/server/world", "/api/server/world",
                       "/api/server/world/stir", "/api/staff/ban", "/api/staff/kick", "/api/staff/unban",
                       "/api/staff/role", "/api/chat/report", "/api/report/player", "/api/staff/reports",
                       "/api/staff/reports/resolve", "/api/staff/mute", "/api/staff/unmute",
                       "/api/staff/users", "/api/staff/actions", "/api/staff/note",
                       "/api/staff/user/<username>", "/api/character/equip", "/api/character/unequip",
                       "/api/character/consume", "/api/character/revive",
                       "/api/character/respawn", "/api/account", "/api/account/bank", "/api/account/lusions",
                       "/api/bank/items", "/api/bank/gold", "/api/bank/move", "/api/bank/discard",
                       "/api/character/inventory", "/api/character/inventory/move",
                       "/api/character/inventory/discard", "/api/character/inventory/cash",
                       "/api/character/skills", "/api/skill/train", "/api/save", "/api/save",
                       "/api/character", "/api/character/delete"]), rules)

# =============================================================================
section("P-2  on a bare app, each answers as on the real server")
client_a = app_a.test_client()
res = client_a.get("/api/server/stats", headers=who("boss"))
body = res.get_json() or {}
check("playerstats: the owner gets the report - two players, the owner left out",
      res.status_code == 200 and body["players"]["total"] == 2 and body["players"]["staff_left_out"] == 1, body.get("players"))
res = client_a.get("/api/server/insights", headers=who("boss"))
body = res.get_json() or {}
check("insights: every land handed in, and the next stir from the callable handed in",
      res.status_code == 200 and [r["element"] for r in body["lands"]] == sorted(LANDS)
      and body["next"] == {"element": 3, "name": "ice", "at": 0, "reason": "Ice: handed in."}, body.get("next"))
res = client_a.get("/api/world/events/active", headers=who("ann"))
check("events: any player reads what is on", res.status_code == 200 and res.get_json()["events"] == [], res.get_json())
hidden = [client_a.get(path, headers=who("ann")).status_code
          for path in ("/api/server/stats", "/api/server/insights", "/api/world/events/schedule", "/api/world/events/report")]
check("a player gets the same bare 404 from every owner's route", hidden == [404, 404, 404, 404], hidden)
res = client_a.get("/api/kills?slot=0", headers=who("ann"))
body = res.get_json() or {}
check("killrecord: a character's record - its own slot's kills, most first, and the day counting began",
      res.status_code == 200 and [(k["enemy_id"], k["kills"]) for k in body["kills"]] == [("slime", 5), ("bat", 1)]
      and body["total"] == 6 and body["kinds"] == 2 and body["since"] == 1234 and body["slot"] == 0, body)
res = client_a.get("/api/kills/everyone", headers=who("ann"))
body = res.get_json() or {}
slime = next((k for k in body.get("kills", []) if k["enemy_id"] == "slime"), {})
check("  everyone's - an account's characters added together (ann's 7 beats bo's 6), and ann's own share beside it",
      res.status_code == 200 and slime.get("kills") == 13 and slime.get("top") == "ann" and slime.get("top_kills") == 7
      and slime.get("yours") == 7 and body["players"] == 2, body)
check("  and a bad slot is the 400 it was, in its words",
      client_a.get("/api/kills?slot=9", headers=who("ann")).get_json() == {"error": "Bad Request", "message": "slot must be 0-3"})
res = client_a.post("/api/friends/request", headers=who("boss"), json={"username": "ANN"})
check("friends: boss asks ann - by her name in any case - and it waits",
      res.status_code == 200 and res.get_json() == {"username": "ann", "state": "pending"}, res.get_json())
check("  a second ask is past the one-outstanding limit handed in",
      client_a.post("/api/friends/request", headers=who("boss"), json={"username": "bo"}).get_json()
      == {"error": "Bad Request", "message": "you have 1 requests waiting for an answer; that is the limit"})
res = client_a.post("/api/friends/respond", headers=who("ann"), json={"username": "boss", "accept": True})
listed = client_a.get("/api/friends", headers=who("boss")).get_json() or {}
check("  ann says yes: on boss's list, online by the presence handed in, the limits his",
      res.get_json() == {"username": "boss", "state": "accepted"}
      and [(f["username"], f["online"]) for f in listed.get("friends", [])] == [("ann", True)]
      and listed.get("limits") == {"friends": 100, "pending_sent": 1, "friends_used": 1, "pending_sent_used": 0}, listed)
check("  bo ignores ann, so she cannot ask him - the ignore list handed in",
      client_a.post("/api/friends/request", headers=who("ann"), json={"username": "bo"}).status_code == 403)
check("  and removed, the link is gone both ways",
      client_a.post("/api/friends/remove", headers=who("ann"), json={"username": "boss"}).get_json()
      == {"username": "boss", "state": "none"}
      and client_a.get("/api/friends", headers=who("boss")).get_json()["friends"] == [])


def ann_holds(item_id):
    conn = sqlite3.connect(path_a)
    held = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM carry_items WHERE user_id = 2 AND slot = 0 AND item_id = ?",
                        (item_id,)).fetchone()[0]
    conn.close()
    return int(held)


def ann_gets(item_id, quantity, position):
    conn = sqlite3.connect(path_a)
    conn.execute("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)", (position, item_id, quantity))
    conn.commit()
    conn.close()


def fish(slot=0):
    return client_a.post("/api/fishing/catch", headers=who("ann"), json={"slot": slot})


def cook(item_id):
    return client_a.post("/api/cooking/cook", headers=who("ann"), json={"slot": 0, "item_id": item_id})


check("gathering: no rod is the 403 it was", fish().get_json() == {"error": "Forbidden", "message": "You need a fishing rod."})
ann_gets("bronzefishingrod", 1, 0)
check("  a rod and no worms, the other 403", fish().get_json() == {"error": "Forbidden", "message": "You need worms for bait."})
ann_gets("fishingworm", 3, 1)
res = fish()
body = res.get_json() or {}
check("  a rod and worms: the fish lands in a free cell, one worm is spent, the skill handed in pays",
      res.status_code == 200 and body.get("item_id") == "rawmudfish" and body.get("carry_positions") == [2]
      and body.get("fishing_level") == 2 and body.get("levelled_up") is True and ann_holds("fishingworm") == 2
      and body.get("skills", {}).get("fishing") == {"level": 2, "xp": 150}, body)
check("  rolled with the rod's tier from the catalogue handed in, and the fishing level before the cast",
      app_a.config["rolled"] == [(2, 1)], app_a.config["rolled"])
conn = sqlite3.connect(path_a)
tokens, cast_at = conn.execute("SELECT cast_tokens, last_cast_at FROM saves WHERE user_id = 2 AND slot = 0").fetchone()
conn.execute("UPDATE saves SET cast_tokens = 0.2, last_cast_at = ? WHERE user_id = 2 AND slot = 0", (int(time.time() * 1000),))
conn.commit()
conn.close()
check("  the cast bucket paid one token (%g of %g), and the cast's time" % (tokens, gathering.CAST_BUCKET_CAPACITY),
      abs(tokens - (gathering.CAST_BUCKET_CAPACITY - 1.0)) < 1e-6 and cast_at > 0, (tokens, cast_at))
check("  an empty bucket is the 429 it was, in its words",
      fish().get_json() == {"error": "Too Many Requests", "message": "Casts are arriving faster than 0.4 per second."})
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET cast_tokens = 6.0 WHERE user_id = 2 AND slot = 0")
conn.commit()
conn.close()
ann_gets("pebble", 1, 3)
res = fish()
check("  a full bag is the 409 it was - and the worm is NOT spent: nothing was committed",
      res.get_json() == {"error": "Conflict", "message": "Your backpack is full (4 slots)."} and ann_holds("fishingworm") == 2,
      (res.get_json(), ann_holds("fishingworm")))
check("  an empty slot and a bad one, the 404 and 400 they were",
      fish(slot=1).get_json() == {"error": "Not Found", "message": "No character in slot 1."}
      and fish(slot=9).get_json() == {"error": "Bad Request", "message": "slot must be an integer 0-3"})
res = cook("rawmudfish")
body = res.get_json() or {}
check("cooking: the raw fish goes, the dish comes, and cooking XP is paid",
      res.status_code == 200 and body.get("burnt") is False and body.get("output") == "cookedmudfish"
      and ann_holds("rawmudfish") == 0 and ann_holds("cookedmudfish") == 1 and body.get("cooking_level") == 1
      and body.get("skills", {}).get("cooking") == {"level": 1, "xp": 30}, body)
conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE item_id = 'pebble'")
conn.execute("INSERT INTO carry_items VALUES (2, 0, 3, 'rawshark', 1)")
conn.commit()
conn.close()
check("  a level too low, the 403 it was, and the fish kept",
      cook("rawshark").get_json() == {"error": "Forbidden", "message": "You need cooking level 40."} and ann_holds("rawshark") == 1)
check("  what is not carried, the 404; what has no recipe, the 409",
      cook("rawtuna").get_json() == {"error": "Not Found", "message": "You are not carrying that."}
      and cook("bronzefishingrod").get_json() == {"error": "Conflict", "message": "That cannot be cooked."})


BAGS_MADE = []


def bag_for_bo(entries, made_at=None):
    """A bag rolled for bo (account 3, slot 0) the way a kill makes one -
    stamped() and store_bag() - and its id."""
    conn = sqlite3.connect(path_a)
    now = int(time.time())
    BAGS_MADE.append(1)
    bag_id = "bag%d" % len(BAGS_MADE)
    stored = loot.stamped([{"item_id": i, "quantity": q} for i, q in entries])
    loot.store_bag(conn, bag_id, 3, 0, "slime", stored, now if made_at is None else made_at,
                   prune_before=now - loot.LOOT_BAG_TTL_SECONDS - 3600)
    conn.commit()
    conn.close()
    return bag_id, stored


def take(bag_id, position, as_who="bo"):
    return client_a.post("/api/loot/take", headers=who(as_who), json={"bag_id": bag_id, "position": position})


def bo_gets(item_id, position):
    conn = sqlite3.connect(path_a)
    conn.execute("INSERT INTO carry_items VALUES (3, 0, ?, ?, 1)", (position, item_id))
    conn.commit()
    conn.close()


minted = app_a.config["minted"]
bo_gets("petmage", 0)
bag, stored = bag_for_bo([("smallhealthpotion", 2), ("silvercoin", 3), ("petmage", 1), ("smallhealthpotion", 1)]
                         + [("pebble", 1)] * 7)
check("loot: a roll past %d cells is stored cut to them, each entry with its cell" % loot.LOOT_BAG_CAPACITY,
      [e["position"] for e in stored] == list(range(loot.LOOT_BAG_CAPACITY)), stored)
res = take(bag, 0)
body = res.get_json() or {}
check("  a potion goes into the backpack handed in, and is held from trade there",
      res.status_code == 200 and body.get("credited") == "inventory" and body.get("carry_positions") == [1]
      and body.get("granted_quantity") == 2 and minted["holds"] == [(3, "smallhealthpotion", 2)], body)
check("  the same cell again is the 404 it was - a second take changes nothing",
      take(bag, 0).get_json() == {"error": "Not Found", "message": "Nothing in that slot of the bag."})
body = take(bag, 1).get_json() or {}
check("  three silver coins are 150 gold, through the gold ledger handed in, as loot",
      body.get("credited") == "gold" and body.get("gold_amount") == 150 and minted["gold"] == [(3, 0, 150, "loot")], body)
body = take(bag, 2).get_json() or {}
check("  a pet bo already holds is 20 lusions instead, through the lusion ledger",
      body.get("duplicate_pet") is True and body.get("credited") == "lusions" and body.get("granted_item_id") == "lusions"
      and body.get("granted_quantity") == 20 and body.get("lusions") == 20
      and minted["lusions"] == [(3, 0, 20, "duplicate_pet")], body)
check("  a cell past the bag's is the 400 it was, in its words",
      take(bag, loot.LOOT_BAG_CAPACITY).get_json()
      == {"error": "Bad Request", "message": "position must be an integer 0-%d" % (loot.LOOT_BAG_CAPACITY - 1)})
check("  and somebody else's bag is the same 404 as no bag at all",
      take(bag, 3, as_who="ann").get_json() == {"error": "Not Found", "message": "No such loot bag."}
      and take("nosuchbag", 3).get_json() == {"error": "Not Found", "message": "No such loot bag."})
res = client_a.get("/api/loot/bag?bag_id=%s" % bag, headers=who("bo"))
body = res.get_json() or {}
check("  what is left: cells 3 to 8, the monster, the slot",
      res.status_code == 200 and [c["position"] for c in body.get("contents", [])] == list(range(3, loot.LOOT_BAG_CAPACITY))
      and body.get("enemy_id") == "slime" and body.get("slot") == 0, body)
check("  and asked with no bag, the 400 it was",
      client_a.get("/api/loot/bag", headers=who("bo")).get_json() == {"error": "Bad Request", "message": "bag_id is required"})
bo_gets("pebble", 2)
bo_gets("pebble", 3)
res = take(bag, 3)
check("  a full backpack is the 409 it was, and the potion stays in the bag",
      res.get_json() == {"error": "Conflict", "message": "Your backpack is full (4 slots)."}
      and 3 in [c["position"] for c in client_a.get("/api/loot/bag?bag_id=%s" % bag, headers=who("bo")).get_json()["contents"]])
old_bag, _stored = bag_for_bo([("silvercoin", 1)], made_at=int(time.time()) - loot.LOOT_BAG_TTL_SECONDS - 5)
check("  a bag past its %d seconds is the 410 it was, and gone after" % loot.LOOT_BAG_TTL_SECONDS,
      take(old_bag, 0).get_json() == {"error": "Gone", "message": "That loot bag has expired."}
      and client_a.get("/api/loot/bag?bag_id=%s" % old_bag, headers=who("bo")).status_code == 404)
last_bag, _stored = bag_for_bo([("silvercoin", 2)])
body = take(last_bag, 0).get_json() or {}
check("  the last thing out empties the bag, and the bag goes with it",
      body.get("bag_empty") is True and body.get("gold_amount") == 100
      and client_a.get("/api/loot/bag?bag_id=%s" % last_bag, headers=who("bo")).status_code == 404, body)


def guild_post(as_who, path, body):
    res = client_a.post(path, headers=who(as_who), json=body)
    return res.status_code, res.get_json() or {}


def guild_of(as_who):
    return client_a.get("/api/guild", headers=who(as_who)).get_json() or {}


conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET gold = 3000, name = 'Ann' WHERE user_id = 2 AND slot = 0")
conn.execute("INSERT INTO sessions (user_id, last_seen_at) VALUES (2, ?)", (int(time.time()),))
conn.commit()
conn.close()
app_a.config["bank"][2] = 4000
gold_before = len(minted["gold"])
code, body = guild_post("ann", "/api/guild/create", {"name": "The Crowned", "slot": 0})
check("guilds: ann founds one - %d gold, the carried 3000 first, then 2000 from the bank, both burned"
      % guilds.GUILD_FOUND_COST,
      code == 200 and body.get("rank") == "leader" and body.get("from_carried") == 3000 and body.get("from_bank") == 2000
      and body.get("carried_gold") == 0 and body.get("bank_gold") == 2000
      and minted["gold"][gold_before:] == [(2, 0, -3000, "guild_found"), (2, "bank", -2000, "guild_found")], body)
crowned = body.get("guild", {})
check("  the guild as every route returns it: tagged, on the roster as she plays, online by the window handed in",
      crowned.get("tag") == "THE CROWNED" and crowned.get("capacity") == 50
      and [(m["username"], m["rank"], m["online"], m["character"]) for m in crowned.get("members", [])]
      == [("ann", "leader", True, "Ann")], crowned)
check("  a second founding is the 409 it was, a bad name the 400",
      guild_post("ann", "/api/guild/create", {"name": "Another", "slot": 0})[0] == 409
      and guild_post("bo", "/api/guild/create", {"name": "x", "slot": 0})[1].get("message", "").startswith(
          "a guild name is 3 to 12 characters"))
check("  and an empty slot pays nothing: the 404 it was",
      guild_post("bo", "/api/guild/create", {"name": "Bo's Lot", "slot": 0})
      == (404, {"error": "Not Found", "message": "That slot is empty."}))
guild_post("ann", "/api/guild/invite", {"username": "bo"})
check("  invited, bo sees it waiting",
      [i["guild"] for i in guild_of("bo").get("invites", [])] == ["The Crowned"], guild_of("bo"))
code, body = guild_post("bo", "/api/guild/respond", {"guild": "the crowned", "accept": True})
check("  accepted - by the name in any case - he is a member",
      code == 200 and body.get("state") == "joined" and body.get("guild", {}).get("size") == 2, body)
code, body = guild_post("ann", "/api/guild/rank", {"username": "bo", "rank": "officer"})
check("  the leader makes him an officer", code == 200 and body.get("rank") == "officer", body)
check("  an officer cannot remove the leader: the same 404 as no such member",
      guild_post("bo", "/api/guild/kick", {"username": "ann"}) == (404, {"error": "Not Found", "message": "Not found."}))
check("  and the leader cannot walk out on him",
      guild_post("ann", "/api/guild/leave", {})[1].get("message")
      == "make somebody else the leader first, or disband the guild")
check("  its history, newest first",
      [e["kind"] for e in guild_of("ann").get("guild", {}).get("activity", [])]
      == ["promoted", "joined", "invited", "founded"], guild_of("ann"))
code, body = guild_post("boss", "/api/staff/guild", {"guild": "The Crowned", "action": "rename", "name": "The First",
                                                     "reason": "a test"})
check("staff: renamed, on the moderation log handed in, and in the guild's history without who did it",
      code == 200 and body == {"guild": "The First", "was": "The Crowned", "state": "renamed"}
      and app_a.config["staff_log"] == ["boss guild_rename The Crowned: -> The First: a test"]
      and guild_of("bo").get("guild", {}).get("activity", [{}])[0] == {
          "at": guild_of("bo")["guild"]["activity"][0]["at"], "kind": "renamed", "actor": "", "target": "The First",
          "detail": "The Crowned"}, (body, app_a.config["staff_log"]))
check("  and the old name no longer finds it",
      guild_post("boss", "/api/staff/guild", {"guild": "The Crowned", "action": "disband", "reason": "x"})[0] == 404)
listed = client_a.get("/api/guild/list", headers=who("boss")).get_json() or {}
check("  the staff list: one guild, its size and leader, and the owner may take it down",
      [(r["name"], r["size"], r["leader"]) for r in listed.get("guilds", [])] == [("The First", 2, "ann")]
      and listed.get("may_disband") is True, listed)
check("  and nobody else is shown the list",
      client_a.get("/api/guild/list", headers=who("ann")).status_code == 404)
code, body = guild_post("ann", "/api/guild/disband", {})
check("the leader takes it down: both out, and the guild gone",
      code == 200 and body.get("members_removed") == 2 and guild_of("bo").get("in_guild") is False
      and client_a.get("/api/guild/list", headers=who("boss")).get_json()["guilds"] == [], body)


def party_post(as_who, path, body):
    res = client_a.post(path, headers=who(as_who), json=body)
    return res.status_code, res.get_json() or {}


def party_of(as_who):
    return client_a.get("/api/party", headers=who(as_who)).get_json() or {}


check("parties: bo ignores ann, so her invitation is the 403 it was - the ignore list handed in",
      party_post("ann", "/api/party/invite", {"username": "bo"})
      == (403, {"error": "Forbidden", "message": "bo is not taking party invites from you."}))
code, body = party_post("boss", "/api/party/invite", {"username": "ann"})
check("  boss asks ann, and she sees it waiting",
      code == 200 and body.get("expires_in") == parties.services.PARTY_INVITE_SECONDS
      and [i["from"] for i in party_of("ann").get("invites", [])] == ["boss"], body)
code, body = party_post("ann", "/api/party/respond", {"from": "boss", "accept": True})
members = (body.get("party") or {}).get("members", [])
check("  she says yes: the party is made with boss leading, and her on it as she plays, online",
      code == 200 and body.get("state") == "joined"
      and [(m["username"], m["leader"], m["online"], m["character"]) for m in members]
      == [("boss", True, False, ""), ("ann", False, True, "Ann")], body)
check("  only the leader invites",
      party_post("ann", "/api/party/invite", {"username": "bo"})
      == (400, {"error": "Bad Request", "message": "only the party's leader can invite"}))
code, body = party_post("boss", "/api/party/lead", {"username": "ann"})
check("  boss hands it on to ann", code == 200 and body.get("party", {}).get("leader") == "ann", body)
check("  and no longer leading, he cannot remove anybody",
      party_post("boss", "/api/party/kick", {"username": "ann"})
      == (404, {"error": "Not Found", "message": "You are not leading a party."}))
code, body = party_post("ann", "/api/party/kick", {"username": "boss"})
check("  ann removes him: down to one, the party is gone",
      code == 200 and body.get("disbanded") is True and body.get("party") is None
      and party_of("boss").get("party") is None, body)
check("  and an answer to nobody's invitation is the 404 it was",
      party_post("ann", "/api/party/respond", {"from": "bo", "accept": True})
      == (404, {"error": "Not Found", "message": "No invitation from them - it may have run out."}))
check("party voice: in no party, the 409 it was",
      client_a.get("/api/party/voice", headers=who("ann")).get_json()
      == {"error": "Conflict", "message": "Voice is for parties: invite somebody first."})
party_post("boss", "/api/party/invite", {"username": "ann"})
party_post("ann", "/api/party/respond", {"from": "boss", "accept": True})
relay = app_a.config["relay"]
body = client_a.get("/api/party/voice", headers=who("ann")).get_json() or {}
urls = [u for server in body.get("ice_servers", []) for u in server["urls"]]
check("  in one: the relay handed in is asked once, for a day, and port 53 is dropped from its answer",
      body.get("policy") == "relay" and relay["asked"] == 1 and relay["ttl"] == parties.VOICE_TURN_TTL
      and "stun:stun.example.com:53" not in urls and "turn:turn.example.com:3478?transport=udp" in urls
      and body.get("expires_at", 0) - time.time() > 23 * 3600, (body, relay))
again = client_a.get("/api/party/voice", headers=who("ann")).get_json() or {}
check("  asked again, the same credentials, and the relay is not asked twice",
      again.get("ice_servers") == body.get("ice_servers") and relay["asked"] == 1, relay)
relay["mode"] = ""
check("  and with no TURN key in the settings handed in, the 503 it was",
      client_a.get("/api/party/voice", headers=who("boss")).get_json()
      == {"error": "Service Unavailable", "message": "Voice is not set up on this server yet."})


def say(as_who, body, **more):
    res = client_a.post("/api/chat/send", headers=who(as_who), json=dict(body=body, **more))
    return res.status_code, res.get_json() or {}


def heard(as_who, channel="world", since=0, **more):
    query = "&".join(["channel=%s" % channel, "since=%d" % since] + ["%s=%s" % kv for kv in more.items()])
    return client_a.get("/api/chat?" + query, headers=who(as_who)).get_json() or {}


talk = app_a.config["talk"]
code, body = say("ann", "hello  world")
check("chat: a line said in world, cleaned by the text rule handed in",
      code == 200 and body.get("body") == "hello world" and body.get("channel") == "world", body)
lines = heard("bo").get("messages", [])
check("  and read back by another, in the shape every chat line has",
      [(m["by"], m["body"], m["channel"]) for m in lines] == [("ann", "hello world", "world")], lines)
talk["muted"].add("bo")
check("  a muted account is the 403 it was, in the words handed in",
      say("bo", "let me speak")[1].get("message") == "Muted: a test")
talk["muted"].clear()
conn = sqlite3.connect(path_a)
conn.execute("UPDATE users SET chat_tokens = 0, last_chat_at = ? WHERE id = 3", (int(time.time() * 1000),))
conn.commit()
conn.close()
check("  an empty flood bucket is the 429 it was",
      say("bo", "too soon")[0] == 429)
conn = sqlite3.connect(path_a)
conn.execute("UPDATE users SET chat_tokens = 8 WHERE id = 3")
conn.commit()
conn.close()
for n in range(4):
    say("ann", "line %d" % n)
check("  the tail kept per channel is the tunable handed in, asked on every line (%d here)" % talk["keep"],
      [m["body"] for m in heard("bo").get("messages", [])] == ["line 1", "line 2", "line 3"],
      heard("bo").get("messages"))
code, body = client_a.post("/api/ignores", headers=who("bo"), json={"username": "ann"}).status_code, None
check("ignores: bo stops hearing ann - in world, and her whispers to him are refused",
      code == 200 and heard("bo").get("messages") == []
      and say("ann", "psst", channel="private", to="bo")
      == (403, {"error": "Forbidden", "message": "bo is not taking whispers from you."}))
check("  and the ignore list says so",
      [i["username"] for i in client_a.get("/api/ignores", headers=who("bo")).get_json().get("ignored", [])] == ["ann"])
check("  staff cannot be ignored",
      client_a.post("/api/ignores", headers=who("ann"), json={"username": "boss"}).status_code == 403)
talk["ignore_limit"] = 0
check("  and the limit is the tunable handed in, asked each time",
      client_a.post("/api/ignores", headers=who("ann"), json={"username": "bo"}).status_code == 409)
talk["ignore_limit"] = 200
check("  heard again once taken back",
      client_a.post("/api/ignores/remove", headers=who("bo"), json={"username": "ann"}).get_json().get("was_ignored") is True
      and len(heard("bo").get("messages", [])) == 3)
feed = heard("bo")
doomed = feed["messages"][-1]["id"]
res = client_a.post("/api/chat/delete", headers=who("boss"), json={"id": doomed})
later = heard("bo", since=int(feed["latest_id"]))
check("  staff take a line down: on the moderation log, and a reader past it is told",
      res.status_code == 200 and doomed in later.get("removed", [])
      and app_a.config["staff_log"][-1].startswith("boss chat_delete ann"), (res.get_json(), later))
check("  and nobody else may", client_a.post("/api/chat/delete", headers=who("ann"), json={"id": 1}).status_code == 404)
if chat.services.PILLOW_AVAILABLE:
    res = client_a.post("/api/chat/image", headers=who("ann"), json={"url": "http://pictures.example/red.png"})
    picture = res.get_json() or {}
    check("pictures: a link is fetched by the call handed in, decoded and kept",
          res.status_code == 200 and picture.get("kind") == "still" and picture.get("width") == 4
          and talk["fetched"] == ["http://pictures.example/red.png"], picture)
    image_path = "/api/chat/image/%s" % picture.get("id", "")
    check("  its maker may load it, and somebody shown no line with it may not",
          client_a.get(image_path, headers=who("ann")).status_code == 200
          and client_a.get(image_path, headers=who("bo")).status_code == 404)
    check("  a second within the cooldown is the 429 it was",
          client_a.post("/api/chat/image", headers=who("ann"), json={"url": "http://pictures.example/again.png"})
          .status_code == 429)
    say("ann", "", image=picture.get("id", ""))
    check("  once it is in a world line, everybody may load it",
          client_a.get(image_path, headers=who("bo")).status_code == 200)
else:
    print("  skip  pictures: this machine has no Pillow")
def trade_post(as_who, path, body=None):
    res = client_a.post(path, headers=who(as_who), json=body or {})
    return res.status_code, res.get_json() or {}


def purse_of(user_id):
    conn = sqlite3.connect(path_a)
    gold = conn.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (user_id,)).fetchone()[0]
    conn.close()
    return int(gold)


def bag_of(user_id):
    conn = sqlite3.connect(path_a)
    rows = conn.execute("SELECT item_id, SUM(quantity) FROM carry_items WHERE user_id = ? AND slot = 0"
                        " GROUP BY item_id ORDER BY item_id", (user_id,)).fetchall()
    conn.close()
    return dict(rows)


# Two fresh bags and two purses: ann carries a raw shark and an old crown she
# found a moment ago, bo three pebbles and 500 gold.
conn = sqlite3.connect(path_a)
conn.row_factory = sqlite3.Row
conn.execute("DELETE FROM carry_items WHERE user_id IN (2, 3)")
conn.execute("UPDATE saves SET gold = 100 WHERE user_id = 2 AND slot = 0")
conn.execute("INSERT INTO saves (user_id, slot, class_id, level, area, updated_at, gold, name)"
             " VALUES (3, 0, 'mage', 3, 'field', ?, 500, 'Bo')", (int(time.time()),))
conn.executemany("INSERT INTO carry_items VALUES (?, 0, ?, ?, ?)",
                 [(2, 0, "rawshark", 1), (2, 1, "crown", 1), (3, 0, "pebble", 3)])
trade.place_hold(conn, TradeCatalogue, 2, "crown")
trade.place_hold(conn, TradeCatalogue, 2, "pebble")
conn.commit()
holds = [tuple(r) for r in conn.execute("SELECT user_id, item_id FROM trade_holds")]
conn.close()
check("trade: a fresh find of the mythic tier is held, and an ordinary one is not",
      holds == [(2, "crown")], holds)
app_a.config["trading"] = False
check("  with the owner's switch handed in off, a new trade is the 503 it was",
      trade_post("bo", "/api/trade/offer", {"slot": 0, "username": "ann"})[0] == 503)
app_a.config["trading"] = True
check("  bo ignores ann, so her trade is the 403 it was - the ignore list handed in",
      trade_post("ann", "/api/trade/offer", {"slot": 0, "username": "bo"})
      == (403, {"error": "Forbidden", "message": "bo is not taking trades from you."}))
check("  and nobody trades with somebody the presence handed in has not seen",
      trade_post("bo", "/api/trade/offer", {"slot": 0, "username": "boss"})
      == (400, {"error": "Bad Request", "message": "boss is not online."}))
code, body = trade_post("bo", "/api/trade/offer", {"slot": 0, "username": "ANN"})
check("  bo asks ann - by her name in any case - with the character she plays, both shown online",
      code == 200 and body.get("state") == "open" and body.get("revision") == 0
      and (body["a"]["username"], body["b"]["username"], body["b"]["name"], body["b"]["slot"]) == ("bo", "ann", "Ann", 0)
      and body["a"]["online"] is True and body["b"]["online"] is True, body)
check("  a second while one is open is the 409 it was",
      trade_post("bo", "/api/trade/offer", {"slot": 0, "username": "ann"})
      == (409, {"error": "Conflict", "message": "Already in a trade."}))
check("  what bo does not carry, and gold he does not have, the 400s they were",
      trade_post("bo", "/api/trade/update", {"items": [{"item_id": "crown", "quantity": 1}]})[1].get("message")
      == "You only have 0 x crown."
      and trade_post("bo", "/api/trade/update", {"gold": 600})[1].get("message") == "You only have 500 gold.")
code, body = trade_post("bo", "/api/trade/update", {"items": [{"item_id": "pebble", "quantity": 1}], "gold": 200})
check("  bo puts up a pebble and 200 gold: valued by the catalogue handed in, ann's cut quoted from it",
      code == 200 and body.get("revision") == 1 and body["a"]["offering_value"] == 202
      and body["b"]["tax"] == 20 and body["a"]["tax"] == 0, body)
code, body = trade_post("ann", "/api/trade/update", {"items": [{"item_id": "crown", "quantity": 1}]})
check("  the crown ann found a moment ago waits its %d hours, and says so" % (trade.TRADE_HOLD_SECONDS // 3600),
      code == 400 and body.get("message", "").startswith(
          "Old Crown was found less than 48 hours ago and can be traded in 48 hours."), body)
code, body = trade_post("ann", "/api/trade/update", {"items": [{"item_id": "rawshark", "quantity": 1}]})
check("  she puts up the shark instead, and the revision moves", code == 200 and body.get("revision") == 2, body)
code, body = trade_post("ann", "/api/trade/confirm", {"revision": 1})
check("  an accept of an offer she was not shown is the 409 it was, with the offer as it stands",
      code == 409 and body.get("message") == "The offer changed. Look at it again before you accept."
      and body.get("trade", {}).get("revision") == 2, body)
code, body = trade_post("bo", "/api/trade/confirm", {"revision": 2})
check("  bo accepts what he saw", code == 200 and body["a"]["confirmed"] is True and body["b"]["confirmed"] is False, body)
code, body = trade_post("ann", "/api/trade/confirm", {"revision": 2})
check("  and ann's accept runs it: both crossed, both taxed on what they received",
      code == 200 and body.get("state") == "done" and body.get("tax_paid") == {"a": 3, "b": 20}
      and body.get("kingdom_take") == 23 and body["a"]["gold"] == 297 and body["b"]["gold"] == 280
      and bag_of(2) == {"crown": 1, "pebble": 1} and bag_of(3) == {"pebble": 2, "rawshark": 1}
      and (purse_of(2), purse_of(3)) == (280, 297), (body, bag_of(2), bag_of(3)))
conn = sqlite3.connect(path_a)
burned = conn.execute("SELECT user_id, delta, reason FROM gold_ledger ORDER BY id").fetchall()
conn.close()
check("  the cut burned through the gold ledger handed in - and the transfer wrote no row",
      burned == [(3, -3, "kingdom_tax"), (2, -20, "kingdom_tax")], burned)
check("  taken bag before keys, every time, and both bags flagged to be read again",
      app_a.config["took"] == [True, True] and app_a.config["resynced"] == [body.get("trade_id")],
      (app_a.config["took"], app_a.config["resynced"]))
history = client_a.get("/api/trade/history", headers=who("ann")).get_json() or {}
line = (history.get("trades") or [{}])[0]
check("  ann's history says it from her side: who with, on which character, what went each way, her cut",
      len(history.get("trades", [])) == 1 and line.get("with") == "bo" and line.get("with_name") == "Bo"
      and line.get("character") == "Ann" and line.get("gave") == [{"item_id": "rawshark", "quantity": 1}]
      and line.get("got") == [{"item_id": "pebble", "quantity": 1}] and line.get("gold_got") == 200
      and line.get("tax") == 20, history)
current = client_a.get("/api/trade", headers=who("ann")).get_json() or {}
check("  and the window, asked after, says how the last one ended",
      current.get("trade") is None and current.get("last") == {"trade_id": body.get("trade_id"), "state": "done"}, current)
trade_post("bo", "/api/trade/offer", {"slot": 0, "username": "ann"})
code, body = trade_post("ann", "/api/trade/cancel")
check("  either side calls one off; a second time there is nothing to",
      code == 200 and body.get("cancelled") is True and trade_post("ann", "/api/trade/cancel")[1] == {"cancelled": False})
staff = client_a.get("/api/staff/trades?username=bo", headers=who("boss")).get_json() or {}
check("staff: an account's trades, worded from its side, and the tally - through the reach handed in",
      [t["state"] for t in staff.get("trades", [])] == ["cancelled", "done"]
      and staff.get("summary") == {"open": 0, "done": 1, "cancelled": 1} and staff.get("more") is False
      and staff["trades"][1].get("gave") == [{"item_id": "pebble", "quantity": 1}], staff)
check("  a page and its cursor",
      (client_a.get("/api/staff/trades?username=bo&limit=1", headers=who("boss")).get_json() or {}).get("more") is True)
check("  nobody else is shown them, and nobody is no account",
      client_a.get("/api/staff/trades?username=bo", headers=who("ann")).status_code == 404
      and client_a.get("/api/staff/trades?username=nobody", headers=who("boss")).status_code == 404)

# The board reads the ledgers: the two cuts above, and boss, who has only ever
# paid in lusions. bo is in a guild of his own.
conn = sqlite3.connect(path_a)
conn.execute("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason) VALUES (?, 1, NULL, -5, 'revive')",
             (int(time.time()),))
conn.execute("UPDATE users SET deaths = 2 WHERE id = 1")
conn.execute("UPDATE users SET deaths = 1 WHERE id = 2")
conn.execute("INSERT INTO guilds (id, name, folded, founded_by, created_at) VALUES (50, 'Fishers', 'fishers', 'bo', 0)")
conn.execute("INSERT INTO guild_members VALUES (3, 50, 'leader', 0)")
conn.commit()
conn.close()
board = client_a.get("/api/economy/kingdom", headers=who("ann")).get_json() or {}
check("kingdom: what the realm took - the cuts, by reason, and the lusions in a column of their own",
      (board.get("total"), board.get("total_taxed"), board.get("total_lusions"), board.get("total_deaths"))
      == (23, 23, 5, 3) and board.get("by_reason") == {"kingdom_tax": 23}
      and board.get("lusions_by_reason") == {"revive": 5}, board)
check("  ranked by gold, lusions breaking the tie - and somebody who only paid in lusions is on it",
      [(e["username"], e["contributed"], e["lusions"], e["rank"]) for e in board.get("top", [])]
      == [("ann", 20, 0, 1), ("bo", 3, 0, 2), ("boss", 0, 5, 3)] and board.get("contributors") == 3, board.get("top"))
check("  the guilds' board, tagged by the guild_tag handed in, and ann's own line",
      board.get("guilds") == [{"name": "Fishers", "tag": "FISHERS", "contributed": 3, "lusions": 0, "deaths": 0,
                               "members": 1, "rank": 1}]
      and board.get("your_guild") is None and board.get("you", {}).get("rank") == 1
      and board.get("you", {}).get("deaths") == 1, board)
check("  bo's line, and his guild's, are his",
      ((client_a.get("/api/economy/kingdom", headers=who("bo")).get_json() or {}).get("your_guild") or {}).get("name")
      == "Fishers")
conn = sqlite3.connect(path_a)
conn.execute("INSERT INTO gold_ledger (at, user_id, slot, delta, reason) VALUES (?, 2, 0, -100, 'shop')",
             (int(time.time()),))
conn.commit()
conn.close()
again = client_a.get("/api/economy/kingdom", headers=who("ann")).get_json() or {}
app_a.config["kingdom"].cache_clear()
fresh = client_a.get("/api/economy/kingdom", headers=who("ann")).get_json() or {}
check("  the shared half is kept for %ds; the cache_clear() handed back drops it"
      % kingdom.KINGDOM_BOARD_CACHE_SECONDS,
      again.get("total") == 23 and fresh.get("total") == 123
      and fresh.get("by_reason") == {"shop": 100, "kingdom_tax": 23}, (again.get("total"), fresh.get("total")))
def world_view():
    res = client_a.get("/api/server/world", headers=who("boss"))
    return res.status_code, res.get_json() or {}


def world_post(path, body):
    res = client_a.post(path, headers=who("boss"), json=body)
    return res.status_code, res.get_json() or {}


def stir_for(enemy_id):
    with app_a.app_context():
        return app_a.config["world"].stir_for(app_a.config["world_db"](), enemy_id)


WIND_TEXT = ("The wind lands stir for 3 hours: wind monsters hit 25% harder, have 50% more health, move 15% faster"
             " and drop loot 1.5x as often, and wind resistance counts 1.5x.")
# A day's hunting: one paid kill in every land but wind.
conn = sqlite3.connect(path_a)
conn.executemany("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (2, 0, ?, ?)",
                 [("e%d" % land, int(time.time()) - 60) for land in (1, 2, 3, 5, 6, 7)])
conn.commit()
conn.close()
world_said = app_a.config["world_said"]
code, view = world_view()
check("world: the owner's view - the settings as shipped, with no row; the least hunted land stirs, and says so",
      code == 200 and view.get("settings") == world.WORLD_DEFAULTS
      and (view["state"]["tick"], view["state"]["element"], view["state"]["name"]) == (1, 4, "wind")
      and view["state"]["ends_at"] - view["state"]["started_at"] == 3 * 3600 and world_said == [WIND_TEXT], (view, world_said))
check("  each land's hunting from the kills handed in, the last stirs, the limits, and the next and why",
      [land["kills"] for land in view.get("lands", [])] == [1, 1, 1, 0, 1, 1, 1]
      and [r["tick"] for r in view.get("recent", [])] == [1] and view.get("limits", {}).get("health_percent") == [0, 200]
      and view.get("next", {}).get("element") in (1, 2, 3, 5, 6, 7)
      and view.get("next", {}).get("reason", "").endswith(": the least hunted in the last day."), view)
check("  asked again inside the stir, the same stir - and nobody else sees it",
      world_view()[1].get("state", {}).get("tick") == 1 and len(world_said) == 1
      and client_a.get("/api/server/world", headers=who("ann")).status_code == 404)
check("  a kill of the stirring land's monster drops 1.5x as often; any other is ordinary",
      stir_for("e4") == (1.5, 1.0) and stir_for("e1") == (1.0, 1.0), (stir_for("e4"), stir_for("e1")))
check("  a setting that is not one, or out of its range, is the 400 it was",
      world_post("/api/server/world", {"hours": 0})
      == (400, {"error": "Bad Request", "message": "hours is a whole number from 1 to 24"})
      and world_post("/api/server/world", {"color": "red"})[1].get("message")
      == "color is not a setting of the stirring lands")
code, body = world_post("/api/server/world", {"on": False, "xp_on": True})
check("  switched off: stored, still, on the moderation log handed in - and a kill is ordinary",
      code == 200 and body.get("state") == {"on": False, "tick": 0, "element": 0}
      and world_said[-1] == "The lands grow still." and app_a.config["staff_log"][-1] == "boss world server: on=False, xp_on=True"
      and stir_for("e4") == (1.0, 1.0), (body.get("state"), world_said, app_a.config["staff_log"][-1:]))
code, body = world_post("/api/server/world", {"on": True})
check("  on again, the stir still running is said again, with the XP now on",
      code == 200 and body["state"]["tick"] == 1 and world_said[-1].startswith("The wind lands stir for 3 hours:")
      and "give 25% more XP" in world_said[-1] and stir_for("e4") == (1.5, 1.25), world_said[-1:])
code, body = world_post("/api/server/world/stir", {"element": 6})
check("  the owner stirs fire by hand: the wind's stir ends now, a whole fire stir begins, announced and logged",
      code == 200 and (body["state"]["tick"], body["state"]["name"]) == (2, "fire")
      and body["recent"][0]["chosen_by"] == "boss" and body["recent"][1]["ends_at"] <= int(time.time())
      and world_said[-1].startswith("The fire lands stir for 3 hours:")
      and app_a.config["staff_log"][-1] == "boss world server: stirred the fire lands", body.get("recent"))
check("  not one of the seven lands is the 400 it was",
      world_post("/api/server/world/stir", {"element": 9})[0] == 400)
app_a.config["world_insights"] = types.SimpleNamespace(
    choose=lambda _db, candidates, _hunts, _chooser, _now: (7, [], {}),
    reason=lambda land, *_rest: "Water: the insights' choice.")
check("  the insights handed in, asked when they are wanted, choose the next land and say why",
      world_view()[1].get("next", {}).get("reason") == "Water: the insights' choice."
      and world_view()[1]["next"]["element"] == 7, world_view()[1].get("next"))
app_a.config["world_events"] = types.SimpleNamespace(
    forced_stir=lambda _db, now=None: {"id": 7, "land": 2, "ends_at": int(time.time()) + 600, "name": "Light Week"},
    kill_scales=lambda _db, element, stirring: (2.0, 1.0), ceiling=lambda bags, xp: (min(bags, 3.0), min(xp, 3.0)))
code, view = world_view()
check("  a stir event handed in wins: its land stirs until it ends, and a kill is paid its scale, held to the ceiling",
      (view["state"]["tick"], view["state"]["name"]) == (3, "light") and view["recent"][0]["chosen_by"] == "event #7"
      and view["next"]["reason"] == "Light: Light Week is on then."
      and stir_for("e2") == (3.0, 1.25) and stir_for("e1") == (2.0, 1.0), (view.get("state"), stir_for("e2")))
app_a.config["world_events"] = None
app_a.config["world_insights"] = None
def staff_post(path, body, as_who="boss", method="post"):
    res = getattr(client_a, method)(path, headers=who(as_who), json=body)
    return res.status_code, res.get_json() or {}


def column(user_id, name):
    conn = sqlite3.connect(path_a)
    value = conn.execute("SELECT %s FROM users WHERE id = ?" % name, (user_id,)).fetchone()[0]
    conn.close()
    return value


def sessions_of(user_id, live=0):
    conn = sqlite3.connect(path_a)
    if live:
        conn.executemany("INSERT INTO sessions (user_id, last_seen_at, expires_at) VALUES (?, ?, ?)",
                         [(user_id, int(time.time()), int(time.time()) + 3600)] * live)
        conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]
    conn.close()
    return int(n)


limits = app_a.config["report_limits"]
staff_log = app_a.config["staff_log"]
ann_lines = [m["id"] for m in heard("bo").get("messages", []) if m["by"] == "ann" and m["body"].startswith("line")]
code, body = staff_post("/api/chat/report", {"id": ann_lines[0], "reason": "spam"}, as_who="bo")
check("moderation: bo reports a line of ann's he was shown - chat's own answer, handed in",
      len(ann_lines) >= 2 and (code, body) == (200, {"id": ann_lines[0], "reported": True}), (ann_lines, code, body))
check("  the same line again is not a second report, and a reason that is not one is the 400 it was",
      staff_post("/api/chat/report", {"id": ann_lines[0], "reason": "spam"}, as_who="bo")[1].get("already") is True
      and staff_post("/api/chat/report", {"id": ann_lines[1], "reason": "rude"}, as_who="bo")[0] == 400)
check("  and a line nobody has is the same 404 as one you were not shown",
      staff_post("/api/chat/report", {"id": 99999, "reason": "spam"}, as_who="bo")
      == (404, {"error": "Not Found", "message": "No such message."}))
code, body = staff_post("/api/report/player", {"username": "ANN", "reason": "harassment", "area": "dark_forest"},
                        as_who="bo")
check("  bo reports ann herself, by her name in any case, from where he saw her",
      (code, body) == (200, {"username": "ann", "reported": True}), body)
limits["per_hour"] = 2
check("  the hourly ceiling is the tunable handed in, asked each time",
      staff_post("/api/chat/report", {"id": ann_lines[1], "reason": "spam"}, as_who="bo")[0] == 429)
limits["per_hour"] = 20
res = client_a.get("/api/staff/reports", headers=who("boss"))
tab = res.get_json() or {}
card = (tab.get("players") or [{}])[0]
check("staff: the Reports tab - one card for ann, her line and the report about her, and the counts",
      res.status_code == 200 and card.get("reported") == "ann" and card.get("line_count") == 2
      and card.get("people") == 1 and card.get("reasons") == {"harassment": 1, "spam": 1}
      and card.get("actionable") is True and (tab.get("open"), tab.get("open_players")) == (2, 1)
      and sorted(r["channel"] for r in tab.get("reports", [])) == ["player", "world"]
      and any(r["body"] == "Reported in dark forest" for r in tab["reports"]), tab)
limits["per_card"] = 1
check("  a card shows the newest few lines - the tunable handed in - and says how many there are",
      len(client_a.get("/api/staff/reports", headers=who("boss")).get_json()["players"][0]["lines"]) == 1)
limits["per_card"] = 10
check("  and nobody else is shown the tab", client_a.get("/api/staff/reports", headers=who("ann")).status_code == 404)
app_a.config["boss_rank"] = "mod"
limits["mod_mute"] = 60
check("a mod's longest mute is the tunable handed in",
      staff_post("/api/staff/mute", {"username": "ann", "minutes": 120, "reason": "spam"})
      == (403, {"error": "Forbidden", "message": "A mod may mute for at most 60 minutes."}))
code, body = staff_post("/api/staff/mute", {"username": "ann", "minutes": 10, "reason": "spamming world"})
check("  muted for ten minutes: stored, logged, and her two reported lines closed with it",
      code == 200 and body.get("muted") is True and body.get("reports_closed") == 2
      and column(2, "chat_muted_until") > int(time.time()) and column(2, "chat_muted_by") == "boss"
      and staff_log[-1] == "boss mute ann: 10 minutes: spamming world - closed 2 reported lines"
      and client_a.get("/api/staff/reports", headers=who("boss")).get_json()["open"] == 0, (body, staff_log[-1:]))
check("  and unmuted", staff_post("/api/staff/unmute", {"username": "ann"}) == (200, {"username": "ann", "muted": False})
      and column(2, "chat_muted_until") == 0)
check("a mod may not ban for good, nor for longer than %d days" % moderation.MAX_MOD_BAN_DAYS,
      staff_post("/api/staff/ban", {"username": "bo", "reason": "griefing"})[0] == 403
      and staff_post("/api/staff/ban", {"username": "bo", "reason": "griefing", "days": 31})[1].get("message")
      == "A mod may ban for at most 30 days.")
sessions_of(3, live=2)
code, body = staff_post("/api/staff/ban", {"username": "bo", "reason": "griefing", "days": 3})
check("  three days: stored, every session of his ended, and logged",
      code == 200 and body.get("permanent") is False and column(3, "is_banned") == 1
      and abs(body["expires_at"] - (int(time.time()) + 3 * 86400)) < 5 and sessions_of(3) == 0
      and staff_log[-1] == "boss ban bo: 3 days: griefing", (body, staff_log[-1:]))
check("  and lifted", staff_post("/api/staff/unban", {"username": "bo"}) == (200, {"username": "bo", "banned": False})
      and column(3, "is_banned") == 0)
sessions_of(2, live=2)
code, body = staff_post("/api/staff/kick", {"username": "ann", "reason": "stuck client"})
check("a kick signs her out everywhere and says from how many places",
      code == 200 and body.get("sessions_ended") == 2 and sessions_of(2) == 0
      and staff_log[-1] == "boss kick ann: 2 session(s): stuck client", (body, staff_log[-1:]))
check("  somebody out of reach - himself, at his own rank - is the same 404 as nobody",
      staff_post("/api/staff/kick", {"username": "boss"}) == staff_post("/api/staff/kick", {"username": "nobody"})
      == (404, {"error": "Not Found", "message": "No such account."}))
app_a.config["boss_rank"] = "owner"
check("ranks: the owner makes bo a mod, the ladder handed in",
      staff_post("/api/staff/role", {"username": "bo", "role": "mod"}, method="put")
      == (200, {"username": "bo", "role": "mod", "was": "player"}))
app_a.config["boss_rank"] = "dev"
check("  a dev cannot make a dev, and nobody can make an owner",
      staff_post("/api/staff/role", {"username": "bo", "role": "dev"}, method="put")[0] == 403
      and staff_post("/api/staff/role", {"username": "bo", "role": "owner"}, method="put")[0] == 400)
staff_post("/api/staff/role", {"username": "bo", "role": "player"}, method="put")
app_a.config["boss_rank"] = "owner"
staff_post("/api/chat/report", {"id": ann_lines[1], "reason": "hate"}, as_who="bo")
code, body = staff_post("/api/staff/reports/resolve", {"message_id": ann_lines[1], "outcome": "dismissed"})
check("closing a line's reports: closed, logged, and nothing left to close",
      (code, body) == (200, {"message_id": ann_lines[1], "outcome": "dismissed", "closed": 1})
      and staff_log[-1].startswith("boss report ann: dismissed: line")
      and staff_post("/api/staff/reports/resolve", {"username": "ann", "outcome": "dismissed"})[0] == 404, body)
def desk(path, as_who="boss"):
    res = client_a.get(path, headers=who(as_who))
    return res.status_code, res.get_json() or {}


# The record so far, as staff wrote it, and what the account page reads.
conn = sqlite3.connect(path_a)
start = int(time.time()) - 60
conn.executemany("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
                 " VALUES (1, 'boss', ?, ?, ?, ?, ?)",
                 [("ban", 3, "bo", "3 days: griefing", start), ("note", 2, "ann", "talks a lot", start + 1),
                  ("guild_rename", None, "The Crowned", "-> The First", start + 2)])
conn.executemany("INSERT INTO login_attempts VALUES ('ann', ?, ?, ?, ?)",
                 [(1, "ok", "203.0.113.7", start), (0, "password", "203.0.113.9", start + 5)])
conn.execute("INSERT INTO accounts (user_id, lusions) VALUES (2, 7)")
conn.execute("INSERT INTO sessions (user_id, last_seen_at, expires_at) VALUES (2, ?, ?)",
             (int(time.time()), int(time.time()) + 3600))
conn.commit()
conn.close()
code, page = desk("/api/staff/users?limit=2")
check("staffdesk: the list is a page - two names, the cursor after the last, and how many matched",
      code == 200 and [a["username"] for a in page.get("accounts", [])] == ["ann", "bo"]
      and page.get("more") is True and page.get("next_after") == "bo" and page.get("matched") == 3, page)
ann_row = (page.get("accounts") or [{}])[0]
check("  each name with its rank, its record and whether it is online - by the stand-ins handed in",
      ann_row.get("record") == {"note": 1} and page["accounts"][1].get("record") == {"ban": 1}
      and ann_row.get("online") is True and ann_row.get("role") == "player" and page.get("online") == 1, ann_row)
code, rest = desk("/api/staff/users?limit=2&after=bo")
check("  the next page starts after the cursor, and says it is the last",
      [a["username"] for a in rest.get("accounts", [])] == ["boss"] and rest.get("more") is False
      and rest["accounts"][0]["role"] == "owner", rest)
check("  filtered in the query - staff by role_for() as SQL, a search by any part of a name",
      [a["username"] for a in desk("/api/staff/users?show=staff")[1].get("accounts", [])] == ["boss"]
      and [a["username"] for a in desk("/api/staff/users?q=O")[1].get("accounts", [])] == ["bo", "boss"])
check("  an unknown filter or limit is the 400 it was, and nobody else sees the list",
      desk("/api/staff/users?show=friends")[0] == 400 and desk("/api/staff/users?limit=0")[0] == 400
      and desk("/api/staff/users", as_who="ann")[0] == 404)
code, log = desk("/api/staff/actions")
check("the moderation log, newest first, with every kind it knows",
      code == 200 and [e["action"] for e in log.get("actions", [])] == ["guild_rename", "note", "ban"]
      and log.get("kinds") == list(staffdesk.STAFF_ACTION_KINDS), log.get("actions"))
code, about = desk("/api/staff/actions?player=BO")
check("  about one player - by the account, in any case - with the tally of the whole record",
      [e["detail"] for e in about.get("actions", [])] == ["3 days: griefing"] and about.get("summary") == {"ban": 1},
      about)
check("  a page and its cursor, and an unknown kind is the 400 it was",
      desk("/api/staff/actions?limit=1")[1].get("next_before") is not None
      and desk("/api/staff/actions?action=hug")[0] == 400)
code, body = staff_post("/api/staff/note", {"username": "ann", "text": "  told to stop\n spamming ", "kind": "warn"})
check("a warning on ann's record: one line, logged through the writer handed in, its id the log's",
      code == 200 and body["entry"]["detail"] == "told to stop spamming" and body["entry"]["action"] == "warn"
      and desk("/api/staff/actions?limit=1")[1]["actions"][0]["id"] == body["entry"]["id"], body)
check("  a kind that is not one, or no text, is the 400 it was",
      staff_post("/api/staff/note", {"username": "ann", "text": "x", "kind": "praise"})[0] == 400
      and staff_post("/api/staff/note", {"username": "ann", "text": "   "})[0] == 400)
code, view = desk("/api/staff/user/ann")
check("an account's page: the character she plays, her logins with their addresses, her kills, lusions, record",
      code == 200 and [(c["name"], c["playing"]) for c in view.get("characters", [])] == [("Ann", True)]
      and [r.get("ip") for r in view["logins"]["recent"]] == ["203.0.113.9", "203.0.113.7"]
      and view["logins"]["failed"] == 1 and view["logins"]["addresses"] == 2 and view.get("lusions") == 7
      and len(view.get("kills", [])) == 6 and [h["action"] for h in view.get("staff_history", [])] == ["warn", "note"]
      and view["sessions"]["active"] == 1 and view["linked_accounts"]["visible"] is True
      and view["linked_accounts"]["crowded_at"] == 6, view)
check("  and nobody is the 404 it was", desk("/api/staff/user/nobody")[0] == 404)
conn = sqlite3.connect(path_a)
conn.execute("UPDATE users SET role = 'mod' WHERE id = 3")
conn.execute("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
             " VALUES (1, 'boss', 'note', 3, 'bo', 'a private word', ?)", (int(time.time()),))
conn.commit()
conn.close()
app_a.config["boss_rank"] = "mod"
hidden = [e["detail"] for e in desk("/api/staff/actions?player=bo")[1].get("actions", [])]
code, view = desk("/api/staff/user/bo")
check("a mod reading another mod: no notes about them, in the log or on their page, and no addresses",
      hidden == ["3 days: griefing"] and [h["action"] for h in view.get("staff_history", [])] == ["ban"]
      and view["linked_accounts"]["visible"] is False and view.get("actionable") is False, (hidden, view))
app_a.config["boss_rank"] = "owner"
conn = sqlite3.connect(path_a)
conn.execute("UPDATE users SET role = 'player' WHERE id = 3")
conn.commit()
conn.close()
def gear(path, body):
    res = client_a.post(path, headers=who("ann"), json=dict({"slot": 0}, **body))
    return res.status_code, res.get_json() or {}


def ann_row(column):
    conn = sqlite3.connect(path_a)
    value = conn.execute("SELECT %s FROM saves WHERE user_id = 2 AND slot = 0" % column).fetchone()[0]
    conn.close()
    return value


conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE user_id = 2")
conn.executemany("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)",
                 [(0, "cobaltrobe", 1), (1, "smallhealthpotion", 2), (2, "crownhelm", 1)])
conn.execute("UPDATE saves SET hp = 150, max_hp = 150, mana = 40, max_mana = 50, equipment = '{}'"
             " WHERE user_id = 2 AND slot = 0")
conn.commit()
conn.close()
code, body = gear("/api/character/equip", {"item_id": "cobaltrobe", "position": 0})
check("equipment: ann puts on the robe - taken from the cell she used, through the grant handed in",
      code == 200 and body.get("equip_slot") == "chest" and body.get("equipment") == {"chest": "cobaltrobe"}
      and body.get("unequipped") == "" and "cobaltrobe" not in [c["item_id"] for c in body.get("inventory", [])]
      and json.loads(ann_row("equipment")) == {"chest": "cobaltrobe"} and app_a.config["preferred"][-1] == 0, body)
check("  her ceilings are what is now worn - by the maxima handed in - and her hp is brought down to them",
      body.get("stats") == {"hp": 120, "max_hp": 120, "mana": 40, "max_mana": 50}, body.get("stats"))
check("  the catalogue's no is the 403 it was: too low a level, and something that is not gear",
      gear("/api/character/equip", {"item_id": "crownhelm"})
      == (403, {"error": "Forbidden", "message": "That needs level 9.", "needs": 9})
      and gear("/api/character/equip", {"item_id": "pebble"}) == (403, {"error": "Forbidden",
                                                                      "message": "That is not equipment."}))
check("  and what she does not carry, or nothing at all, the 404 and 400 they were",
      gear("/api/character/equip", {"item_id": "ironsword"})
      == (404, {"error": "Not Found", "message": "You are not carrying that."})
      and gear("/api/character/equip", {"item_id": "nothing"})
      == (400, {"error": "Bad Request", "message": "No such item 'nothing'."}))
conn = sqlite3.connect(path_a)
conn.execute("INSERT INTO carry_items VALUES (2, 0, 0, 'pebble', 1)")
conn.execute("INSERT INTO carry_items VALUES (2, 0, 3, 'pebble', 1)")
conn.commit()
conn.close()
check("  a full bag keeps the robe on: the 409 it was, and nothing moved",
      gear("/api/character/unequip", {"equip_slot": "chest"}) == (409, {"error": "Conflict", "message": "Your bag is full."})
      and json.loads(ann_row("equipment")) == {"chest": "cobaltrobe"})
conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE user_id = 2 AND item_id = 'pebble'")
conn.commit()
conn.close()
code, body = gear("/api/character/unequip", {"equip_slot": "chest"})
check("  taken off: back in the bag, nothing worn, and the ceiling down again",
      code == 200 and body.get("unequipped") == "cobaltrobe" and body.get("equipment") == {}
      and "cobaltrobe" in [c["item_id"] for c in body.get("inventory", [])] and body["stats"]["max_hp"] == 100, body)
check("  twice is the 404 it was, and a place gear does not go the 400",
      gear("/api/character/unequip", {"equip_slot": "chest"}) == (404, {"error": "Not Found",
                                                                      "message": "Nothing is worn there."})
      and gear("/api/character/unequip", {"equip_slot": "hat"})[0] == 400)
code, body = gear("/api/character/consume", {"item_id": "smallhealthpotion", "position": 1})
conn = sqlite3.connect(path_a)
grants = conn.execute("SELECT item_id, target, amount FROM consume_grants WHERE user_id = 2").fetchall()
conn.close()
check("consuming: ann drinks one - taken by the grant handed in, and what it restores written down from the catalogue",
      code == 200 and body.get("remaining") == 1 and body.get("restores") == {"target": "hp", "amount": 25}
      and grants == [("smallhealthpotion", "hp", 25)] and app_a.config["preferred"][-1] == 1, (body, grants))
conn = sqlite3.connect(path_a)
conn.executemany("INSERT INTO carry_items VALUES (2, 0, ?, ?, 1)", [(4, "elixir"), (5, "chefsoup")])
conn.commit()
conn.close()
check("  the catalogue's no is the 403 it was - her level, and a skill from the levels handed in",
      gear("/api/character/consume", {"item_id": "elixir"}) == (403, {"error": "Forbidden", "message": "You need level 30."})
      and gear("/api/character/consume", {"item_id": "chefsoup"})
      == (403, {"error": "Forbidden", "message": "You need Cooking level 5."}))
check("  and what she does not carry is the 404 before anything else, what is not a drink the 409",
      gear("/api/character/consume", {"item_id": "smallmanapotion"})[0] == 404
      and gear("/api/character/consume", {"item_id": "cobaltrobe"})
      == (409, {"error": "Conflict", "message": "That cannot be used."}))
def died(gold=None, bank=None, lusions=None):
    conn = sqlite3.connect(path_a)
    conn.execute("UPDATE saves SET hp = 0 WHERE user_id = 2 AND slot = 0")
    if gold is not None:
        conn.execute("UPDATE saves SET gold = ? WHERE user_id = 2 AND slot = 0", (gold,))
    conn.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (2)")
    if bank is not None:
        conn.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = 2", (bank,))
    if lusions is not None:
        conn.execute("UPDATE accounts SET lusions = ? WHERE user_id = 2", (lusions,))
    conn.commit()
    conn.close()


def ann_says(sql, args=()):
    conn = sqlite3.connect(path_a)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


check("death: the living are not revived, and a currency that is not one is the 400 it was",
      gear("/api/character/revive", {}) == (409, {"error": "Conflict", "message": "That character is not dead."})
      and gear("/api/character/revive", {"pay": "beans"})[0] == 400)
died(lusions=5)
check("  dead, with 5 lusions: the 402 it was, with both numbers",
      gear("/api/character/revive", {}) == (402, {"error": "Payment Required",
                                                  "message": "Reviving costs 20 lusions; you have 5.",
                                                  "cost": 20, "lusions": 5}))
died(lusions=25)
score_before = ann_says("SELECT score FROM accounts WHERE user_id = 2")[0][0]
code, body = gear("/api/character/revive", {})
check("  with 25: back at the maxima handed in, 20 lusions through the ledger handed in, a grant, and the score in gold",
      code == 200 and body.get("paid_with") == "lusions" and body.get("lusions") == 5
      and body.get("status", {}).get("hp") == 100
      and ann_says("SELECT delta, reason FROM lusion_ledger WHERE user_id = 2") == [(-20, "revive")]
      and ann_says("SELECT item_id FROM consume_grants WHERE user_id = 2 AND item_id LIKE '\\_\\_%' ESCAPE '\\'")
      == [("__revive__",)]
      and ann_says("SELECT score FROM accounts WHERE user_id = 2")[0][0] == score_before + 20 * 1000, body)
died(gold=280, bank=1000)
code, body = gear("/api/character/revive", {"pay": "gold"})
check("  paid in gold: eight tenths of everything, the carry first and then the bank, each burned in its ledger",
      code == 200 and body.get("cost") == 1024 and body.get("bank_gold") == 256
      and ann_says("SELECT gold FROM saves WHERE user_id = 2 AND slot = 0") == [(0,)]
      and ann_says("SELECT slot, delta FROM gold_ledger WHERE user_id = 2 AND reason = 'revive'") == [(0, -280), (None, -744)],
      body)
died(gold=0, bank=0)
check("  with no gold at all, the 402 it was",
      gear("/api/character/revive", {"pay": "gold"})[1].get("message") == "You have no gold to pay with.")
died(gold=40)
check("  and with less than the floor, the 402 that says the floor",
      gear("/api/character/revive", {"pay": "gold"})[1].get("message") == "Reviving costs at least 100 gold; you have 40.")
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET equipment = ? WHERE user_id = 2 AND slot = 0", (json.dumps({"chest": "cobaltrobe"}),))
conn.commit()
conn.close()
code, body = gear("/api/character/respawn", {})
check("respawn: the carried gold through the ledger, the bag and every worn piece gone, the bare maxima, and a grant",
      code == 200 and body.get("gold_lost") == 40 and body.get("gear_lost") == ["cobaltrobe"]
      and body.get("equipment") == {} and body.get("status", {}).get("max_hp") == 100
      and ann_says("SELECT COUNT(*) FROM carry_items WHERE user_id = 2 AND slot = 0") == [(0,)]
      and ann_says("SELECT equipment FROM saves WHERE user_id = 2 AND slot = 0") == [("{}",)]
      and ann_says("SELECT delta, reason FROM gold_ledger WHERE user_id = 2 AND reason = 'death'") == [(-40, "death")]
      and ("__respawn__",) in ann_says("SELECT item_id FROM consume_grants WHERE user_id = 2"), body)
check("  and the living, or nobody, are the 409 and 404 they were",
      gear("/api/character/respawn", {})[0] == 409
      and gear("/api/character/respawn", {"slot": 1}) == (404, {"error": "Not Found", "message": "No character in slot 1."}))
def banked():
    return [(c["item_id"], c["quantity"]) if c else None
            for c in client_a.get("/api/account", headers=who("ann")).get_json()["bank_inventory"]]


conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE user_id = 2")
conn.executemany("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)",
                 [(0, "smallhealthpotion", 7), (1, "cobaltrobe", 1), (2, "ironsword", 1), (3, "ironsword", 1)])
conn.execute("DELETE FROM bank_items WHERE user_id = 2")
conn.execute("UPDATE saves SET gold = 500 WHERE user_id = 2 AND slot = 0")
conn.execute("UPDATE accounts SET bank_gold = 0 WHERE user_id = 2")
conn.commit()
conn.close()
code, view = desk("/api/account", as_who="ann")
check("bank: the account - the bank's cells at the size handed in, empty, and the account row's balances",
      code == 200 and view.get("bank_inventory") == [None] * 4 and view.get("capacity") == 4
      and view.get("bank_gold") == 0 and view.get("lusions") == 5, view)
code, body = gear("/api/bank/items", {"op": "deposit", "item_id": "smallhealthpotion", "quantity": 7})
check("  seven potions in: packed by the catalogue handed in - a five and a two - and gone from the bag",
      code == 200 and banked() == [("smallhealthpotion", 5), ("smallhealthpotion", 2), None, None]
      and "smallhealthpotion" not in [c["item_id"] for c in body.get("inventory", [])], body)
check("  more than she carries is the 400 it was, in its words",
      gear("/api/bank/items", {"op": "deposit", "item_id": "cobaltrobe", "quantity": 3})[1].get("message")
      == "Cannot move 3 cobaltrobe - only 1 carried.")
gear("/api/bank/items", {"op": "deposit", "item_id": "cobaltrobe"})
code, body = gear("/api/bank/items", {"op": "deposit", "item_id": "ironsword", "quantity": 2})
check("  two swords and one free cell: the 409 it was, and both swords are still in her bag",
      (code, body) == (409, {"error": "Conflict", "message": "The bank is full (4 slots)."})
      and len([r for r in ann_says("SELECT item_id FROM carry_items WHERE user_id = 2") if r[0] == "ironsword"]) == 2,
      body)
code, body = gear("/api/bank/items", {"op": "withdraw", "item_id": "smallhealthpotion", "quantity": 3})
check("  three back out: from the highest cell first, through the grant handed in",
      code == 200 and banked()[:2] == [("smallhealthpotion", 4), None]
      and ("smallhealthpotion", 3) in [(c["item_id"], c["quantity"]) for c in body.get("inventory", [])], body)
code, body = gear("/api/bank/gold", {"op": "deposit", "amount": 200})
check("gold: 200 banked - both sides in one go, the total the same",
      code == 200 and body.get("carried_gold") == 300 and body.get("bank_gold") == 200
      and ann_says("SELECT gold FROM saves WHERE user_id = 2 AND slot = 0") == [(300,)], body)
check("  and no more out than is in, the 400 it was",
      gear("/api/bank/gold", {"op": "withdraw", "amount": 500})[1].get("message")
      == "Cannot withdraw 500 - only 200 banked.")
code, body = gear("/api/bank/move", {"from": 0, "to": 3, "item_id": "smallhealthpotion"})
check("a bank cell moved, through the grid's move handed in; a move of what is not there is the 409 with the bank",
      code == 200 and [c and c["item_id"] for c in body["bank_inventory"]] == [None, None, "cobaltrobe",
                                                                                 "smallhealthpotion"]
      and gear("/api/bank/move", {"from": 0, "to": 1, "item_id": "smallhealthpotion"})[1].get("account", {})
      .get("capacity") == 4, body)
code, body = gear("/api/bank/discard", {"position": 2, "item_id": "cobaltrobe"})
check("  and a stack destroyed, said so",
      code == 200 and body.get("discarded") == {"item_id": "cobaltrobe", "quantity": 1} and banked()[2] is None, body)
res = client_a.put("/api/account/bank", headers=who("ann"),
                   json={"bank_inventory": [{"item_id": "crownhelm", "quantity": 1}, None, None, None]})
check("a whole bank from a player is ignored, with the bank the server holds",
      res.status_code == 200 and res.get_json().get("ignored") == ["bank_inventory"] and banked()[0] is None)
res = client_a.put("/api/account/lusions", headers=who("ann"), json={"lusions": 9999})
check("  and so is a lusion balance, read back from the account",
      res.get_json().get("ignored") == ["lusions"] and res.get_json().get("lusions") == 5)
def ann_carry(app=None):
    """ann's slot 0 as the carry hands it back, (item_id, quantity) or None a
    cell - read through the package's own inventory_of()."""
    app = app or app_a
    with app.app_context():
        return [(c["item_id"], c["quantity"]) if c else None for c in app.config["carry"].inventory_of(2, 0)]


def cells_of(body):
    return [(c["item_id"], c["quantity"]) if c else None for c in body.get("inventory", [])]


def handed_back(area, name, *args, **kwargs):
    """Call one of what register() handed back, in a request's context, and
    commit - as the routes that use them do."""
    with app_a.app_context():
        out = getattr(app_a.config[area], name)(*args, **kwargs)
        app_a.config["world_db"]().commit()
    return out


conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE user_id = 2")
conn.executemany("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)",
                 [(0, "smallhealthpotion", 3), (1, "pebble", 1), (4, "smallhealthpotion", 4)])
conn.commit()
conn.close()
code, body = gear("/api/character/inventory/move", {"from": 1, "to": 2, "item_id": "pebble"})
check("carry: a drag onto an empty cell moves it - and the answer is every carried cell, the bag's four and two keys",
      code == 200 and cells_of(body) == [("smallhealthpotion", 3), None, ("pebble", 1), None,
                                         ("smallhealthpotion", 4), None], body)
gear("/api/character/inventory/move", {"from": 4, "to": 0, "item_id": "smallhealthpotion"})
check("  a key's potions onto the bag's merge up to the stack the catalogue handed in allows, the rest staying put",
      ann_carry() == [("smallhealthpotion", 5), None, ("pebble", 1), None, ("smallhealthpotion", 2), None], ann_carry())
gear("/api/character/inventory/move", {"from": 2, "to": 0, "item_id": "pebble"})
check("  and onto anything else they swap",
      ann_carry()[:3] == [("pebble", 1), None, ("smallhealthpotion", 5)], ann_carry())
code, body = gear("/api/character/inventory/move", {"from": 1, "to": 3, "item_id": "pebble"})
check("  a cell that does not hold what the game saw is the 409 it was, with the carry and the purse, and nothing moved",
      code == 409 and body.get("message") == "Your backpack changed on the server. It has been reloaded."
      and body.get("resync", {}).get("reason") == "stale_save" and body["resync"].get("gold") == ann_row("gold")
      and cells_of(body["resync"]) == ann_carry() and ann_carry()[0] == ("pebble", 1), body)
code, body = gear("/api/character/inventory/discard", {"position": 0, "item_id": "pebble"})
check("  the bin destroys the stack, and says what it was",
      code == 200 and body.get("discarded") == {"item_id": "pebble", "quantity": 1} and ann_carry()[0] is None, body)
check("  and a cell off the carry is the 400 of the parser handed in",
      gear("/api/character/inventory/discard", {"position": 6, "item_id": "pebble"})
      == (400, {"error": "Bad Request", "message": "position must be an integer 0-5"}))
conn = sqlite3.connect(path_a)
conn.executemany("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)", [(1, "goldpile", 3), (3, "lusions", 4)])
conn.commit()
conn.close()
gold_before, lusions_before = ann_row("gold"), ann_says("SELECT lusions FROM accounts WHERE user_id = 2")[0][0]
code, body = gear("/api/character/inventory/cash", {"position": 1, "item_id": "goldpile"})
check("  a coin pile cashed: worth what money_item_worth() handed in says, through the gold ledger handed in",
      code == 200 and body.get("gold") == gold_before + 30 == ann_row("gold")
      and body.get("cashed") == {"item_id": "goldpile", "quantity": 3, "currency": "gold", "amount": 30}
      and ann_says("SELECT delta, detail FROM gold_ledger WHERE user_id = 2 AND reason = 'pile'") == [(30, "goldpile x3")]
      and ann_carry()[1] is None, body)
code, body = gear("/api/character/inventory/cash", {"position": 3, "item_id": "lusions"})
check("  a lusion pile into lusions, by the pile id handed in, through the lusion ledger, the account read back",
      code == 200 and body.get("lusions") == lusions_before + 4 and body["cashed"]["currency"] == "lusions"
      and ann_says("SELECT delta, reason FROM lusion_ledger WHERE user_id = 2 AND reason = 'pile'") == [(4, "pile")], body)
check("  and a potion is not money, the 409 it was",
      gear("/api/character/inventory/cash", {"position": 2, "item_id": "smallhealthpotion"})
      == (409, {"error": "Conflict", "message": "That is not money."}))


def whole_carry(cells, **more):
    res = client_a.put("/api/character/inventory", headers=who("ann"), json=dict({"slot": 0, "inventory": cells}, **more))
    return res.status_code, res.get_json() or {}


before = ann_carry()
code, body = whole_carry([{"item_id": "crownhelm", "quantity": 1}])
check("a whole carry from a player is ignored, with the carry the server holds",
      code == 200 and body.get("ignored") == ["inventory"] and cells_of(body) == before == ann_carry(), body)
check("  and still validated first, the 400s it was: too many cells, a stack past the catalogue's, an unknown past the ceiling",
      whole_carry([None] * 7)[1].get("message") == "inventory has 7 entries, capacity is 6"
      and whole_carry([{"item_id": "smallhealthpotion", "quantity": 6}])[1].get("message")
      == "inventory[0].quantity is 6, the most smallhealthpotion stacks to is 5"
      and whole_carry([{"item_id": "mystery", "quantity": 100}])[1].get("message")
      == "inventory[0].quantity is 100, the most mystery stacks to is 99")
app_a.config["trade_resyncs"][(2, 0)] = {"slot": 0, "reason": "trade"}
first = whole_carry([])
check("  a trade's resync, handed in, refuses the write once and is gone",
      first == (409, {"error": "Conflict", "message": "Your backpack changed on the server - a trade finished. "
                                                      "It has been reloaded.", "resync": {"slot": 0, "reason": "trade"}})
      and whole_carry([])[0] == 200, first)
code, body = whole_carry([], based_on="0" * 40)
check("  a bag built on another is the 409 with the carry the server holds",
      code == 409 and body["resync"]["reason"] == "stale_save" and cells_of(body["resync"]) == ann_carry(), body)
app_a.config["owners"].add("ann")
code, body = whole_carry([{"item_id": "crownhelm", "quantity": 1}, None])
app_a.config["owners"].discard("ann")
check("  an owner's is written - the whole bag, and a key past the end of the array left as it was",
      code == 200 and ann_carry() == [("crownhelm", 1), None, None, None, ("smallhealthpotion", 2), None]
      and cells_of(body) == ann_carry(), body)
check("the grants: seven potions land in the bag's empty cells, in fives - never on the key already holding some",
      handed_back("carry", "add_to_backpack", 2, 0, "smallhealthpotion", 7) == [1, 2]
      and ann_carry() == [("crownhelm", 1), ("smallhealthpotion", 5), ("smallhealthpotion", 2), None,
                          ("smallhealthpotion", 2), None], ann_carry())
check("  three more top up the part-used stack first",
      handed_back("carry", "add_to_backpack", 2, 0, "smallhealthpotion", 3) == [2]
      and ann_carry()[2] == ("smallhealthpotion", 5), ann_carry())
check("  and two pebbles for one free cell is None, and nothing written",
      handed_back("carry", "add_to_backpack", 2, 0, "pebble", 2) is None and ann_carry()[3] is None, ann_carry())
check("  a take the player did not point at spends the bag before the keys, the highest cell first",
      handed_back("carry", "take_from_backpack", 2, 0, "smallhealthpotion", 6, keys_last=True) is True
      and ann_carry()[1:5] == [("smallhealthpotion", 4), None, None, ("smallhealthpotion", 2)], ann_carry())
check("  one the player did - a key - spends that cell first",
      handed_back("carry", "take_from_backpack", 2, 0, "smallhealthpotion", 1, prefer=4) is True
      and ann_carry()[4] == ("smallhealthpotion", 1) and ann_carry()[1] == ("smallhealthpotion", 4), ann_carry())
check("  more than is carried is False, and nothing taken",
      handed_back("carry", "take_from_backpack", 2, 0, "smallhealthpotion", 99) is False
      and ann_carry()[1] == ("smallhealthpotion", 4), ann_carry())
check("  and what an account holds is the carry or the bank: a potion banked counts for another slot, a helm does not",
      handed_back("carry", "owns_item", 2, 0, "crownhelm") is True
      and handed_back("carry", "owns_item", 2, 1, "smallhealthpotion") is True
      and handed_back("carry", "owns_item", 2, 1, "crownhelm") is False)


def trained(**report):
    res = client_a.post("/api/skill/train", headers=who("ann"), json=dict({"slot": 0}, **report))
    return res.status_code, res.get_json() or {}


code, body = trained(agility=1000, defense=3, magic=4)
check("skills: a training report held to the clock handed in - ten seconds at most - the specialty applied, by the curve",
      code == 200 and body.get("skills") == {"agility": {"level": 3, "xp": 0, "levels_gained": 2},
                                             "defense": {"level": 1, "xp": 3, "levels_gained": 0},
                                             "magic": {"level": 1, "xp": 8, "levels_gained": 0}}
      and ann_row("last_train_at") > 0, body)
code, body = trained(agility=1000)
check("  the same report again at once has no time left to cash",
      code == 200 and body["skills"]["agility"] == {"level": 3, "xp": 0, "levels_gained": 0}, body)
check("  and a bad amount, or nobody, the 400 and 404 they were",
      trained(defense=-1) == (400, {"error": "Bad Request", "message": "defense must be a non-negative integer"})
      and trained(slot=1) == (404, {"error": "Not Found", "message": "No character in slot 1."}))


def skills_write(skills):
    res = client_a.put("/api/character/skills", headers=who("ann"), json={"slot": 0, "skills": skills})
    return res.status_code, res.get_json() or {}


code, body = skills_write({"cooking": {"level": 40, "xp": 5}, "agility": {"level": 99, "xp": 0}})
check("  a skills write: held to the ceiling handed in, the server's own dropped, the rest stored",
      code == 200 and body["skills"].get("cooking") == {"level": 5, "xp": 5}
      and body["skills"].get("agility") == {"level": 3, "xp": 0}, body)
check("  and an unknown skill, or a level of nothing, the 400 it was",
      skills_write({"cookery": {"level": 2}})
      == (400, {"error": "Bad Request",
                "message": "Unknown skill 'cookery'. Known skills: agility, attack, cooking, defense, fishing, magic"})
      and skills_write({"cooking": {"level": 0}})[1].get("message") == "skills.cooking.level must be a positive integer")
check("  the grant handed back: a thousand XP is a hundred levels on that curve, held to five",
      handed_back("skills_api", "grant_xp", 2, 0, "attack", 1000) == (5, 0, 100)
      and handed_back("skills_api", "skills_of", 2, 0).get("attack") == {"level": 5, "xp": 0}
      and handed_back("skills_api", "grant_xp", 2, 0, "nothing", 0) == (1, 0, 0))
def bo_saves(method, path, body=None):
    res = client_a.open(path, method=method, headers=who("bo"), json=body)
    return res.status_code, res.get_json() or {}


def bo_row(slot, column):
    return ann_says("SELECT %s FROM saves WHERE user_id = 3 AND slot = ?" % column, (slot,))[0][0]


code, body = bo_saves("GET", "/api/save")
check("saves: an account's slots - bo's one, its JSON columns read back",
      code == 200 and body.get("username") == "bo" and [s["slot"] for s in body["slots"]] == [0]
      and body["slots"][0]["equipment"] == {} and body["slots"][0]["explored"] == {}, body)
made = bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "Healer", "name": "  Bo   Two ", "level": 50,
                                    "area": "town", "equipment": {"chest": "cobaltrobe"}, "hotbar": ["x"],
                                    "active_pet_id": "petmage",
                                    "explored": {"town": {"w": 2, "h": 2, "bits": "AA"}}})
check("  a new one: the name by the text rule handed in, its level never the body's, full at the maxima handed in",
      made[0] == 200 and bo_row(1, "name") == "Bo Two" and bo_row(1, "class_id") == "healer"
      and bo_row(1, "level") == 1 and bo_row(1, "area") == "town" and bo_row(1, "hp") == 100
      and bo_row(1, "max_mana") == 50 and bo_row(1, "xp_to_next") == 1250, made)
check("  and the pet it does not hold, the equipment and the hotbar are said to be ignored, nothing worn",
      made[1].get("ignored") == ["active_pet_id", "equipment", "hotbar"] and made[1].get("equipment") == {}
      and made[1].get("active_pet_id") == "" and bo_row(1, "equipment") == "{}" and snapshots_of(app_a) == [], made)
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET hp = 10 WHERE user_id = 3 AND slot = 1")
conn.execute("INSERT INTO carry_items VALUES (3, 1, 0, 'petmage', 1)")
conn.commit()
conn.close()
code, body = bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "healer", "name": "Bo Two", "active_pet_id": "petmage"})
check("  saved again: what was not sent is kept, a held pet is out, the pools never refilled, a snapshot asked for",
      code == 200 and body.get("active_pet_id") == "petmage" and "ignored" not in body
      and bo_row(1, "area") == "town" and '"town"' in bo_row(1, "explored") and bo_row(1, "hp") == 10
      and snapshots_of(app_a) == [(3, 1, "save")], body)
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET equipment = '{\"chest\": \"cobaltrobe\"}', max_hp = 120, hp = 120"
             " WHERE user_id = 3 AND slot = 1")
conn.commit()
conn.close()
bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "healer", "name": "Bo Two"})
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET equipment = '{}' WHERE user_id = 3 AND slot = 1")
conn.commit()
conn.close()
bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "healer", "name": "Bo Two"})
check("  and a ceiling that fell takes the pool down with it",
      bo_row(1, "max_hp") == 100 and bo_row(1, "hp") == 100, (bo_row(1, "max_hp"), bo_row(1, "hp")))
check("  the 400s it had: a class, a name too long, a map of the wrong shape, a pet id too long",
      bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "bard", "name": "x"})[1].get("message")
      == "class_id must be one of: healer, mage, tank, warrior"
      and bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "mage", "name": "x" * 21})[1].get("message")
      == "name must be 1-20 characters"
      and bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "mage", "name": "x", "explored": []})[1].get("message")
      == "explored must be an object of {area: {w, h, ox, oy, bits}} and under 65536 bytes"
      and bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "mage", "name": "x",
                                        "active_pet_id": "p" * 65})[1].get("message")
      == "active_pet_id must be a string of at most 64 characters")
code, body = bo_saves("GET", "/api/character?slot=1")
check("  the whole character in one read: who, the vitals, the carry, what is worn, the skills",
      code == 200 and body.get("class_id") == "healer" and body.get("name") == "Bo Two"
      and body.get("active_pet_id") == "petmage" and body.get("status", {}).get("hp") == 100
      and body.get("inventory", [None])[0] == {"item_id": "petmage", "quantity": 1} and len(body["inventory"]) == 6
      and body.get("equipment") == {} and body.get("skills") == {}, body)
check("  and an empty slot, or no slot, the 404 and 400 they were",
      bo_saves("GET", "/api/character?slot=2") == (404, {"error": "Not Found", "message": "No character in slot 2."})
      and bo_saves("GET", "/api/character?slot=9")[0] == 400)
conn = sqlite3.connect(path_a)
conn.execute("UPDATE trades SET state = 'cancelled' WHERE state = 'open'")
conn.execute("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
             " VALUES ('t-saves', 2, 0, 3, 1, 'open', 0, 0)")
conn.execute("UPDATE saves SET gold = 70 WHERE user_id = 3 AND slot = 1")
conn.commit()
conn.close()
check("deleting: the name not typed is the 400 it was, and a character in a trade the 409",
      bo_saves("POST", "/api/character/delete", {"slot": 1, "confirm": "Bo"})
      == (400, {"error": "Bad Request", "message": "Type the character's name to delete it."})
      and bo_saves("POST", "/api/character/delete", {"slot": 1, "confirm": "bo two"})
      == (409, {"error": "Conflict", "message": "This character is in a trade. Finish or cancel it first."}))
conn = sqlite3.connect(path_a)
conn.execute("UPDATE trades SET state = 'cancelled' WHERE trade_id = 't-saves'")
conn.commit()
conn.close()
code, body = bo_saves("POST", "/api/character/delete", {"slot": 1, "confirm": " BO TWO "})
kept = ann_says("SELECT class_id, name, gold, snapshot FROM character_deletions WHERE user_id = 3")
check("  typed, it goes: the purse through the ledger handed in, its rows gone, and a copy kept",
      body == {"slot": 1, "deleted": True, "gold_lost": 70, "items_lost": 1}
      and ann_says("SELECT delta, reason FROM gold_ledger WHERE user_id = 3 AND reason = 'character-deleted'")
      == [(-70, "character-deleted")]
      and ann_says("SELECT COUNT(*) FROM saves WHERE user_id = 3 AND slot = 1") == [(0,)]
      and ann_says("SELECT COUNT(*) FROM carry_items WHERE user_id = 3 AND slot = 1") == [(0,)]
      and len(kept) == 1 and kept[0][:3] == ("healer", "Bo Two", 70)
      and json.loads(kept[0][3])["carry_items"] == [{"position": 0, "item_id": "petmage", "quantity": 1}], (body, kept))
for again in range(2):
    bo_saves("PUT", "/api/save", {"slot": 1, "class_id": "tank", "name": "Again"})
    bo_saves("POST", "/api/character/delete", {"slot": 1, "confirm": "again"})
check("  and an account keeps only the deleted characters it is handed to keep, the newest",
      ann_says("SELECT name FROM character_deletions WHERE user_id = 3 ORDER BY id") == [("Again",), ("Again",)])
check("and nobody without a login gets in anywhere",
      [client_a.get(path).status_code for path in ("/api/server/stats", "/api/world/events/active", "/api/server/insights",
                                                  "/api/kills?slot=0", "/api/kills/everyone", "/api/friends",
                                                  "/api/loot/bag?bag_id=%s" % bag, "/api/guild", "/api/guild/list",
                                                  "/api/party", "/api/party/voice", "/api/chat", "/api/ignores",
                                                  "/api/trade", "/api/trade/history", "/api/staff/trades",
                                                  "/api/economy/kingdom", "/api/server/world", "/api/staff/reports",
                                                  "/api/staff/users", "/api/staff/actions", "/api/staff/user/ann",
                                                  "/api/account", "/api/save", "/api/character?slot=0")]
      + [client_a.post(path, json={"slot": 0}).status_code for path in ("/api/fishing/catch", "/api/cooking/cook",
                                                                         "/api/loot/take", "/api/guild/create",
                                                                         "/api/staff/guild", "/api/party/invite",
                                                                         "/api/chat/send", "/api/chat/image",
                                                                         "/api/trade/offer", "/api/trade/update",
                                                                         "/api/trade/confirm", "/api/trade/cancel",
                                                                         "/api/server/world", "/api/server/world/stir",
                                                                         "/api/staff/ban", "/api/staff/kick",
                                                                         "/api/staff/unban", "/api/staff/mute",
                                                                         "/api/staff/unmute", "/api/chat/report",
                                                                         "/api/report/player",
                                                                         "/api/staff/reports/resolve",
                                                                         "/api/staff/note", "/api/character/equip",
                                                                         "/api/character/unequip",
                                                                         "/api/character/consume",
                                                                         "/api/character/revive",
                                                                         "/api/character/respawn", "/api/bank/items",
                                                                         "/api/bank/gold", "/api/bank/move",
                                                                         "/api/bank/discard",
                                                                         "/api/character/inventory/move",
                                                                         "/api/character/inventory/discard",
                                                                         "/api/character/inventory/cash",
                                                                         "/api/skill/train",
                                                                         "/api/character/delete")]
      + [client_a.put(path, json={}).status_code for path in ("/api/staff/role", "/api/account/bank",
                                                              "/api/account/lusions", "/api/character/inventory",
                                                              "/api/character/skills", "/api/save")]
      == [401] * 68)

# =============================================================================
section("P-3  two apps from the same code, each its own")
now = int(time.time())
res = client_a.post("/api/world/events", headers=who("boss"), json={"kind": "xp", "amount": 2, "starts_at": now, "hours": 1})
check("an event set on the first app", res.status_code == 201, res.get_json())
on_a = client_a.get("/api/world/events/active", headers=who("ann")).get_json()["events"]
on_b = app_b.test_client().get("/api/world/events/active", headers=who("ann")).get_json()["events"]
check("  is on there, and not on the second", [e["name"] for e in on_a] == ["Double XP"] and on_b == [], (on_a, on_b))
check("  its staff log heard of it, the second's did not", len(logged_a) == 1 and "Double XP" in logged_a[0] and logged_b == [],
      (logged_a, logged_b))
client_a.post("/api/world/events", headers=who("boss"), json={"kind": "loot", "amount": 1.5, "starts_at": now + 86400 + 30,
                                                                 "hours": 2})
check("  and what it says - begun, when first asked; one set ahead, coming - is said on its own app only",
      len(said_a) == 2 and said_a[0].startswith("Double XP has begun, for ")
      and said_a[0].endswith(": 2x XP from every monster.")
      and said_a[1] == "Coming up: 1.5x Loot, in 1 day, for 2 hours - Loot bags drop 1.5x as often from every monster."
      and said_b == [], (said_a, said_b))
board_b = app_b.test_client().get("/api/economy/kingdom", headers=who("ann")).get_json() or {}
check("  and the kingdom board's snapshot is its own app's: the second, asked while the first's is fresh, has nothing",
      board_b.get("total") == 0 and board_b.get("top") == [] and fresh.get("total") == 123, board_b)
check("  and the carry is its own app's: what the first one's grants put in ann's bag is not in the second's",
      ann_carry(app_b) == [None] * 6 and ann_carry()[0] == ("crownhelm", 1), ann_carry(app_b))
for path in (path_a, path_b):
    if os.path.exists(path):
        os.remove(path)

# =============================================================================
section("P-4  every package is here")
check("the code map finds the packages app.py imports: %s" % ", ".join(PACKAGES),
      set(PACKAGES) >= {"playerstats", "events", "insights", "killrecord", "friends", "gathering", "loot", "guilds",
                        "parties", "chat", "trade", "kingdom", "world", "moderation", "staffdesk",
                        "equipment", "consuming", "death", "bank", "carry", "skills", "saves"},
      PACKAGES)
check("  and every one of them runs on a bare app in this file", set(PACKAGES) == set(RUN_HERE),
      sorted(set(PACKAGES) ^ set(RUN_HERE)))

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
