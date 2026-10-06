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
          welcome == {"t": "welcome", "id": uid("presa"), "v": presence.SHARED_VERSION}, welcome)
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
          move == [uid("presa"), 120.5, 205.2, "walkright", [], ""], got_b)
    await a.send(state("field", x=120.5, y=205.2, m="attackright"))
    await a.send(state("field", x=121.0, y=205.2, m="attackright"))
    got_b = await collect(b)
    batched = [m for m in got_b if m.get("t") == "moves"]
    check("  two steps inside one tick are one message, the newest",
          len(batched) == 1 and moved(batched)[uid("presa")][1] == 121.0, got_b)

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
          moved(got_b).get(uid("presa")) == [uid("presa"), 101.0, 200.0, "walkup", ["firering", "ring"], ""],
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
    server = presence.PresenceServer(sweep_seconds=0.3, tick_seconds=0.05)
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
