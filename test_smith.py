"""
test_smith.py - the smith (0.74.0): tempering a piece of gear, and the Royal
Certification a smith needs for the higher tiers.

The owner, 22 Oct: "a new smith spends materials plus gold to upgrade gear",
and of the economy, "upgrades that cost materials, plus a 'Royal
Certification' step to reach higher tiers". Both are sinks, and tempering
rewrites the one thing every route trusts - the item id in a cell - so the
questions here are: does a piece come out as a roll the server can read, is
every refusal free, does every coin go through the ledger, and can a held find
get out of its hold by way of the anvil?

  S-1  STATUS: the rank, every certification with its gold scaled by the
       owner's certification_gold_scale and its materials beside what is
       carried, every tier's temper cost.
  S-2  QUOTE: what a temper would make of the piece in one cell and cost, and
       nothing done.
  S-3  TEMPER REFUSED, each taking nothing: a stale cell, not gear, a plain
       piece, a Perfect, one fully tempered, a tier above the character's
       certification, a material short, a light purse.
  S-4  TEMPERED: every rolled stat up the step, none past the cap, the
       resistance as it was, in the same cell; the materials taken, the gold
       burned (smith_temper) and scaled by temper_gold_scale, at least one; the
       piece sells for what it did; a held find keeps its hold.
  S-5  CERTIFIED: only the next rank, at its level, for its materials and its
       gold (royal_certification) - a scale of nothing burns nothing - and a
       deleted character takes its ranks with it.

After every section canary.py's own invariants hold on this suite's database.

Run:  python test_smith.py
"""
import importlib.util
import math
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_smith_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "smithowner"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata
import canary  # noqa: E402
import economyrules  # noqa: E402

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
    print("\n=== %s ===\n" % title)


def sql(query, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def register(username):
    r = client.post("/api/auth/register", json={"username": username, "password": "hunter2hunter2"})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_char(headers, slot=0, class_id="warrior"):
    r = client.put("/api/save", headers=headers, json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()


def uid(username):
    return int(sql("SELECT id FROM users WHERE username = ?", (username,))[0]["id"])


def bag(username, entries, slot=0):
    sql("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (uid(username), slot))
    for position, item_id, quantity in entries:
        sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
            (uid(username), slot, position, item_id, quantity))


def cells(username, slot=0):
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in sql(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?", (uid(username), slot))}


def purse(username, slot=0):
    return int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (uid(username), slot))[0]["gold"])


def set_gold(username, amount, slot=0):
    """The purse made `amount`, through a ledger row, so the supply invariant
    still holds."""
    delta = int(amount) - purse(username, slot)
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (delta, uid(username), slot))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, ?, ?, 'test_seed', '')",
        (uid(username), slot, delta))


def set_level(username, level, slot=0):
    sql("UPDATE saves SET level = ? WHERE user_id = ? AND slot = ?", (level, uid(username), slot))


def give_rank(username, rank, slot=0):
    sql("DELETE FROM smith_ranks WHERE user_id = ? AND slot = ?", (uid(username), slot))
    for r in range(1, rank + 1):
        sql("INSERT INTO smith_ranks (user_id, slot, rank, earned_at) VALUES (?, ?, ?, 1)", (uid(username), slot, r))


def ledger(reason):
    return [int(r["delta"]) for r in sql("SELECT delta FROM gold_ledger WHERE reason = ? ORDER BY id", (reason,))]


def invariants(label):
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        broken = [(name, detail) for name, ok, detail in canary.check_all(conn) if not ok]
    finally:
        conn.close()
    check("canary: every invariant holds after %s" % label, not broken, broken)


def name_of(item_id):
    return gamedata.item_row(item_id)["display_name"]


def half_up(x):
    return int(math.floor(x + 0.5))


CONSTANTS = gamedata.CONSTANTS
COSTS = {int(c["tier"]): c for c in CONSTANTS["temper_costs"]}
CERTS = sorted(CONSTANTS["certifications"], key=lambda c: c["rank"])
STEP, CAP, RATE = int(CONSTANTS["temper_step"]), int(CONSTANTS["temper_cap"]), float(CONSTANTS["temper_gold_rate"])
real_value = economyrules.value


def rules(**numbers):
    """The owner's economy numbers, for a moment: these, the defaults else."""
    economyrules.value = lambda db, key: float(numbers[key]) if key in numbers else real_value(db, key)


def gear_of_tier(tier, kind="ARMOR"):
    """A catalogue piece of this tier and kind with at least two rolled stats
    if there is one - a roll where one stat can reach the cap before another."""
    found = sorted(i for i, r in gamedata.ITEMS.items() if int(r["tier"]) == tier and r["type_name"] == kind
                   and gamedata.rolled_fields(r))
    two = [i for i in found if len(gamedata.rolled_fields(gamedata.ITEMS[i])) >= 2]
    return (two or found)[0]


def roll_of(base, percents, resist=True):
    """The id of `base` rolled at these percents, one a stat, in order - and,
    for armour, resisting the first element at the bottom of its range."""
    row = gamedata.ITEMS[base]
    part = "".join("%s%d" % (letter, p) for (letter, _f), p in zip(gamedata.rolled_fields(row), percents))
    low, high = gamedata.resist_range(row)
    suffix = "%s%d%02d" % (gamedata.RESIST_LETTER, gamedata.RESIST_ELEMENTS[0], low) if resist and high else ""
    item_id = base + "~" + part + suffix
    assert gamedata.item_row(item_id) is not None, item_id
    return item_id


def smith(path, body, headers=None, slot=0):
    return client.post(path, headers=headers or smith_t, json=dict({"slot": slot}, **body))


def materials_for(tier, extra=0):
    return [(item["item_id"], int(item["quantity"]) + extra) for item in COSTS[tier]["materials"]]


# =============================================================================
section("SETUP")
# =============================================================================
smith_t = register("smithy")
make_char(smith_t)
make_char(smith_t, slot=1, class_id="mage")
check("the catalogue has the smith's numbers: a step, a cap, a rate, a cost for every tier, four ranks",
      STEP > 0 and CAP <= gamedata.QUALITY_HIGH and RATE > 0 and sorted(COSTS) == list(range(1, len(COSTS) + 1))
      and len(CERTS) == 4, (STEP, CAP, RATE, sorted(COSTS), len(CERTS)))
invariants("setup")


# =============================================================================
section("S-1  STATUS")
# =============================================================================
res = client.get("/api/smith/status?slot=0", headers=smith_t)
status = res.get_json() or {}
check("a new smith: rank 0, no name, every rank unearned at its own gold, step and cap",
      res.status_code == 200 and status.get("rank") == 0 and status.get("rank_name") == ""
      and [(r["rank"], r["name"], r["gold"], r["level"], r["earned"]) for r in status.get("ranks", [])]
      == [(c["rank"], c["name"], c["gold"], c["level"], False) for c in CERTS]
      and status.get("temper_step") == STEP and status.get("temper_cap") == CAP, status)
check("  every tier's temper: its gold rate, its materials (with what is carried), its certification",
      [(t["tier"], t["gold_rate"], [(m["item_id"], m["quantity"], m["have"]) for m in t["materials"]],
        t["certification"]) for t in status.get("temper_costs", [])]
      == [(tier, RATE, [(m["item_id"], m["quantity"], 0) for m in COSTS[tier]["materials"]],
           COSTS[tier]["certification"]) for tier in sorted(COSTS)], status.get("temper_costs"))
rules(certification_gold_scale=2.0, temper_gold_scale=3.0)
try:
    scaled = client.get("/api/smith/status?slot=0", headers=smith_t).get_json() or {}
finally:
    economyrules.value = real_value
check("  the owner's scales: every rank's gold doubled, the temper rate tripled",
      [r["gold"] for r in scaled.get("ranks", [])] == [c["gold"] * 2 for c in CERTS]
      and all(abs(t["gold_rate"] - RATE * 3) < 1e-9 for t in scaled.get("temper_costs", [])), scaled.get("ranks"))
check("  a bad slot, an empty one, no login: 400, 404, 401",
      [client.get("/api/smith/status?slot=9", headers=smith_t).status_code,
       client.get("/api/smith/status?slot=3", headers=smith_t).status_code,
       client.get("/api/smith/status?slot=0").status_code] == [400, 404, 401])
invariants("status")


# =============================================================================
section("S-2  QUOTE")
# =============================================================================
chest = gear_of_tier(2)
fields = gamedata.rolled_fields(gamedata.ITEMS[chest])
piece = roll_of(chest, [100, 96][:len(fields)] + [100] * max(0, len(fields) - 2))
_b, rolls, resist = gamedata._parse_variant(piece)
gold = max(1, half_up(int(gamedata.ITEMS[chest]["value"]) * RATE))
set_gold("smithy", 1000)
bag("smithy", [(3, piece, 1)] + [(5 + i, m, q) for i, (m, q) in enumerate(materials_for(2))])
before_cells, before_purse = cells("smithy"), purse("smithy")
res = smith("/api/smith/quote", {"position": 3, "item_id": piece})
quote = res.get_json() or {}
expected_after = {f: min(CAP, p + STEP) for f, p in rolls.items()}
check("the quote on %s: the roll it becomes, before and after, its gold and materials" % piece,
      res.status_code == 200 and quote.get("item_id") == piece and quote.get("before") == rolls
      and quote.get("after") == expected_after and quote.get("gold") == gold and quote.get("can") is True
      and quote.get("reason") == "" and quote.get("certification") == COSTS[2]["certification"]
      and quote.get("materials") == [{"item_id": m, "quantity": q, "have": q} for m, q in materials_for(2)], quote)
check("  and the new id is a roll the server reads, the resistance as it was",
      gamedata.item_row(quote.get("new_item_id", "")) is not None
      and gamedata._parse_variant(quote.get("new_item_id", ""))[2] == resist, quote.get("new_item_id"))
check("  nothing was done: the cell, the materials and the purse as they were",
      cells("smithy") == before_cells and purse("smithy") == before_purse)
stale = smith("/api/smith/quote", {"position": 3, "item_id": chest})
check("  a cell that does not hold what the game saw: the carry's 409 resync",
      stale.status_code == 409 and (stale.get_json() or {}).get("resync", {}).get("reason") == "stale_save")
invariants("quote")


# =============================================================================
section("S-3  TEMPER REFUSED")
# =============================================================================
def refused(label, body, code, message, setup_cells, gold_now=1000):
    """The temper refused, nothing taken - and the quote of the same piece
    says the same words as its reason, with can false (or, for a request
    that is wrong in itself, refuses it the same way)."""
    bag("smithy", setup_cells)
    set_gold("smithy", gold_now)
    cells_before, purse_before, burns = cells("smithy"), purse("smithy"), len(ledger("smith_temper"))
    res = smith("/api/smith/temper", body)
    got = res.get_json() or {}
    check("%s: %d %r, nothing taken" % (label, code, message),
          res.status_code == code and (message is None or got.get("message") == message)
          and cells("smithy") == cells_before and purse("smithy") == purse_before
          and len(ledger("smith_temper")) == burns, (res.status_code, got))
    quoted = smith("/api/smith/quote", body)
    said = quoted.get_json() or {}
    if message is not None:
        check("  ...and its quote: can false, the same reason", quoted.status_code == 200 and said.get("can") is False
              and said.get("reason") == message, said)
    else:
        check("  ...and its quote is refused the same way", quoted.status_code == code, (quoted.status_code, said))
    return got


mats = [(10 + i, m, q) for i, (m, q) in enumerate(materials_for(2))]
refused("not gear (a potion)", {"position": 0, "item_id": "smallhealthpotion"}, 409,
        "The smith tempers weapons and armour.", [(0, "smallhealthpotion", 1)] + mats)
refused("a plain piece, no rolls", {"position": 0, "item_id": chest}, 409,
        "That piece has no rolls to temper.", [(0, chest, 1)] + mats)
perfect = gamedata.perfect_id(chest)
refused("a Perfect piece", {"position": 0, "item_id": perfect}, 409, "That piece is already perfect.",
        [(0, perfect, 1)] + mats)
capped = roll_of(chest, [CAP] * len(fields))
refused("every stat at the cap", {"position": 0, "item_id": capped}, 409, "That piece is fully tempered.",
        [(0, capped, 1)] + mats)
three = gear_of_tier(3, "WEAPON")
three_piece = roll_of(three, [100] * len(gamedata.rolled_fields(gamedata.ITEMS[three])))
needs = COSTS[3]["certification"]
refused("tier 3 without the %s" % CERTS[needs - 1]["name"], {"position": 0, "item_id": three_piece}, 403,
        "You need to be a %s to temper tier 3 gear." % CERTS[needs - 1]["name"],
        [(0, three_piece, 1)] + [(10 + i, m, q) for i, (m, q) in enumerate(materials_for(3))])
short_id, short_q = materials_for(2)[0]
refused("a %s short" % short_id, {"position": 0, "item_id": piece}, 404, "You are missing %s." % name_of(short_id),
        [(0, piece, 1), (10, short_id, short_q - 1)] + [(11 + i, m, q) for i, (m, q) in enumerate(materials_for(2)[1:])])
refused("a light purse", {"position": 0, "item_id": piece}, 400,
        "That costs %d gold and you have %d." % (gold, gold - 1), [(0, piece, 1)] + mats, gold_now=gold - 1)
got = refused("a stale cell", {"position": 0, "item_id": chest}, 409, None, [(0, piece, 1)] + mats)
check("  ...answered with the carry the server holds", got.get("resync", {}).get("reason") == "stale_save", got)
refused("a cell past the carry", {"position": 99, "item_id": piece}, 400, None, [(0, piece, 1)] + mats)
refused("no item named", {"position": 0}, 400, None, [(0, piece, 1)] + mats)
check("  and no login is the 401", client.post("/api/smith/temper", json={"slot": 0, "position": 0,
                                                                          "item_id": piece}).status_code == 401)
bag("smithy", [(0, perfect, 1)] + mats)
q = smith("/api/smith/quote", {"position": 0, "item_id": perfect}).get_json() or {}
check("a Perfect's quote leaves its id as it is, and still prices the work",
      q.get("new_item_id") == perfect and q.get("gold") == gold and q.get("before") == q.get("after"), q)
invariants("refusals")


# =============================================================================
section("S-4  TEMPERED")
# =============================================================================
set_gold("smithy", 5000)
bag("smithy", [(4, piece, 1), (app_module.INVENTORY_CAPACITY, materials_for(2)[0][0], 50)]
    + [(10 + i, m, q + 1) for i, (m, q) in enumerate(materials_for(2))])
held_before = {m: sum(qq for ii, qq in cells("smithy").values() if ii == m) for m, _q in materials_for(2)}
key_stack = cells("smithy")[app_module.INVENTORY_CAPACITY]
burns = ledger("smith_temper")
res = smith("/api/smith/temper", {"position": 4, "item_id": piece})
body = res.get_json() or {}
new_id = body.get("item_id", "")
check("tempered: the new roll, in the same cell, every stat up %d" % STEP,
      res.status_code == 200 and new_id == quote.get("new_item_id") and cells("smithy").get(4) == (new_id, 1)
      and gamedata._parse_variant(new_id)[1] == expected_after and body.get("position") == 4, body)
check("  the resistance unchanged, and the piece a better one: every rolled stat at least what it was",
      gamedata._parse_variant(new_id)[2] == resist
      and all(int(gamedata.item_row(new_id).get(f, 0)) >= int(gamedata.item_row(piece).get(f, 0)) for f in rolls), new_id)
check("  the materials taken, exactly, the bag's before a hotbar key's",
      {m: sum(qq for ii, qq in cells("smithy").values() if ii == m) for m, _q in materials_for(2)}
      == {m: held_before[m] - q for m, q in materials_for(2)}
      and cells("smithy").get(app_module.INVENTORY_CAPACITY) == key_stack
      and body.get("consumed") == [{"item_id": m, "quantity": q} for m, q in materials_for(2)], body.get("consumed"))
check("  the gold burned through the ledger as smith_temper, the purse in the answer",
      ledger("smith_temper") == burns + [-gold] and purse("smithy") == 5000 - gold == body.get("gold"),
      (ledger("smith_temper"), body.get("gold")))
check("  and the smith's status rides along",
      body.get("status", {}).get("temper_step") == STEP and body.get("status", {}).get("gold") == body.get("gold"))
check("  a roll changes what a piece does, not what it is worth: the shop pays the same for it",
      gamedata.vendor_pays("generalstore", new_id) == gamedata.vendor_pays("generalstore", piece)
      and gamedata.item_value(new_id) == gamedata.item_value(piece))
invariants("a temper")

# The cap: one stat a step from it, one at it.
if len(fields) >= 2:
    near = roll_of(chest, [CAP - 1, CAP] + [100] * (len(fields) - 2))
    bag("smithy", [(0, near, 1)] + mats)
    res = smith("/api/smith/temper", {"position": 0, "item_id": near})
    after = gamedata._parse_variant((res.get_json() or {}).get("item_id", ""))[1] or {}
    check("one stat a point under the cap goes to it, never past; one at the cap stays",
          res.status_code == 200 and after.get(fields[0][1]) == CAP and after.get(fields[1][1]) == CAP, res.get_json())

# Tempered again and again: from the bottom of the range to the cap, then no more.
low = roll_of(chest, [gamedata.QUALITY_LOW] * len(fields))
bag("smithy", [(0, low, 1)] + [(10 + i, m, q * 40) for i, (m, q) in enumerate(materials_for(2))])
set_gold("smithy", gold * 40)
current, times = low, 0
while times < 40:
    res = smith("/api/smith/temper", {"position": 0, "item_id": current})
    if res.status_code != 200:
        break
    current, times = res.get_json()["item_id"], times + 1
check("from %d%% to the cap takes %d tempers, then 'That piece is fully tempered.'"
      % (gamedata.QUALITY_LOW, math.ceil((CAP - gamedata.QUALITY_LOW) / float(STEP))),
      times == math.ceil((CAP - gamedata.QUALITY_LOW) / float(STEP))
      and (res.get_json() or {}).get("message") == "That piece is fully tempered."
      and set(gamedata._parse_variant(current)[1].values()) == {CAP}, (times, current))

# The owner's scale on the temper's gold, and its floor of one coin.
bag("smithy", [(0, piece, 1)] + mats)
rules(temper_gold_scale=2.0)
try:
    doubled = smith("/api/smith/quote", {"position": 0, "item_id": piece}).get_json() or {}
    rules(temper_gold_scale=0.0)
    nothing = smith("/api/smith/quote", {"position": 0, "item_id": piece}).get_json() or {}
    set_gold("smithy", 10)
    burns = ledger("smith_temper")
    cheap = smith("/api/smith/temper", {"position": 0, "item_id": piece})
finally:
    economyrules.value = real_value
check("temper_gold_scale 2: twice the gold; 0: still one coin - a temper is never free",
      doubled.get("gold") == max(1, half_up(int(gamedata.ITEMS[chest]["value"]) * RATE * 2)) and nothing.get("gold") == 1
      and cheap.status_code == 200 and ledger("smith_temper") == burns + [-1] and purse("smithy") == 9,
      (doubled.get("gold"), nothing.get("gold"), cheap.get_json()))

# A held find keeps its hold: a fresh mythic tempered by a Royal Smith.
mythic = sorted(i for i, r in gamedata.ITEMS.items() if int(r["tier"]) == gamedata.mythic_tier()
                and r["type_name"] in ("WEAPON", "ARMOR") and gamedata.rolled_fields(r))[0]
fresh = roll_of(mythic, [100] * len(gamedata.rolled_fields(gamedata.ITEMS[mythic])))
give_rank("smithy", len(CERTS))
tier = int(gamedata.ITEMS[mythic]["tier"])
bag("smithy", [(0, fresh, 1)] + [(10 + i, m, q) for i, (m, q) in enumerate(materials_for(tier))])
set_gold("smithy", 100000)
sql("INSERT INTO trade_holds (user_id, item_id, created_at, held_until) VALUES (?, ?, 1, ?)",
    (uid("smithy"), fresh, 2 ** 31 - 1))
res = smith("/api/smith/temper", {"position": 0, "item_id": fresh})
moved = (res.get_json() or {}).get("item_id", "")
holds = [(r["item_id"], r["held_until"]) for r in sql("SELECT item_id, held_until FROM trade_holds WHERE user_id = ?",
                                                       (uid("smithy"),))]
check("a held mythic tempered by a Royal Smith keeps its hold under the new id, its clock as it was",
      res.status_code == 200 and moved != fresh and holds == [(moved, 2 ** 31 - 1)], (res.get_json(), holds))
invariants("tempering")


# =============================================================================
section("S-5  CERTIFIED")
# =============================================================================
give_rank("smithy", 0)
first, second = CERTS[0], CERTS[1]


def certify(rank, headers=None, slot=0):
    return smith("/api/smith/certify", {"rank": rank}, headers=headers, slot=slot)


def cert_bag(cert, short=None):
    bag("smithy", [(i, m["item_id"], int(m["quantity"]) - (1 if m["item_id"] == short else 0))
                   for i, m in enumerate(cert["materials"]) if int(m["quantity"]) - (1 if m["item_id"] == short else 0) > 0])


def refused_cert(label, rank, code, message):
    cells_before, purse_before = cells("smithy"), purse("smithy")
    burns = len(ledger("royal_certification"))
    res = certify(rank)
    got = res.get_json() or {}
    check("%s: %d %r, nothing taken" % (label, code, message),
          res.status_code == code and (message is None or got.get("message") == message)
          and cells("smithy") == cells_before and purse("smithy") == purse_before
          and len(ledger("royal_certification")) == burns, (res.status_code, got))


cert_bag(first)
set_gold("smithy", first["gold"])
set_level("smithy", first["level"] - 1)
refused_cert("the second rank before the first", second["rank"], 403, "Earn the %s first." % first["name"])
refused_cert("a rank there is not", 99, 400, None)
refused_cert("no rank at all", "first", 400, None)
refused_cert("below the rank's level", first["rank"], 403, "You need level %d." % first["level"])
set_level("smithy", first["level"])
short = first["materials"][0]["item_id"]
cert_bag(first, short=short)
refused_cert("a %s short" % short, first["rank"], 404, "You are missing %s." % name_of(short))
cert_bag(first)
set_gold("smithy", first["gold"] - 1)
refused_cert("a light purse", first["rank"], 400,
             "That costs %d gold and you have %d." % (first["gold"], first["gold"] - 1))
set_gold("smithy", first["gold"] + 7)
burns = ledger("royal_certification")
res = certify(float(first["rank"]))
body = res.get_json() or {}
check("the %s earned: the rank, its name, the gold burned as royal_certification, every material taken"
      % first["name"],
      res.status_code == 200 and body.get("rank") == first["rank"] and body.get("rank_name") == first["name"]
      and ledger("royal_certification") == burns + [-first["gold"]] and purse("smithy") == 7 == body.get("gold")
      and cells("smithy") == {} and body["ranks"][0]["earned"] is True and body["ranks"][1]["earned"] is False
      and body.get("consumed") == [{"item_id": m["item_id"], "quantity": m["quantity"]} for m in first["materials"]],
      body)
check("  written down per character, and the status says so",
      [int(r["rank"]) for r in sql("SELECT rank FROM smith_ranks WHERE user_id = ? AND slot = 0", (uid("smithy"),))]
      == [first["rank"]]
      and (client.get("/api/smith/status?slot=0", headers=smith_t).get_json() or {}).get("rank") == first["rank"]
      and (client.get("/api/smith/status?slot=1", headers=smith_t).get_json() or {}).get("rank") == 0)
refused_cert("the same rank again", first["rank"], 409, "You are already a %s." % first["name"])
bag("smithy", [(0, three_piece, 1)] + [(10 + i, m, q) for i, (m, q) in enumerate(materials_for(3))])
set_gold("smithy", 100000)
res = smith("/api/smith/temper", {"position": 0, "item_id": three_piece})
check("  and now tier 3 is the smith's to temper", res.status_code == 200, res.get_json())

set_level("smithy", second["level"])
cert_bag(second)
set_gold("smithy", 50)
burns = ledger("royal_certification")
rules(certification_gold_scale=0.0)
try:
    res = certify(second["rank"])
finally:
    economyrules.value = real_value
check("certification_gold_scale 0: the %s for its materials alone - no gold, no ledger row" % second["name"],
      res.status_code == 200 and (res.get_json() or {}).get("rank") == second["rank"]
      and ledger("royal_certification") == burns and purse("smithy") == 50, res.get_json())
third = CERTS[2]
set_level("smithy", third["level"])
cert_bag(third)
set_gold("smithy", third["gold"] * 2)
rules(certification_gold_scale=2.0)
try:
    res = certify(third["rank"])
finally:
    economyrules.value = real_value
check("  and at 2, twice the gold", res.status_code == 200 and ledger("royal_certification")[-1] == -third["gold"] * 2
      and purse("smithy") == 0, res.get_json())
check("no login is the 401", client.post("/api/smith/certify", json={"slot": 0, "rank": 1}).status_code == 401)

give_rank("smithy", 2, slot=1)
res = client.post("/api/character/delete", headers=smith_t, json={"slot": 1, "confirm": "mage"})
check("a deleted character takes its certifications with it - the next in the slot starts at none",
      res.status_code == 200 and sql("SELECT * FROM smith_ranks WHERE user_id = ? AND slot = 1", (uid("smithy"),)) == [])
invariants("certifications")


print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
