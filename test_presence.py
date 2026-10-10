"""
Players seeing each other: the presence socket. Run: python3 test_presence.py

WHAT IT IS. presence.py is a WebSocket process beside the API. A game with a
login asks the API for a ticket (POST /api/presence/ticket), hands it to the
socket, then says where it stands and how it is moving; everyone in the same
area hears it, about ten times a second. Who a player IS on screen - name, rank,
colour, guild, class, level, the pets they may show - comes from the ticket,
which the API writes from its own rows. presence.py's own header has the wire.

WHAT THIS GUARDS. The door (no ticket, no socket), the rooms (an area hears
only itself), what a game may claim (where it stands, never who it is), the
limits (size, rate, one connection per account), and that a login ended
anywhere - a logout, a ban, a login from another computer, a ticket left to run
out - takes the player off everybody's screen within seconds.

It runs the real presence server in this process, on a free port, against a
throwaway database in the temp folder - never elusion.db.
"""
import asyncio, importlib.util, json, os, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_presence_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "presowner"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()

import presence  # noqa: E402  (after ELUSION_DB is set: it reads it on import)
import combatbook  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

passed = failed = 0
failures = []


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n=== %s ===\n" % title)


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def uid(name):
    return int(sql("SELECT id FROM users WHERE username = ?", (name,))[0]["id"])


def login(name):
    token = client.post("/api/auth/login", json={"username": name, "password": "password123"}
                        ).get_json()["token"]
    return {"Authorization": "Bearer " + token}


def account(name, class_id="warrior"):
    client.post("/api/auth/register", json={"username": name, "password": "password123"})
    headers = login(name)
    client.put("/api/save", headers=headers, json={"slot": 0, "class_id": class_id, "name": name.capitalize()})
    return headers


def ticket(headers, slot=0):
    res = client.post("/api/presence/ticket", headers=headers, json={"slot": slot})
    return (res.get_json() or {}).get("ticket", ""), res


def state(area="field", x=100.0, y=200.0, m="idledown", fx=None, pet=""):
    return json.dumps({"t": "s", "a": area, "x": x, "y": y, "m": m, "fx": fx or [], "pet": pet})


async def collect(ws, seconds=0.35):
    """Every message that arrives within `seconds`, parsed."""
    got = []
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return got
        try:
            raw = await asyncio.wait_for(ws.recv(), left)
        except asyncio.TimeoutError:
            return got
        except ConnectionClosed:
            got.append({"t": "_closed"})
            return got
        got.append(json.loads(raw))


def joined(messages):
    """{id: entry} from every join in these messages."""
    out = {}
    for m in messages:
        if m.get("t") == "join":
            for e in m["p"]:
                out[e["id"]] = e
    return out


def moved(messages):
    out = {}
    for m in messages:
        if m.get("t") == "moves":
            for e in m["p"]:
                out[e[0]] = e
    return out


def left(messages):
    return [i for m in messages if m.get("t") == "leave" for i in m["ids"]]


def byes(messages):
    return [m.get("why") for m in messages if m.get("t") == "bye"]


def of(messages, kind):
    return [m for m in messages if m.get("t") == kind]


def last_lead(messages):
    leads = of(messages, "lead")
    return leads[-1] if leads else None


async def enter(port, headers, area=None, v=None, **kw):
    """A connected game: hello with a fresh ticket, welcome read, optionally
    standing somewhere. v=2 is a game that shares monsters."""
    raw, _ = ticket(headers)
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port, max_size=presence.MAX_SOCKET_BYTES)
    hello = {"t": "hello", "ticket": raw}
    if v is not None:
        hello["v"] = v
    await ws.send(json.dumps(hello))
    welcome = json.loads(await asyncio.wait_for(ws.recv(), 2))
    if area:
        await ws.send(state(area, **kw))
    return ws, welcome


async def run(port, server):
    ALICE = account("presa")
    BOB = account("presb", "mage")
    CAROL = account("presc", "tank")

    # =========================================================================
    section("P-1 THE DOOR")
    # =========================================================================
    res = client.post("/api/presence/ticket", json={"slot": 0})
    check("a ticket needs a login (401)", res.status_code == 401, res.status_code)
    _, res = ticket(ALICE, slot=2)
    check("and a character in that slot (404)", res.status_code == 404, res.status_code)
    raw, res = ticket(ALICE)
    body = res.get_json() or {}
    check("a ticket is a random string, two minutes, with the socket's address",
          res.status_code == 200 and len(raw) >= 40 and body.get("expires_in") == 120
          and str(body.get("socket_url", "")).endswith("/ws/presence"), body)
    rows = sql("SELECT * FROM presence_tickets WHERE user_id = ?", (uid("presa"),))
    check("  stored as its hash, never the ticket", rows and all(r["token_hash"] != raw for r in rows)
          and any(r["token_hash"] == presence._hash(raw) for r in rows))
    ident = json.loads(rows[-1]["identity"])
    check("  with who the player is, read from the server's own rows",
          ident.get("name") == "presa" and ident.get("cls") == "warrior" and ident.get("role") == "player"
          and ident.get("pets") == [], ident)

    ws = await connect("ws://127.0.0.1:%d/somewhere/else" % port)
    got = await collect(ws, 0.5)
    check("a socket on any other path is closed", got and got[-1]["t"] == "_closed"
          and ws.close_code == 1008, (got, ws.close_code))
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port)
    got = await collect(ws, presence.HELLO_SECONDS + 0.4)
    check("one that never says hello is closed", got and got[-1]["t"] == "_closed", got)
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port)
    await ws.send(json.dumps({"t": "hello", "ticket": "made-up"}))
    got = await collect(ws, 0.5)
    check("a ticket the API never wrote is turned away", byes(got) == ["ticket"]
          and got[-1]["t"] == "_closed", got)
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port)
    await ws.send(state())
    got = await collect(ws, 0.5)
    check("so is a game that starts talking before hello", got and got[-1]["t"] == "_closed", got)

    a, welcome = await enter(port, ALICE)
    check("a real ticket is welcomed, by account id",
          welcome == {"t": "welcome", "id": uid("presa"), "v": presence.SHARED_VERSION,
                      "books": presence.BOOKS, "x": presence.RELAY_VERSION}, welcome)
    await a.close()

    # =========================================================================
    section("P-2 AN AREA SEES ITSELF")
    # =========================================================================
    a, _ = await enter(port, ALICE, "field", x=100.0, y=200.0, m="walkdown")
    b, _ = await enter(port, BOB, "field", x=150.0, y=210.0)
    got_b = await collect(b)
    got_a = await collect(a)
    seen = joined(got_b).get(uid("presa"))
    check("arriving in an area, you are told who is already there",
          seen is not None and seen["x"] == 100.0 and seen["y"] == 200.0 and seen["m"] == "walkdown", got_b)
    check("  by their name, class, level, rank and guild, from the server",
          seen and seen["name"] == "presa" and seen["cls"] == "warrior" and seen["lvl"] == 1
          and seen["role"] == "player" and seen["guild"] == "", seen)
    check("and they are told you arrived", uid("presb") in joined(got_a)
          and joined(got_a)[uid("presb")]["cls"] == "mage", got_a)

    await a.send(state("field", x=120.5, y=205.25, m="walkright"))
    got_b = await collect(b)
    move = moved(got_b).get(uid("presa"))
    check("a step is heard by everyone in the area, about ten times a second",
          move == [uid("presa"), 120.5, 205.2, "walkright", [], "", -1], got_b)
    await a.send(state("field", x=120.5, y=205.2, m="attackright"))
    await a.send(state("field", x=121.0, y=205.2, m="attackright"))
    got_b = await collect(b)
    batched = [m for m in got_b if m.get("t") == "moves"]
    steps = [e for m in batched for e in m["p"] if e[0] == uid("presa")]
    check("  two steps inside one tick are one message, both of them, in order",
          len(batched) == 1 and [e[1] for e in steps] == [120.5, 121.0], got_b)

    c, _ = await enter(port, CAROL, "elusion", x=10.0, y=10.0)
    got_c = await collect(c)
    got_a = await collect(a)
    check("another area hears nothing of this one", not joined(got_c) and not moved(got_c), got_c)
    check("  and this one nothing of it", uid("presc") not in joined(got_a), got_a)

    await a.send(state("elusion", x=12.0, y=12.0))
    got_b, got_c, got_a = await collect(b), await collect(c), await collect(a)
    check("walking to another area leaves the old one", left(got_b) == [uid("presa")], got_b)
    check("  and joins the new", uid("presa") in joined(got_c), got_c)
    check("  where you are told who is there", uid("presc") in joined(got_a), got_a)
    await a.send(state("field", x=100.0, y=200.0))
    await collect(a), await collect(b), await collect(c)

    # THE SAME AREA, A NEW SCENE: a revive in town reloads it and every body in
    # it is gone, while the server still has you in the same room.
    await a.send(json.dumps({"t": "sync"}))
    got_a = await collect(a)
    check("asking again (sync) is told everyone in the area again, not yourself",
          set(joined(got_a)) == {uid("presb")}, got_a)

    # =========================================================================
    section("P-3 A GAME SAYS WHERE IT IS, NEVER WHO")
    # =========================================================================
    await a.send(json.dumps({"t": "s", "a": "elusion", "x": 1.0, "y": 1.0, "m": "idledown",
                             "name": "TheOwner", "role": "owner", "lvl": 99, "cls": "boss"}))
    entry = joined(await collect(c)).get(uid("presa"))
    check("a name, rank, level or class in the state is not what anyone sees",
          entry and entry["name"] == "presa" and entry["role"] == "player" and entry["lvl"] == 1
          and entry["cls"] == "warrior", entry)
    await a.send(state("field", x=100.0, y=200.0))
    await collect(a), await collect(b), await collect(c)

    bad = [
        '{"t": "s", "a": "field", "x": NaN, "y": 1, "m": "idledown"}',
        state(x=1e9), state(x=True), state(m="dance"), state(m="idledownright"), state(fx=["laser"]),
        state(fx=["ring", "ring", "ring"]), state(area="Field"), state(area="../db"), state(area="x" * 65),
        json.dumps({"t": "s", "a": "field", "x": "100", "y": 1, "m": "idledown"}),
        "not json", "[1, 2, 3]",
    ]
    for raw_bad in bad:
        await a.send(raw_bad)
    got_b = await collect(b)
    check("none of %d malformed states reaches anyone else" % len(bad),
          not moved(got_b) and not joined(got_b) and not left(got_b), got_b)
    await a.send(state("field", x=101.0, y=200.0, m="walkup", fx=["ring", "firering"]))
    got_b = await collect(b)
    check("  and the socket is still open for the good one after them",
          moved(got_b).get(uid("presa")) == [uid("presa"), 101.0, 200.0, "walkup", ["firering", "ring"], "", -1],
          got_b)

    # A pet is shown only when the account holds it.
    sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, 0, 'petsniper', 1)",
        (uid("presa"),))
    await a.close()
    a, _ = await enter(port, ALICE, "field", x=100.0, y=200.0, pet="petsniper")
    entry = joined(await collect(b)).get(uid("presa"))
    check("a pet the account holds is shown", entry and entry["pet"] == "petsniper", entry)
    await a.send(state("field", x=102.0, y=200.0, pet="petboss"))
    move = moved(await collect(b)).get(uid("presa"))
    check("  one it does not hold is not", move and move[5] == "", move)

    # =========================================================================
    section("P-4 LIMITS")
    # =========================================================================
    d, _ = await enter(port, CAROL, "field")
    got_c = await collect(c)
    check("a second socket for one account replaces the first: the old one is told and closed",
          "replaced" in byes(got_c) and got_c[-1]["t"] == "_closed", got_c)
    check("  and the account is drawn once, from the new one",
          server.players.get(uid("presc")) is not None and server.players[uid("presc")].ws.remote_address
          == d.local_address, None)
    for _ in range(int(presence.BURST) + 20):
        await d.send(state("field", x=1.0, y=1.0))
    got = await collect(d, 1.0)
    check("a game sending far faster than any game does is cut off", "too fast" in byes(got)
          and got[-1]["t"] == "_closed", [m.get("t") for m in got][-5:])
    got_b = await collect(b)
    check("  and leaves everyone's screen", uid("presc") in left(got_b), got_b)

    d, _ = await enter(port, CAROL, "field")
    await collect(b)
    try:
        await d.send(json.dumps({"t": "s", "a": "field", "x": 1, "y": 1, "m": "idledown", "pad": "x" * 4000}))
    except ConnectionClosed:
        pass
    got = await collect(d, 0.5)
    check("a message bigger than any state closes the socket", got and got[-1]["t"] == "_closed"
          and d.close_code == 1009, (got, d.close_code))
    await collect(b)

    # =========================================================================
    section("P-5 A LOGIN ENDED ANYWHERE ENDS THE SOCKET")
    # =========================================================================
    c, _ = await enter(port, CAROL, "field")
    await collect(b)
    login("presc")   # one login at a time: this ends the session the ticket came from
    got = await collect(c, server.sweep_seconds + 0.6)
    check("a login from somewhere else drops the socket within a sweep", "signed out" in byes(got), got)
    check("  and everyone in the area is told", uid("presc") in left(await collect(b)))

    CAROL = login("presc")
    c, _ = await enter(port, CAROL, "field")
    await collect(b)
    client.post("/api/auth/logout", headers=CAROL)
    got = await collect(c, server.sweep_seconds + 0.6)
    check("logging out drops it", "signed out" in byes(got), got)
    await collect(b)

    CAROL = login("presc")
    c, _ = await enter(port, CAROL, "field")
    await collect(b)
    OWNER = account("presowner")
    res = client.post("/api/staff/ban", headers=OWNER, json={"username": "presc", "reason": "test", "days": 1})
    got = await collect(c, server.sweep_seconds + 0.6)
    check("a ban drops it (the ban ends the session)", res.status_code == 200 and "signed out" in byes(got),
          [res.status_code, got])
    client.post("/api/staff/unban", headers=OWNER, json={"username": "presc"})
    await collect(b)

    # A ticket left to run out.
    sql("UPDATE presence_tickets SET expires_at = ? WHERE user_id = ?", (int(time.time()) - 1, uid("presb")))
    got = await collect(b, server.sweep_seconds + 0.6)
    check("a ticket that runs out without a renewal drops the socket", "signed out" in byes(got), got)
    await collect(a)

    # Renewal keeps it, and carries a change of identity.
    BOB = login("presb")
    b, _ = await enter(port, BOB, "field")
    await collect(a)
    client.put("/api/account/name-colour", headers=BOB, json={"hue": 200})
    fresh, _ = ticket(BOB)
    sql("UPDATE presence_tickets SET expires_at = ? WHERE user_id = ? AND token_hash != ?",
        (int(time.time()) - 1, uid("presb"), presence._hash(fresh)))
    await b.send(json.dumps({"t": "renew", "ticket": fresh}))
    got_a = await collect(a)
    got_b = await collect(b, server.sweep_seconds + 0.6)
    check("a renewed ticket keeps the socket past the old one's end", not byes(got_b), got_b)
    check("  and a changed colour reaches everyone in the area",
          joined(got_a).get(uid("presb"), {}).get("hue") == 200, got_a)
    other, _ = ticket(ALICE)
    await b.send(json.dumps({"t": "renew", "ticket": other}))
    await collect(b)
    check("somebody else's ticket is not a renewal", server.players[uid("presb")].identity["name"] == "presb")


    # =========================================================================
    section("P-7 SHARED MONSTERS: ONE GAME RUNS THEM, THE SERVER PASSES THE NOTES")
    # =========================================================================
    # 0.7.0. Everyone in an area fights the same monsters: the game that has
    # been there longest (the leader) runs them and says what they do; the
    # others draw them and send their hits to the leader. This file only
    # decides who leads and who hears what.
    DAVE = account("presd", "healer")
    ERIN = account("prese", "mage")
    FRAN = account("presf")
    GUS = account("presg")

    d, welcome = await enter(port, DAVE, v=2)
    check("the server says it shares monsters", welcome.get("v") == presence.SHARED_VERSION, welcome)
    await d.send(state("crypt", x=10.0, y=10.0))
    got_d = await collect(d)
    lead = last_lead(got_d)
    check("the first game into an area leads it, alone",
          lead == {"t": "lead", "a": "crypt", "id": uid("presd"), "n": 0}, got_d)

    e, _ = await enter(port, ERIN, "crypt", v=2)
    got_d = await collect(d)
    got_e = await collect(e)
    check("the second one in is told who leads", last_lead(got_e) == {
        "t": "lead", "a": "crypt", "id": uid("presd"), "n": 1}, got_e)
    check("  and so is the leader, with one game sharing",
          (last_lead(got_d) or {}).get("n") == 1, got_d)
    check("  and the leader is asked to send it everything",
          of(got_d, "need") == [{"t": "need", "id": uid("prese")}], got_d)
    check("  but the newcomer is not asked to send anything", not of(got_e, "need"), got_e)

    world = {"t": "w", "d": {"snap": [[1, 50.0, 60.0, "walkleft", 90]], "ev": []}}
    await d.send(json.dumps(world))
    got_e = await collect(e)
    check("the leader's world reaches the other game exactly as sent", of(got_e, "w") == [world], got_e)
    check("  and not back to the leader", not of(await collect(d), "w"))

    await e.send(json.dumps({"t": "w", "d": {"snap": [[1, 0.0, 0.0, "idledown", 1]]}}))
    check("a world message from a game that is not the leader goes nowhere",
          not of(await collect(d), "w"))

    f, _ = await enter(port, FRAN, "crypt", v=2)
    await collect(d)
    await collect(e)
    await collect(f)
    await d.send(json.dumps({"t": "w", "to": uid("presf"), "d": {"full": True}}))
    check("a world message for one game reaches that game", of(await collect(f), "w"), None)
    check("  and only that game", not of(await collect(e), "w"))
    await d.send(json.dumps({"t": "w", "to": uid("presa"), "d": {"full": True}}))
    check("  one for a game in another area reaches nobody",
          not of(await collect(e), "w") and not of(await collect(f), "w"))
    await d.send(json.dumps({"t": "w", "d": ["not", "a", "dict"]}))
    check("a world message that is not an object is not passed on", not of(await collect(e), "w"))

    await e.send(json.dumps({"t": "h", "p": [[1, 30, 0], [4, 12, 3]]}))
    got_d = await collect(d)
    check("a follower's hits reach the leader, with who sent them",
          of(got_d, "h") == [{"t": "h", "from": uid("prese"), "p": [[1, 30, 0], [4, 12, 3]]}], got_d)
    check("  and nobody else", not of(await collect(f), "h"))
    await d.send(json.dumps({"t": "h", "p": [[1, 30, 0]]}))
    check("the leader's own 'hits' go nowhere - its game applies them itself",
          not of(await collect(e), "h") and not of(await collect(f), "h"))
    for bad in ([[1, "30", 0]], [[1, 30]], [[-1, 30, 0]], [[1, 0, 0]], [[1, presence.MAX_HIT + 1, 0]],
                [[1, True, 0]], [[1, 30, 0]] * (presence.MAX_HITS_PER_MESSAGE + 1), [], "hits",
                [[1, 30.5, 0]]):
        await e.send(json.dumps({"t": "h", "p": bad}))
    check("hits that are not whole numbers in range are not passed on",
          not of(await collect(d), "h"))

    g, _ = await enter(port, GUS, "crypt")
    got_g = await collect(g)
    got_d = await collect(d)
    check("a game from before shared monsters is never told who leads",
          not of(got_g, "lead") and not of(got_g, "need"), got_g)
    check("  nor is the leader asked to send it anything", not of(got_d, "need"), got_d)
    check("  and everyone can see it does not share them",
          joined(got_d).get(uid("presg"), {}).get("v") == 0, got_d)
    await d.send(json.dumps(world))
    check("  so it hears no monsters", not of(await collect(g), "w"))
    await g.close()
    await collect(d)
    await collect(e)
    await collect(f)

    await f.send(json.dumps({"t": "sync"}))
    got_d = await collect(d)
    got_f = await collect(f)
    check("a game that rebuilt its world is told again who leads",
          (last_lead(got_f) or {}).get("id") == uid("presd"), got_f)
    check("  and the leader is asked to send it everything again",
          of(got_d, "need") == [{"t": "need", "id": uid("presf")}], got_d)

    await d.close()
    got_e = await collect(e)
    got_f = await collect(f)
    check("when the leader goes, the game that came in next takes over",
          last_lead(got_e) == {"t": "lead", "a": "crypt", "id": uid("prese"), "n": 1}, got_e)
    check("  and everyone left is told", (last_lead(got_f) or {}).get("id") == uid("prese"), got_f)
    check("  and the server agrees", server.leaders.get("crypt") is server.players.get(uid("prese")))
    await e.send(json.dumps(world))
    check("the new leader's world reaches the others", of(await collect(f), "w") == [world])

    await e.send(state("dungeon", x=1.0, y=1.0))
    got_e = await collect(e)
    got_f = await collect(f)
    check("a leader walking into another area hands this one on",
          (last_lead(got_f) or {}).get("id") == uid("presf"), got_f)
    check("  and leads the empty one it walked into",
          (last_lead(got_e) or {}) == {"t": "lead", "a": "dungeon", "id": uid("prese"), "n": 0}, got_e)
    await e.send(json.dumps(world))
    check("  whose monsters nobody in the old area hears", not of(await collect(f), "w"))

    big = {"t": "w", "d": {"spawn": [{"id": n, "s": "res://scene/enemy/" + "x" * 60 + ".tscn"}
                                     for n in range(120)]}}
    await f.send(json.dumps(big))
    got_f = await collect(f, 0.3)
    check("a leader's world message far bigger than a state is not refused",
          not byes(got_f) and not of(got_f, "_closed"), got_f[-2:])
    await f.send(json.dumps({"t": "h", "p": [[1, 1, 0]], "pad": "x" * 4000}))
    got_f = await collect(f, 0.5)
    check("  but anything else that big still closes the socket",
          got_f and got_f[-1]["t"] == "_closed" and f.close_code == 1009, (got_f[-2:], f.close_code))
    await e.close()
    await asyncio.sleep(0.1)
    check("an empty area has no leader", not server.leaders, server.leaders)

    # =========================================================================
    section("P-8 THE BOOKS: THE SERVER COUNTS EVERY MONSTER'S HEALTH, AND CHANGES NOTHING")
    # =========================================================================
    # 0.10.0, E3_SCOPE.md option C step 1. Every leader's world and every
    # follower's hit is also read into combatbook.py's books; what they find
    # is written to combat_kills and combat_flags. The relay is untouched.
    check("the books are on with this catalogue", presence.BOOKS)
    HAL = account("presh")
    IVY = account("presi", "mage")
    raw, _ = ticket(HAL)
    ident = json.loads(sql("SELECT identity FROM presence_tickets WHERE token_hash = ?",
                           (presence._hash(raw),))[0]["identity"])
    check("a ticket carries what the books hold a character to: its gear and fighting skills",
          ident.get("gear") == [] and isinstance(ident.get("skills"), dict), ident)
    sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, 0, 'ironsword', 1)",
        (uid("presh"),))
    sql("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'attack', 7, 0)",
        (uid("presh"),))
    raw, _ = ticket(HAL)
    ident = json.loads(sql("SELECT identity FROM presence_tickets WHERE token_hash = ?",
                           (presence._hash(raw),))[0]["identity"])
    check("  gear carried counts as well as gear worn, and the skills are the character's",
          ident.get("gear") == ["ironsword"] and ident["skills"].get("attack") == 7, ident)

    spots = [s for s in presence.gamedata.AREAS["bigfield"]["spawns"] if s["e"] == "firesprite"][:3]
    fire = presence.gamedata.ENEMIES["firesprite"]

    def rec(i, spot, hp=None):
        return {"id": i, "o": spot["o"], "s": "res://scene/enemy/firesprite.tscn", "pp": "ysortworld",
                "x": spot["x"], "y": spot["y"], "a": "idledown", "hp": hp or fire["max_hp"],
                "mh": fire["max_hp"], "p": {"ed": fire["resource"], "eo": -1, "lr": 250.0, "g": False}}

    h, welcome = await enter(port, HAL, "bigfield", v=presence.BOOKS_VERSION, x=spots[0]["x"] + 20, y=spots[0]["y"])
    check("the welcome says this server keeps books", welcome.get("books") is True, welcome)
    await collect(h)
    full = {"t": "w", "d": {"full": True, "reset": True, "part": 0, "parts": 1, "wave": -1,
                            "spawn": [rec(1, spots[0]), rec(2, spots[1])]}}
    await h.send(json.dumps(full))
    await asyncio.sleep(0.1)
    book = server.books.get("bigfield")
    check("a leader alone still has its world read into the books",
          book is not None and set(book.monsters) == {1, 2}, book and book.monsters)

    i, _ = await enter(port, IVY, "bigfield", v=presence.BOOKS_VERSION, x=spots[0]["x"] - 20, y=spots[0]["y"])
    got_h = await collect(h)
    seen = joined(got_h).get(uid("presi"), {})
    check("what the books hold a player to is never shown to anyone", seen and "gear" not in seen
          and "skills" not in seen, seen)
    await collect(i)
    ivy_bounds = server.players[uid("presi")].fighter.bounds
    hal_bounds = server.players[uid("presh")].fighter.bounds
    check("  each player's bounds come from their own ticket",
          hal_bounds["max_hit"] > ivy_bounds["max_hit"] > 0, (hal_bounds, ivy_bounds))
    half = fire["max_hp"] // 2
    left_hp = fire["max_hp"] - half
    sent = 0
    while sent < half:
        n = min(ivy_bounds["max_hit"], half - sent)
        await i.send(json.dumps({"t": "h", "p": [[1, n, 0]]}))
        sent += n
        await asyncio.sleep(n / ivy_bounds["dps"])
    got_h = await collect(h, 0.2)
    check("a follower's hits still reach the leader, exactly as before",
          sum(p[1] for m in of(got_h, "h") for p in m["p"]) == half, of(got_h, "h")[-1:])
    own = []
    while sum(own) < left_hp:
        own.append(min(hal_bounds["max_hit"], left_hp - sum(own)))
    world = {"t": "w", "d": {"hits": [[1, n, 0] for n in own],
                             "ev": [{"k": "die", "id": 1, "x": spots[0]["x"], "y": spots[0]["y"]}]}}
    await h.send(json.dumps(world))
    got_i = await collect(i)
    check("a world carrying the leader's own hits reaches the others exactly as sent",
          of(got_i, "w") == [world], got_i)
    await h.send(json.dumps({"t": "w", "d": {"ev": [{"k": "die", "id": 2, "x": 0, "y": 0}]}}))
    await asyncio.sleep(combatbook.HOLD_SECONDS + server.flush_seconds + 0.4)
    rows = sql("SELECT * FROM combat_kills WHERE area = 'bigfield' ORDER BY id")
    first = {r["user_id"]: r for r in rows if r["origin"] == spots[0]["o"]}
    check("a monster both players killed is AGREED, one row each with their own damage",
          set(first) == {uid("presh"), uid("presi")}
          and all(r["verdict"] == "agreed" for r in first.values())
          and first[uid("presi")]["damage"] == half and first[uid("presh")]["damage"] == left_hp
          and first[uid("presi")]["slot"] == 0 and first[uid("presh")]["leader_id"] == uid("presh"),
          [dict(r) for r in rows])
    second = [r for r in rows if r["origin"] == spots[1]["o"]]
    check("one the leader says died with nobody hitting it is SHORT, on the leader",
          len(second) == 1 and second[0]["verdict"] == "short" and second[0]["user_id"] == uid("presh")
          and second[0]["hp_left"] == fire["max_hp"], [dict(r) for r in second])

    await h.send(json.dumps({"t": "w", "d": {"ev": [{"k": "spawn", "r": rec(3, spots[2])}]}}))
    await asyncio.sleep(0.1)
    await i.send(json.dumps({"t": "h", "p": [[3, ivy_bounds["max_hit"] * 5, 0]]}))
    await asyncio.sleep(combatbook.PENDING_SECONDS + server.flush_seconds + 0.4)
    flags = sql("SELECT * FROM combat_flags WHERE user_id = ?", (uid("presi"),))
    check("a hit bigger than the character could land is written down, with the numbers",
          [f["kind"] for f in flags] == ["hit_too_big"] and str(ivy_bounds["max_hit"] * 5) in flags[0]["detail"],
          [dict(f) for f in flags])
    await collect(h)
    await collect(i)

    await h.send(json.dumps({"t": "w", "d": {"snap": [[999, 1.0, 1.0, "idledown", 5]]}}))
    got_h = await collect(h)
    check("a leader that talks of a monster the books never saw is asked for everything, for game 0",
          {"t": "need", "id": 0} in of(got_h, "need"), got_h)
    await collect(i)
    await h.send(json.dumps({"t": "w", "to": 0, "d": {"full": True, "reset": False, "part": 0, "parts": 1,
                                                       "spawn": [dict(rec(999, spots[1]), hp=40)]}}))
    await asyncio.sleep(0.1)
    check("  and its answer, to nobody, is read by the books alone",
          999 in book.monsters and book.monsters[999].hp == 40 and not of(await collect(i), "w"),
          (sorted(book.monsters), book.monsters.get(999) and book.monsters[999].hp))

    await i.close()
    await asyncio.sleep(0.1)
    i, _ = await enter(port, IVY, "bigfield", v=2, x=spots[0]["x"] - 20, y=spots[0]["y"])
    await asyncio.sleep(0.1)
    await collect(h)
    await h.close()
    got_i = await collect(i)
    check("a game from before the books still leads an area it is alone in",
          (last_lead(got_i) or {}).get("id") == uid("presi"), got_i)
    await i.send(json.dumps({"t": "w", "d": {"full": True, "reset": True, "part": 0, "parts": 1,
                                             "spawn": [rec(1, spots[0])]}}))
    await i.send(json.dumps({"t": "w", "d": {"ev": [{"k": "die", "id": 1, "x": 0, "y": 0}]}}))
    await asyncio.sleep(combatbook.HOLD_SECONDS + server.flush_seconds + 0.4)
    check("  but nothing it kills is judged - it never sends its own hits - so no row, not a false SHORT",
          len(sql("SELECT * FROM combat_kills WHERE area = 'bigfield'")) == len(rows) + 0,
          [dict(r) for r in sql("SELECT * FROM combat_kills WHERE area = 'bigfield'")])
    await i.close()
    await asyncio.sleep(0.1)
    h, _ = await enter(port, HAL, "bigfield", v=presence.BOOKS_VERSION, x=spots[0]["x"] + 20, y=spots[0]["y"])
    i, _ = await enter(port, IVY, "bigfield", v=presence.BOOKS_VERSION, x=spots[0]["x"] - 20, y=spots[0]["y"])
    await collect(h)
    await collect(i)
    book = server.books.get("bigfield")

    odd = {"t": "w", "d": {"ev": "x", "snap": 5, "full": True, "spawn": [1, None], "hits": "lots"}}
    await h.send(json.dumps(odd))
    check("a world the books cannot read is still passed on exactly as sent",
          of(await collect(i), "w") == [odd])
    errors = server._book_errors
    book.world = lambda *args: 1 / 0
    await h.send(json.dumps(odd))
    await i.send(json.dumps({"t": "h", "p": [[1, 5, 0]]}))
    got_i = await collect(i)
    got_h = await collect(h)
    del book.world
    check("  and a book that breaks outright breaks nothing else: the world and the hits still go through",
          of(got_i, "w") == [odd] and of(got_h, "h") and server._book_errors == errors + 1,
          (got_i, got_h, server._book_errors))

    presence.BOOKS = False
    try:
        j, welcome = await enter(port, login("presa"), v=2)
        check("ELUSION_BOOKS=off: the welcome says so", welcome.get("books") is False, welcome)
        check("  and the player has nothing to be judged by", server.players[uid("presa")].fighter is None)
        await j.close()
    finally:
        presence.BOOKS = True
    ALICE = login("presa")
    a, _ = await enter(port, ALICE, "field", x=100.0, y=200.0)
    await collect(b)

    bare = os.path.join(tempfile.gettempdir(), "elusion_presence_bare.db")
    if os.path.exists(bare):
        os.remove(bare)
    sqlite3.connect(bare).close()
    keep = presence.DB_PATH
    presence.DB_PATH = bare
    try:
        presence.write_books([dict(rows[0])], [(1, "field", "too_fast", 1, "x", 1, 1)], prune_before=0)
        quiet = True
    except Exception as exc:  # noqa: BLE001
        quiet = repr(exc)
    finally:
        presence.DB_PATH = keep
    check("a database from before the books' tables is not an error", quiet is True, quiet)
    await h.send(json.dumps({"t": "w", "d": {"ev": [{"k": "spawn", "r": rec(5, spots[2])}]}}))
    await asyncio.sleep(0.1)
    await h.close()
    await i.close()
    await asyncio.sleep(0.1)
    check("an emptied area's books keep its living monsters a while, for a link coming back",
          book.monsters and book.empty_since is not None, book.monsters)
    await asyncio.sleep(combatbook.EMPTY_KEEP + 0.2)
    check("  then forget them: the next game in loads the scene afresh", not book.monsters, book.monsters)

    # =========================================================================
    section("P-9 ATTACKS AND LEVERS: A PICTURE OF EVERY ATTACK, AND ONE GATE FOR EVERYONE")
    # =========================================================================
    # Game 0.19.0. The owner: "i could not see their attacks but they could see
    # mine the had to lower the gate to boss". In an area of its own, "town".
    HAL = account("presh", "mage")
    IVY = account("presi", "tank")
    JON = account("presj")
    KIT = account("presk", "healer")
    h, welcome = await enter(port, HAL, "town", x=500.0, y=500.0)
    check("the welcome says attacks and levers are passed on", welcome.get("x") == presence.RELAY_VERSION, welcome)
    i, _ = await enter(port, IVY, "town", x=520.0, y=500.0)
    j, _ = await enter(port, JON, "elusion", x=10.0, y=10.0)
    await collect(h), await collect(i), await collect(j)

    # ---- the game's clock on every step ----
    await h.send(json.dumps({"t": "s", "a": "town", "x": 501.0, "y": 500.0, "m": "walkright",
                             "fx": [], "pet": "", "ts": 81234}))
    move = moved(await collect(i)).get(uid("presh"))
    check("a step carries the game's own clock to the others (ts)", move and move[6] == 81234, move)
    await h.send(json.dumps({"t": "s", "a": "town", "x": 502.0, "y": 500.0, "m": "walkright",
                             "fx": [], "pet": "", "ts": 81334.0}))
    move = moved(await collect(i)).get(uid("presh"))
    check("  a whole number sent as a float is that number", move and move[6] == 81334
          and isinstance(move[6], int), move)
    k, _ = await enter(port, KIT, "town", x=540.0, y=500.0)
    entry = joined(await collect(k)).get(uid("presh"))
    check("  and arriving, you are told everyone's latest", entry and entry.get("ts") == 81334, entry)
    await collect(h), await collect(i)
    bad_clocks = [-5, 1.5, "81434", True, presence.MAX_CLOCK + 1, None]
    for clock in bad_clocks:
        await h.send(json.dumps({"t": "s", "a": "town", "x": 503.0, "y": 500.0, "m": "walkright",
                                 "fx": [], "pet": "", "ts": clock}))
    check("a clock that is not one (%d kinds) refuses the state" % len(bad_clocks),
          not moved(await collect(i)), None)
    for n in range(presence.MAX_STEPS_PER_TICK + 3):
        await h.send(json.dumps({"t": "s", "a": "town", "x": 510.0 + n, "y": 500.0, "m": "walkright",
                                 "fx": [], "pet": "", "ts": 90000 + n}))
    got_i = await collect(i)
    per_message = [[e for e in m["p"] if e[0] == uid("presh")] for m in of(got_i, "moves")]
    check("a burst of steps in one tick keeps the newest %d" % presence.MAX_STEPS_PER_TICK,
          per_message and all(len(steps) <= presence.MAX_STEPS_PER_TICK for steps in per_message)
          and per_message[-1][-1][1] == 510.0 + presence.MAX_STEPS_PER_TICK + 2, per_message)
    await h.send(json.dumps({"t": "s", "a": "town", "x": 500.0, "y": 500.0, "m": "idleright",
                             "fx": [], "pet": "", "ts": 91000}))
    await collect(h), await collect(i), await collect(k)

    # ---- attacks ----
    attacks = [["meteor", 91100, 500.0, 500.0, 640.25, 410.0, 0, 1],
               ["meteor", 91100, 500.0, 500.0, 660.0, 420.0, 250, 0],
               ["dyn", 91200.0, 500.04, 499.96, 560.0, 500.0, 120.0, 0]]
    await h.send(json.dumps({"t": "x", "e": attacks}))
    got_i, got_h, got_j = await collect(i), await collect(h), await collect(j)
    heard = of(got_i, "x")
    check("an attack is passed to everyone else in the area, who made it named",
          len(heard) == 1 and heard[0]["id"] == uid("presh") and len(heard[0]["e"]) == 3, got_i)
    check("  checked and tidied: positions to a tenth, whole clocks and delays",
          heard and heard[0]["e"][2] == ["dyn", 91200, 500.0, 500.0, 560.0, 500.0, 120, 0]
          and heard[0]["e"][0] == ["meteor", 91100, 500.0, 500.0, 640.2, 410.0, 0, 1], heard)
    check("  never back to the game that made it", not of(got_h, "x"), got_h)
    check("  and never to another area", not of(got_j, "x"), got_j)
    check("  a picture is not a hit: nothing goes to anyone as one", not of(got_i, "h") and not of(got_h, "h"))
    await collect(k)
    good = ["slash", 92000, 500.0, 500.0, 520.0, 500.0, 0, 0]
    bad_attacks = [
        [["laser"] + good[1:]],
        [good[:7]],
        [["slash", -1] + good[2:]],
        [["slash", 92000, 950.0, 500.0, 960.0, 500.0, 0, 0]],
        [["meteor", 92000, 500.0, 500.0, 1550.0, 500.0, 0, 0]],
        [["meteor", 92000, 500.0, 500.0, 520.0, 500.0, presence.MAX_ATTACK_DELAY_MS + 1, 0]],
        [["axe", 92000, 500.0, 500.0, 520.0, 500.0, 0, presence.MAX_ATTACK_FLAGS + 1]],
        [["axe", 92000, 500.0, 500.0, 520.0, 500.0, 0, 1.5]],
        [["orb", 92000, True, 500.0, 520.0, 500.0, 0, 0]],
        [["orb", 92000, "500", 500.0, 520.0, 500.0, 0, 0]],
        [good] * (presence.MAX_ATTACKS_PER_MESSAGE + 1),
        [],
        [good, ["slash", 92000, 500.0, 500.0, 1e9, 500.0, 0, 0]],
    ]
    for batch in bad_attacks:
        await h.send(json.dumps({"t": "x", "e": batch}))
    await h.send('{"t": "x", "e": [["slash", 92000, NaN, 500.0, 520.0, 500.0, 0, 0]]}')
    await h.send(json.dumps({"t": "x", "e": "slash"}))
    check("none of %d malformed attack messages reaches anyone - one bad attack refuses its message"
          % (len(bad_attacks) + 2), not of(await collect(i), "x"), None)
    await h.send(json.dumps({"t": "x", "e": [good]}))
    check("  and the socket is still open for a good one after them",
          [m["e"] for m in of(await collect(i), "x")] == [[good]])
    lost, _ = await enter(port, HAL)
    await collect(h)
    await lost.send(json.dumps({"t": "x", "e": [good]}))
    check("an attack from a game standing nowhere yet goes nowhere", not of(await collect(i), "x"))
    await lost.close()
    h, _ = await enter(port, HAL, "town", x=500.0, y=500.0)
    await collect(h), await collect(i), await collect(k)
    check("the rate leaves room for ten states, ten batches of hits and ten of attack pictures a second",
          presence.RATE_PER_SECOND >= 3 * 10 + 5 and presence.BURST >= 2 * presence.RATE_PER_SECOND,
          (presence.RATE_PER_SECOND, presence.BURST))

    # ---- levers ----
    OUT, IN = "ysortworld/interactables/levergatesout", "ysortworld/interactables/levergatesin"
    await h.send(json.dumps({"t": "l", "n": OUT, "on": True}))
    got_i, got_h, got_j = await collect(i), await collect(h), await collect(j)
    check("a lever pulled is passed to everyone else in the area",
          of(got_i, "l") == [{"t": "l", "id": uid("presh"), "n": OUT, "on": True}], got_i)
    check("  not back, and not to another area", not of(got_h, "l") and not of(got_j, "l"))
    await k.close()
    await collect(h), await collect(i)
    k, _ = await enter(port, KIT, "town", x=540.0, y=500.0)
    got_k = await collect(k)
    check("a game walking in later is told the levers pulled", of(got_k, "levers") == [
        {"t": "levers", "p": [[OUT, True]]}], got_k)
    await i.send(json.dumps({"t": "l", "n": IN, "on": False}))
    await h.send(json.dumps({"t": "l", "n": OUT, "on": False}))
    await collect(h), await collect(i), await collect(k)
    await k.send(json.dumps({"t": "sync"}))
    got_k = await collect(k)
    check("  in the order they were last pulled, so two levers on one gate end where the last pull left it",
          of(got_k, "levers") == [{"t": "levers", "p": [[IN, False], [OUT, False]]}], got_k)
    bad_levers = [{"n": "../../db", "on": True}, {"n": "a b", "on": True}, {"n": "x" * 129, "on": True},
                  {"n": OUT, "on": 1}, {"n": OUT}, {"on": True}, {"n": 5, "on": True}, {"n": "", "on": True}]
    for bad_lever in bad_levers:
        await h.send(json.dumps(dict(bad_lever, t="l")))
    check("none of %d malformed lever pulls is passed on or remembered" % len(bad_levers),
          not of(await collect(i), "l") and list(server.levers["town"]) == [IN, OUT], server.levers.get("town"))
    for n in range(presence.MAX_LEVERS_PER_AREA):
        await h.send(json.dumps({"t": "l", "n": "lever%d" % n, "on": True}))
    await collect(h), await collect(i), await collect(k)
    check("an area remembers at most %d levers - a new name past that is refused"
          % presence.MAX_LEVERS_PER_AREA, len(server.levers["town"]) == presence.MAX_LEVERS_PER_AREA
          and "lever%d" % (presence.MAX_LEVERS_PER_AREA - 1) not in server.levers["town"],
          len(server.levers["town"]))
    await h.send(json.dumps({"t": "l", "n": OUT, "on": True}))
    check("  while one it knows can still be pulled", of(await collect(i), "l")
          and list(server.levers["town"])[-1] == OUT and server.levers["town"][OUT] is True)
    for ws in (h, i, k):
        await ws.close()
    await asyncio.sleep(0.15)
    check("the area emptied, its levers are forgotten: the next game in loads every lever as built",
          "town" not in server.levers, server.levers.keys())
    h, _ = await enter(port, HAL, "town", x=500.0, y=500.0)
    check("  and is told of none", not of(await collect(h), "levers"))
    await h.close()
    await j.close()

    # =========================================================================
    section("P-6 LEAVING")
    # =========================================================================
    await a.close()
    got_b = await collect(b)
    check("closing the game leaves everyone's screen", left(got_b) == [uid("presa")], got_b)
    check("  and the server's memory", uid("presa") not in server.players
          and all(p.user_id != uid("presa") for room in server.rooms.values() for p in room))
    await b.close()
    await asyncio.sleep(0.1)
    check("an empty area is forgotten", not server.rooms, server.rooms)


async def main():
    presence.HELLO_SECONDS = 0.5
    combatbook.HOLD_SECONDS = 0.2
    combatbook.PENDING_SECONDS = 0.3
    combatbook.EMPTY_KEEP = 0.5
    server = presence.PresenceServer(sweep_seconds=0.3, tick_seconds=0.05)
    server.flush_seconds = 0.2
    server.start_clocks()
    async with serve(server.handler, "127.0.0.1", 0, max_size=presence.MAX_SOCKET_BYTES) as ws_server:
        port = list(ws_server.sockets)[0].getsockname()[1]
        try:
            await run(port, server)
        finally:
            server.stop_clocks()


asyncio.run(main())

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
if failures:
    for f in failures:
        print("  - " + f)
sys.exit(1 if failed else 0)
