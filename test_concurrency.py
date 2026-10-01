"""
Requests that arrive together. Run: python3 test_concurrency.py

Found by playing on day 1. A warrior's slash wave killed two enemies in one
swing, the game reported both kills at once, and one kill's XP never arrived:
both requests read the character's XP before either wrote it, and the second
write replaced the first. Six kills sent together banked 99 XP of the 676 their
answers had promised. The game had added up all 676, levelled up before the
server did, refilled its mana, and the healing check clamped that as a cheat.

The fix is one rule in get_db() (THE WRITE LOCK in app.py): every POST, PUT,
PATCH and DELETE takes SQLite's write lock before its first read, so a second
write request waits for the first to commit and reads what it wrote. This suite
holds it.

  CC-1  KILLS SENT TOGETHER ALL COUNT. Every XP point an answer promised is
        stored, and so is the attack XP.
  CC-2  A LOOT CELL TAKEN TWICE AT ONCE PAYS ONCE. Two takes of the same coins
        used to both pay - a double-click was a duplicate.
  CC-3  A POTION DRUNK FIVE TIMES AT ONCE IS DRUNK ONCE. One potion, one heal
        grant.
  CC-4  ONE PURSE DEPOSITED TWICE AT ONCE. Both deposits used to read the
        same purse, so the bank got the gold twice.
  CC-5  THE RULE ITSELF. A write route holds the lock from its first read and
        keeps it after a commit; a read does not take it; and the only write
        routes left out are the ones named here, each for slow work.

HOW THE RACES ARE MADE TO HAPPEN EVERY TIME. The server runs in this process on
real threads, and the requests are real HTTP sent together behind a barrier. A
race that needs two threads to land inside a few microseconds would pass by
luck, so each section holds the window open: it wraps one function the route
calls between its read and its write so it waits 50 ms. Without the lock every
request reads the same old row inside that wait. With it, the second request
cannot start reading until the first has committed, so the wait changes nothing.
Sabotage-checked: take the lock out of get_db() and CC-1 to CC-4 all fail.
"""
import importlib.util, json, os, sqlite3, sys, tempfile, threading, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_concurrency_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
app = app_module.app
gamedata = app_module.gamedata

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))


# --- a real server on real threads ------------------------------------------
import logging
from werkzeug.serving import make_server
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # one line per request otherwise
_server = make_server("127.0.0.1", 0, app, threaded=True)
threading.Thread(target=_server.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % _server.server_port
# This machine may have a proxy in the environment; a loopback call must not go
# through it.
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with _open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def together(requests):
    """Send every (method, path, body, token) at the same moment; answers in order."""
    answers = [None] * len(requests)
    start = threading.Barrier(len(requests))
    def run(i):
        start.wait()
        answers[i] = call(*requests[i])
    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(requests))]
    for t in threads: t.start()
    for t in threads: t.join()
    return answers

class held_open:
    """Make `owner.name` wait before it answers, for the length of a with-block.
    It is called between the route's read and its write - see the header."""
    def __init__(self, owner, name, seconds=0.05):
        self.owner, self.name, self.seconds = owner, name, seconds
    def __enter__(self):
        self.real = getattr(self.owner, self.name)
        real, seconds = self.real, self.seconds
        def slow(*args, **kwargs):
            time.sleep(seconds)
            return real(*args, **kwargs)
        setattr(self.owner, self.name, slow)
    def __exit__(self, *exc):
        setattr(self.owner, self.name, self.real)


# --- accounts and rows ----------------------------------------------------------
PW = "hunter2hunter2"
_ip = [10]
def register(name):
    _ip[0] += 1
    status, body = call("POST", "/api/auth/register", {"username": name, "password": PW})
    assert status == 201, (status, body)
    return body["token"]

def make(token, slot, class_id):
    status, body = call("PUT", "/api/save", {"slot": slot, "class_id": class_id, "name": class_id}, token)
    assert status == 200, (status, body)

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()

def one(sql, args=()):
    return raw(sql, args)[0][0]

def uid(name):
    return one("SELECT id FROM users WHERE username = ?", (name,))

def mint(user_id, slot, amount):
    """Gold the way the server mints it, so the ledger agrees before the test."""
    raw("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (amount, user_id, slot))
    raw("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (1, ?, ?, ?, 'test', '')",
        (user_id, slot, amount))

def total_xp(level, xp, need):
    """Everything a character has earned: the levels it passed plus the bar."""
    return sum(need(l) for l in range(1, int(level))) + int(xp)


# =============================================================================
section("CC-1  kills sent together all count")
# =============================================================================
token = register("swinger")
me = uid("swinger")
make(token, 0, "warrior")
ENEMIES = [e for e in sorted(gamedata.ENEMIES)
           if gamedata.ENEMIES[e].get("grants_rewards", True)
           and not gamedata.ENEMIES[e].get("slots_are_gear")][:8]

with held_open(gamedata, "roll_kill_rewards"):
    answers = together([("POST", "/api/combat/kill", {"slot": 0, "enemy_id": e}, token) for e in ENEMIES])
check("all %d kills were paid" % len(ENEMIES), all(s == 200 for s, _ in answers),
      [s for s, _ in answers])
promised = sum(int(a.get("xp_gained", 0)) for s, a in answers if s == 200)
level, xp = raw("SELECT level, xp FROM saves WHERE user_id = ? AND slot = 0", (me,))[0]
check("every XP point the answers promised is stored",
      total_xp(level, xp, gamedata.xp_needed_for_level) == promised,
      "promised %d, stored level %s xp %s = %d" % (promised, level, xp, total_xp(level, xp, gamedata.xp_needed_for_level)))
check("and the kill log agrees", one("SELECT COALESCE(SUM(xp), 0) FROM kill_reports WHERE user_id = ?", (me,)) == promised)

attack_promised = sum(int(a.get("attack_xp_gained", 0)) for s, a in answers if s == 200)
skill = raw("SELECT level, xp FROM skills WHERE user_id = ? AND slot = 0 AND skill_id = 'attack'", (me,))
attack_need = lambda level: gamedata.xp_needed_for_skill_level("attack", level)
check("the attack XP is all there too (%d)" % attack_promised,
      bool(skill) and total_xp(skill[0][0], skill[0][1], attack_need) == attack_promised,
      "stored %s" % (skill,))


# =============================================================================
section("CC-2  a loot cell taken twice at once pays once")
# =============================================================================
COIN = "coppercoin"
value = app_module.gold_item_value(COIN)
check("the coin is worth something (%d)" % value, value > 0)
raw("INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES ('twice', ?, 0, 'test', ?)",
    (me, int(time.time())))
raw("INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES ('twice', 0, ?, 4)", (COIN,))
gold_before = one("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (me,))

with held_open(app_module, "gold_item_value"):
    answers = together([("POST", "/api/loot/take", {"bag_id": "twice", "position": 0}, token)] * 5)
codes = sorted(s for s, _ in answers)
check("one take paid and the rest found nothing", codes.count(200) == 1, codes)
check("the purse rose by the coins once (%d)" % (4 * value),
      one("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (me,)) == gold_before + 4 * value,
      one("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (me,)) - gold_before)
check("the ledger minted it once",
      one("SELECT COUNT(*) FROM gold_ledger WHERE user_id = ? AND reason = 'loot'", (me,)) == 1)
check("and the bag is gone", one("SELECT COUNT(*) FROM loot_bags WHERE bag_id = 'twice'") == 0)


# =============================================================================
section("CC-3  a potion drunk five times at once is drunk once")
# =============================================================================
POTION = next((i for i in ("tinyhealthpotion", "smallhealthpotion") if i in gamedata.ITEMS), None)
check("there is a potion to drink", POTION is not None)
raw("DELETE FROM carry_items WHERE user_id = ? AND slot = 0", (me,))
raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, 0, ?, 1)", (me, POTION))
raw("UPDATE saves SET level = MAX(level, 50) WHERE user_id = ? AND slot = 0", (me,))
grants_before = one("SELECT COUNT(*) FROM consume_grants WHERE user_id = ? AND item_id = ?", (me, POTION))

with held_open(gamedata, "consume_check"):
    answers = together([("POST", "/api/character/consume", {"slot": 0, "item_id": POTION}, token)] * 5)
codes = sorted(s for s, _ in answers)
check("one drink went through", codes.count(200) == 1, [(s, a.get("message", "")) for s, a in answers])
check("the potion is gone",
      one("SELECT COALESCE(SUM(quantity), 0) FROM carry_items WHERE user_id = ? AND slot = 0", (me,)) == 0)
check("and it explains one heal, not five",
      one("SELECT COUNT(*) FROM consume_grants WHERE user_id = ? AND item_id = ?", (me, POTION)) == grants_before + 1)


# =============================================================================
section("CC-4  one purse deposited twice at once")
# =============================================================================
# The route reads the purse, then makes sure the account row exists (its first
# write), then writes both balances. Without the lock both requests read 100 in
# the purse; the second then banked 100 that the first had already moved.
token = register("depositor")
me = uid("depositor")
make(token, 0, "warrior")
mint(me, 0, 100)
held = lambda: one("SELECT COALESCE(SUM(gold), 0) FROM saves WHERE user_id = ?", (me,)) + \
               one("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts WHERE user_id = ?", (me,))
check("the account holds 100 to start", held() == 100, held())

with held_open(app_module, "_ensure_account"):
    answers = together([("POST", "/api/bank/gold", {"slot": 0, "op": "deposit", "amount": 100}, token)] * 2)
codes = sorted(s for s, _ in answers)
check("one deposit was paid and the other refused", codes == [200, 400], codes)
check("the account still holds 100, not 200", held() == 100, held())
check("all of it in the bank", one("SELECT bank_gold FROM accounts WHERE user_id = ?", (me,)) == 100)


# =============================================================================
section("CC-5  the rule itself")
# =============================================================================
def holds_lock(method, path):
    with app.test_request_context(path, method=method):
        db = app_module.get_db()
        holding = db.in_transaction
        db.commit()
        after_commit = db.in_transaction
        db.close()
        from flask import g
        g.pop("db", None)
    return holding, after_commit

check("a kill holds the write lock from its first read", holds_lock("POST", "/api/combat/kill")[0])
check("and still holds it after a commit part-way through",
      holds_lock("PUT", "/api/save")[1])
check("a read does not take it", holds_lock("GET", "/api/player/status") == (False, False))
check("a login does not take it before its password hash", holds_lock("POST", "/api/auth/login")[0] is False)

# THE LIST OF EXCEPTIONS IS WRITTEN HERE ON PURPOSE. Adding @no_write_lock to a
# route fails this check until the route is added below with its reason - so
# leaving a route out of the lock is a decision somebody wrote down, not one
# that happened.
SLOW_ROUTES = {
    "login":                "scrypt on the password, and on a staff login code",
    "register":             "scrypt on the new password",
    "change_password":      "scrypt, twice",
    "complete_recovery":    "scrypt on the code and the new password",
    "request_recovery":     "scrypt on the code it stores",
    "set_account_email":    "scrypt on the password and the code",
    "verify_account_email": "scrypt on the code",
    "chat_relay_image":     "fetches a picture from another server",
    "chat_upload_image":    "decodes and re-encodes a picture",
}
left_out = set()
locked = 0
for rule in app.url_map.iter_rules():
    if not (rule.methods & app_module.WRITE_METHODS):
        continue
    view = app.view_functions[rule.endpoint]
    if getattr(view, "_elusion_no_write_lock", False):
        left_out.add(rule.endpoint)
    else:
        locked += 1
check("%d write routes take the lock" % locked, locked >= 50, locked)
check("the only ones left out are the slow ones named here",
      left_out == set(SLOW_ROUTES), sorted(left_out ^ set(SLOW_ROUTES)))


_server.shutdown()
print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
