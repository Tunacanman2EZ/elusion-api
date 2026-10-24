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
import asyncio, importlib.util, json, math, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_presence_test.db")
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


def state(area="field", x=100.0, y=200.0, m="idledown", fx=None, pet="", **more):
    msg = {"t": "s", "a": area, "x": x, "y": y, "m": m, "fx": fx or [], "pet": pet}
    msg.update(more)
    return json.dumps(msg)


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


async def enter(port, headers, area=None, v=None, xt=None, **kw):
    """A connected game: hello with a fresh ticket, welcome read, optionally
    standing somewhere. v=2 is a game that shares monsters; xt=1 one that
    takes attacks inside the tick (game 0.76.0)."""
    raw, _ = ticket(headers)
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port, max_size=presence.MAX_SOCKET_BYTES)
    hello = {"t": "hello", "ticket": raw}
    if v is not None:
        hello["v"] = v
    if xt is not None:
        hello["xt"] = xt
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
          move == [uid("presa"), 120.5, 205.2, "walkright", [], "", -1, -1, -1], got_b)
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
    await asyncio.sleep(0.5)
    stood = [tuple(r) for r in sql("SELECT slot, area, x, y FROM position_samples WHERE user_id = ?",
                                   (uid("presa"),))]
    check("  and where a game stands reaches position_samples off the wire (P-12)",
          (0, "elusion", 12.0, 12.0) in stood and any(r[1] == "field" for r in stood), stood)
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
          moved(got_b).get(uid("presa")) == [uid("presa"), 101.0, 200.0, "walkup", ["firering", "ring"], "", -1, -1, -1],
          got_b)

    # THEIR AIM AND THEIR HEALTH (game 0.41.0): "i want players to see
    # everything i see".
    await a.send(state("field", x=101.0, y=200.0, m="walkup", aim=344, hp=87))
    got_b = await collect(b)
    carried = moved(got_b).get(uid("presa"))
    check("a state's aim (whole degrees) and health (whole percent) go on to everyone in the area",
          carried is not None and carried[7:] == [344, 87], got_b)
    for raw_bad in (state(aim=360), state(aim=-2), state(aim="up"), state(aim=1.5), state(aim=True),
                    state(hp=101), state(hp=-5), state(hp="full"), state(hp=None)):
        await a.send(raw_bad)
    got_b = await collect(b)
    check("  an aim past 359 or a health past 100, or either not a whole number, reaches nobody",
          not moved(got_b), got_b)
    await a.send(json.dumps({"t": "sync"}))
    told = joined(await collect(a)).get(uid("presb"))
    await b.send(state("field", x=140.0, y=200.0, aim=0, hp=100))
    await collect(a), await collect(b)
    await a.send(json.dumps({"t": "sync"}))
    told = joined(await collect(a)).get(uid("presb"))
    check("  and somebody arriving is told everyone's, in the join",
          told is not None and told.get("aim") == 0 and told.get("hp") == 100, told)

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
    # RENEWED FIRST, THEN THE OLD ONE RUNS OUT - the order a game does it in
    # (it renews a minute into a two-minute ticket). The other way round, a
    # sweep landing between the two lines found an expired ticket the renewal
    # had not reached yet, and dropped the socket: a race of the test's own,
    # which suites run side by side (run_tests.ps1 -Jobs) made show.
    await b.send(json.dumps({"t": "renew", "ticket": fresh}))
    got_a = await collect(a)
    sql("UPDATE presence_tickets SET expires_at = ? WHERE user_id = ? AND token_hash != ?",
        (int(time.time()) - 1, uid("presb"), presence._hash(fresh)))
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
    # By the rate: since game 0.71.0 a bare mage's critical spike is as big a
    # single hit as Hal's kitted swing and wave.
    check("  each player's bounds come from their own ticket",
          hal_bounds["dps"] > ivy_bounds["dps"] > 0 and ivy_bounds["max_hit"] > 0, (hal_bounds, ivy_bounds))
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

    bare = os.path.join(testdir.PATH, "elusion_presence_bare.db")
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
    section("P-10 THE AURA'S HEALS: A HEALER'S AURA IS WRITTEN DOWN FOR EVERYONE IT REACHES")
    # =========================================================================
    # Game 0.40.0. The owner: "Heal players + hurt monsters", and of the blobs:
    # "each blob travels to players outside of the circle giving them hp".
    # Every game heals its own player from the picture; this server writes it
    # down for the API's healing check.
    share, reach, cooldown = presence.aura_numbers()
    hrow = (presence.gamedata.COMBAT.get("classes") or {}).get("healer") or {}
    check("the aura's numbers come from the catalogue's healer row",
          share == float(hrow.get("aura_heal_share")) and reach == float(hrow.get("aura_reach"))
          and cooldown == float(hrow.get("aura_cooldown")) and share > 0 and reach > 0 and cooldown > 0,
          (share, reach, cooldown))
    check("  \"heal\" is a kind of attack picture", "heal" in presence.ATTACK_KINDS)
    MEND = account("presmend", "healer")
    NEAR = account("presnear")
    AFAR = account("presafar", "mage")
    ELSE = account("preselse", "tank")
    m, _ = await enter(port, MEND, "town", x=500.0, y=500.0)
    n, _ = await enter(port, NEAR, "town", x=600.0, y=520.0)
    f, _ = await enter(port, AFAR, "town", x=500.0, y=900.0)
    e, _ = await enter(port, ELSE, "elusion", x=530.0, y=480.0)
    for ws in (m, n, f, e):
        await collect(ws)

    def grants():
        return {r["user_id"]: (r["target"], r["amount"]) for r in sql(
            "SELECT user_id, target, amount FROM consume_grants WHERE item_id = ?", (presence.HEAL_GRANT_ID,))}

    def owed(name, class_id):
        top = presence.gamedata.max_stats_for(class_id, 1, {})["max_hp"]
        return ("HP", math.ceil(top * share) + presence.HEAL_ROUNDING)

    heal = ["heal", 93000, 500.0, 500.0, 530.0, 480.0, 0, 0]
    await m.send(json.dumps({"t": "x", "e": [heal]}))
    heard = of(await collect(n), "x")
    await collect(m), await collect(f), await collect(e)
    check("a healer's aura is passed on as a picture, as every attack is",
          heard and heard[0]["e"] == [heal] and heard[0]["id"] == uid("presmend"), heard)
    got = grants()
    check("  and written down for the healer and everyone in the area within its reach",
          got.get(uid("presmend")) == owed("presmend", "healer") and got.get(uid("presnear")) == owed("presnear", "warrior"),
          got)
    check("  'HP', the aura's share of each one's own maximum, rounded up, and a little for the game's rounding",
          got.get(uid("presnear"), ("", 0))[1] == math.ceil(
              presence.gamedata.max_stats_for("warrior", 1, {})["max_hp"] * share) + presence.HEAL_ROUNDING, got)
    check("  not for anyone further than the reach, nor in another area",
          uid("presafar") not in got and uid("preselse") not in got, got)
    before = len(sql("SELECT id FROM consume_grants WHERE item_id = ?", (presence.HEAL_GRANT_ID,)))
    await m.send(json.dumps({"t": "x", "e": [["heal", 93100, 500.0, 500.0, 530.0, 480.0, 0, 0]]}))
    again = of(await collect(n), "x")
    after = len(sql("SELECT id FROM consume_grants WHERE item_id = ?", (presence.HEAL_GRANT_ID,)))
    check("a second aura sooner than the cooldown is passed on as a picture and written down for nobody",
          again and before == after == 2, (before, after))
    await n.send(json.dumps({"t": "x", "e": [["heal", 93200, 600.0, 520.0, 600.0, 520.0, 0, 0]]}))
    from_warrior = of(await collect(m), "x")
    check("  nor is one from anybody but a healer",
          from_warrior and len(sql("SELECT id FROM consume_grants WHERE item_id = ?", (presence.HEAL_GRANT_ID,))) == 2)
    mender = server.players[uid("presmend")]
    mender.last_aura -= cooldown
    owed_now = server.note_heal(mender, 500.0, 900.0)
    written = await server.grant_heals()
    check("once the cooldown has passed, the next is written down - here, for the one it reached",
          [p.user_id for p in owed_now] == [uid("presafar")] and [w[0] for w in written] == [uid("presafar")],
          (owed_now, written))
    check("  the slot each grant is for is the character's own",
          all(w[1] == 0 for w in written) and mender.slot == 0, written)
    for ws in (m, n, f, e):
        await ws.close()
    await asyncio.sleep(0.1)

    # =========================================================================
    section("P-11 PARTY VOICE: A CALL'S SETUP PASSES ONLY BETWEEN TWO IN VOICE IN ONE PARTY")
    # =========================================================================
    # Game 0.43.0. The owner: "I really really want the option for voice chat
    # in game for parties". The voice never comes here; what starts a call does.
    VA = account("presva")
    VB = account("presvb", "mage")
    VC = account("presvc", "healer")
    VD = account("presvd", "tank")
    va, _ = await enter(port, VA, "town")
    vb, _ = await enter(port, VB, "voiceside")
    vc_, _ = await enter(port, VC, "town")
    vd, _ = await enter(port, VD, "town")
    for ws in (va, vb, vc_, vd):
        await collect(ws)

    def voice(messages):
        return of(messages, "vs")

    await va.send(json.dumps({"t": "vs", "on": True}))
    got = voice(await collect(va))
    check("joining voice in no party: told no (off: party), and not in it",
          got == [{"t": "vs", "off": "party"}] and not server.players[uid("presva")].voice, got)
    client.post("/api/party/invite", headers=VA, json={"username": "presvb"})
    client.post("/api/party/respond", headers=VB, json={"from": "presva", "accept": True})
    client.post("/api/party/invite", headers=VA, json={"username": "presvc"})
    client.post("/api/party/respond", headers=VC, json={"from": "presva", "accept": True})
    await va.send(json.dumps({"t": "vs", "on": True}))
    got = voice(await collect(va))
    check("the first in voice in a party is told nobody else is", got == [{"t": "vs", "in": []}], got)
    await vb.send(json.dumps({"t": "vs", "on": True}))
    got_b, got_a = voice(await collect(vb)), voice(await collect(va))
    check("the next is told who is (it calls them) - in another area, too",
          got_b == [{"t": "vs", "in": [{"id": uid("presva"), "name": "presva"}]}], got_b)
    check("  and they are told it came (they wait for its call)",
          got_a == [{"t": "vs", "join": {"id": uid("presvb"), "name": "presvb"}}], got_a)
    offer = {"k": "sdp", "sdp": {"type": "offer", "sdp": "v=0\r\n" + "a=x\r\n" * 600}}
    await vb.send(json.dumps({"t": "vc", "to": uid("presva"), "d": offer}))
    got = of(await collect(va), "vc")
    check("a call's setup passes between two in voice in one party, as sent, with who from - "
          "even past the socket's usual size (a description is kilobytes)",
          got == [{"t": "vc", "from": uid("presvb"), "d": offer}]
          and len(json.dumps(offer)) > presence.MAX_MESSAGE_BYTES, len(json.dumps(offer)))
    await vd.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"k": "ice"}}))
    await vc_.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"k": "ice"}}))
    await va.send(json.dumps({"t": "vc", "to": uid("presvc"), "d": {"k": "ice"}}))
    await va.send(json.dumps({"t": "vc", "to": uid("presvd"), "d": {"k": "ice"}}))
    heard = [of(await collect(ws), "vc") for ws in (va, vc_, vd)]
    check("nothing passes from somebody in no party, from a member not in voice, "
          "or to somebody not in voice or not in the party", heard == [[], [], []], heard)
    # ANOTHER PARTY, IN VOICE TOO: in voice is not enough - the same party is.
    VE = account("presve", "mage")
    ve, _ = await enter(port, VE, "town")
    await collect(ve)
    client.post("/api/party/invite", headers=VD, json={"username": "presve"})
    client.post("/api/party/respond", headers=VE, json={"from": "presvd", "accept": True})
    await vd.send(json.dumps({"t": "vs", "on": True}))
    await ve.send(json.dumps({"t": "vs", "on": True}))
    await collect(vd), await collect(ve)
    await vd.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"k": "ice"}}))
    await va.send(json.dumps({"t": "vc", "to": uid("presve"), "d": {"k": "ice"}}))
    crossed = [of(await collect(ws), "vc") for ws in (va, ve)]
    await vd.send(json.dumps({"t": "vc", "to": uid("presve"), "d": {"k": "ice"}}))
    own = of(await collect(ve), "vc")
    check("nor between two in voice in two different parties - while within one, it does",
          crossed == [[], []] and own == [{"t": "vc", "from": uid("presvd"), "d": {"k": "ice"}}], (crossed, own))
    for bad in ({"t": "vc", "to": True, "d": {}}, {"t": "vc", "to": uid("presva"), "d": "text"},
                {"t": "vc", "to": uid("presvb"), "d": {}}, {"t": "vc", "d": {}}):
        await vb.send(json.dumps(bad))
    check("  nor anything not shaped as a call's setup, or to yourself", of(await collect(va), "vc") == []
          and of(await collect(vb), "vc") == [])
    sent = int(presence.VOICE_BURST) + 10
    for i in range(sent):
        await vb.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"k": "ice", "n": i}}))
    passed_on = len(of(await collect(va, 0.6), "vc"))
    check("at most VOICE_BURST at once pass on, however many are sent",
          int(presence.VOICE_BURST) - 1 <= passed_on <= int(presence.VOICE_BURST) + 2 and passed_on < sent,
          (passed_on, sent))
    big, _ = await enter(port, account("presvbig"), "town")
    await collect(big)
    await big.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"x": "y" * (presence.VOICE_MAX_BYTES + 10)}}))
    check("  and none bigger than VOICE_MAX_BYTES: the socket goes",
          "too big" in byes(await collect(big)), "")
    await vc_.send(json.dumps({"t": "vs", "on": True}))
    await collect(vc_)
    await collect(va), await collect(vb)
    client.post("/api/party/leave", headers=VB)
    await asyncio.sleep(0.5)
    got_b, got_a, got_c = voice(await collect(vb)), voice(await collect(va)), voice(await collect(vc_))
    check("leaving the party takes you out of its voice within a sweep",
          {"t": "vs", "off": "party"} in got_b and not server.players[uid("presvb")].voice, got_b)
    check("  and the others are told you left it",
          {"t": "vs", "left": uid("presvb")} in got_a and {"t": "vs", "left": uid("presvb")} in got_c, (got_a, got_c))
    await vb.send(json.dumps({"t": "vc", "to": uid("presva"), "d": {"k": "ice"}}))
    check("  after which nothing of yours reaches them", of(await collect(va), "vc") == [])
    await vc_.send(json.dumps({"t": "vs", "on": False}))
    got = voice(await collect(va))
    check("switching voice off tells the others", got == [{"t": "vs", "left": uid("presvc")}], got)
    await vc_.send(json.dumps({"t": "vs", "on": True}))
    await collect(vc_), await collect(va)
    await va.close()
    got = voice(await collect(vc_))
    check("closing the game is leaving voice", got == [{"t": "vs", "left": uid("presva")}], got)
    for ws in (vb, vc_, vd, ve, big):
        await ws.close()
    await asyncio.sleep(0.1)

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

    # LAST, ON TICKETS OF THEIR OWN: the games above are two minutes old by
    # here, and a ticket lives two (the sweep signs them out).
    await attacks_ride_the_tick(port, server)
    await a_crowd_walks_in(port, server)


async def a_crowd_walks_in(port, server):
    """A LEADER ANSWERING IS NOT A GAME GOING TOO FAST (0.76.0): see presence.py."""
    section("P-9c A CROWD WALKS IN: THE LEADER SENDS EVERY ONE OF THEM EVERYTHING, AND STAYS")
    quiet = presence.Player(None, 1, "hash", {}, time.time() + 60)
    quiet.tokens, quiet.stamp = 0.0, time.monotonic() + 3600
    check("a game with an empty bucket is refused", quiet.allow() is False)
    quiet.asked_for_world()
    granted = sum(1 for _ in range(presence.NEED_MESSAGES + 5) if quiet.allow())
    check("  one this server just asked for the world may send NEED_MESSAGES (%d) more, and no more"
          % presence.NEED_MESSAGES, granted == presence.NEED_MESSAGES, granted)
    quiet.asked_for_world()
    quiet.need_credit_until = time.monotonic() - 1
    check("  for NEED_CREDIT_SECONDS after it was asked, not for ever", quiet.allow() is False)
    for _ in range(400):
        quiet.asked_for_world()
    check("  and never more than MAX_NEED_CREDIT, however many walk in",
          quiet.need_credit == presence.MAX_NEED_CREDIT, quiet.need_credit)
    check("NEED_MESSAGES covers what the biggest area's leader sends a newcomer: what changed, and every "
          "authored monster at monstersync.gd's 60 a message",
          presence.NEED_MESSAGES >= 1 + math.ceil(max(len(a.get("spawns", [])) for a in
                                                       json.load(open(os.path.join(HERE, "gamedata.json"),
                                                                      encoding="utf-8"))["areas"].values()) / 60))

    # ---- live: fourteen arrive together; the leader answers each with eight ----
    PARTS = 8
    lead_hdr = account("preslead")
    crowd = [account("prescrowd%d" % n) for n in range(14)]
    lead, _ = await enter(port, lead_hdr, "crowdtest", v=presence.BOOKS_VERSION, x=10.0, y=10.0)
    answered = []
    said = []

    async def lead_plays():
        clock = 0
        try:
            async for raw in lead:
                msg = json.loads(raw)
                if msg.get("t") == "bye":
                    said.append(msg.get("why"))
                if msg.get("t") == "need" and msg.get("id", 0) > 0:
                    to = msg["id"]
                    await lead.send(json.dumps({"t": "w", "d": {"snap": []}}))
                    for part in range(PARTS - 1):
                        await lead.send(json.dumps({"t": "w", "to": to, "d": {
                            "full": True, "reset": False, "part": part, "parts": PARTS - 1, "spawn": [], "wave": 0}}))
                    answered.append(to)
                    clock += 100
                    await lead.send(state("crowdtest", x=10.0 + len(answered), y=10.0, ts=clock))
        except ConnectionClosed:
            said.append("_closed")
    player = asyncio.create_task(lead_plays())
    arrivals = []
    for hdr in crowd:
        ws, _ = await enter(port, hdr, "crowdtest", v=presence.BOOKS_VERSION, x=20.0, y=20.0)
        arrivals.append(ws)
    for _ in range(40):
        if len(answered) >= len(crowd) or said:
            break
        await asyncio.sleep(0.05)
    fulls = [len([m for m in await collect(ws, 0.3) if m.get("t") == "w" and m.get("d", {}).get("full")])
             for ws in arrivals]
    check("fourteen walk in at once and the leader sends each of them everything (%d messages each), "
          "and is not dropped \"too fast\"" % PARTS,
          len(answered) == len(crowd) and not said, (len(answered), said))
    check("  every one of them has its whole world", fulls == [PARTS - 1] * len(crowd), fulls)
    player.cancel()
    for ws in arrivals + [lead]:
        await ws.close()


async def kills_written_soon():
    """A DEATH IS WRITTEN SOONER (0.57.0): a game waits on it to be paid."""
    calls = []
    real = presence.write_books
    presence.write_books = lambda kills, flags, prune=None: calls.append((len(kills), len(flags), prune))
    try:
        quick = presence.PresenceServer(sweep_seconds=5, tick_seconds=5)
        quick.flush_seconds = 5.0
        quick.kills_flush_seconds = 0.05
        quick._kills = [{"at": 1}]
        quick._flags = {(1, "field", "too_fast"): [1, "x", 1, 1]}
        task = asyncio.create_task(quick.flush_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        flags_left = len(quick._flags)
    finally:
        presence.write_books = real
    check("a death the books judged is written within KILLS_FLUSH_SECONDS, the flags kept for the full flush",
          calls == [(1, 0, None)] and flags_left == 1 and presence.KILLS_FLUSH_SECONDS <= 0.25, (calls, flags_left))


async def positions_written():
    """WHERE EVERYONE STANDS (19 Oct; LAUNCH_ONE_WAY_DOORS.md, D3): a sample of
    each character that moved, beside the books and never in their write."""
    section("P-12 WHERE EVERYONE STANDS: A SAMPLE OF EACH CHARACTER THAT MOVED, WRITTEN BESIDE THE BOOKS")
    def samples():
        return [tuple(r) for r in sql("SELECT user_id, slot, area, x, y FROM position_samples"
                                      " WHERE user_id IN (9001, 9002, 9003) ORDER BY id")]
    hub = presence.PresenceServer(sweep_seconds=5, tick_seconds=5)
    hub.position_sample_seconds = 0.0
    walker = presence.Player(None, 9001, "hash-1", {}, time.time() + 60, slot=1)
    walker.area, walker.x, walker.y = "field", 120.5, 340.0
    nowhere = presence.Player(None, 9002, "hash-2", {}, time.time() + 60, slot=0)
    hub.players = {9001: walker, 9002: nowhere}
    await hub.flush_books()
    check("a character in an area is written down: who, which character, the area and where",
          samples() == [(9001, 1, "field", 120.5, 340.0)], samples())
    check("  and one in no area yet is not", all(r[0] != 9002 for r in samples()), samples())
    await hub.flush_books()
    check("standing still writes nothing more", len(samples()) == 1, samples())
    walker.x = 180.0
    await hub.flush_books()
    walker.area = "town"
    await hub.flush_books()
    check("a step writes a sample, and so does another area where it stood",
          samples()[1:] == [(9001, 1, "field", 180.0, 340.0), (9001, 1, "town", 180.0, 340.0)], samples())
    hub.position_sample_seconds = 3600.0
    walker.x = 999.0
    await hub.flush_books()
    check("  but only one a sample's length (POSITION_SAMPLE_SECONDS, %g s)" % presence.POSITION_SAMPLE_SECONDS,
          len(samples()) == 3 and 5.0 <= presence.POSITION_SAMPLE_SECONDS <= 60.0, samples())
    old = int(time.time()) - presence.POSITIONS_KEPT_SECONDS - 60
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO position_samples (at, user_id, slot, area, x, y) VALUES (?, 9003, 0, 'field', 1, 1)",
                (old,))
    con.commit()
    con.close()
    hub._positions_pruned_at = 0.0
    await hub.flush_books()
    check("a sample older than POSITIONS_KEPT_SECONDS (two weeks) is pruned, the rest kept",
          all(r[0] != 9003 for r in samples()) and len(samples()) == 3
          and presence.POSITIONS_KEPT_SECONDS == 14 * 86400, samples())

    # A DATABASE FROM BEFORE THE TABLE: the books are written all the same.
    before = os.path.join(testdir.PATH, "before_positions.db")
    con = sqlite3.connect(before)
    con.execute("CREATE TABLE combat_kills (id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, area TEXT,"
                " enemy_id TEXT, origin TEXT, leader_id INTEGER, user_id INTEGER, slot INTEGER, verdict TEXT,"
                " damage INTEGER, refused INTEGER, hp_left INTEGER, max_hp INTEGER, seconds REAL)")
    con.execute("CREATE TABLE combat_flags (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, area TEXT,"
                " kind TEXT, count INTEGER, detail TEXT, first_at INTEGER, last_at INTEGER)")
    con.commit()
    con.close()
    real_path = presence.DB_PATH
    presence.DB_PATH = before
    try:
        old_db = presence.PresenceServer(sweep_seconds=5, tick_seconds=5)
        old_db.position_sample_seconds = 0.0
        mover = presence.Player(None, 9001, "hash-1", {}, time.time() + 60, slot=1)
        mover.area, mover.x, mover.y = "field", 10.0, 10.0
        old_db.players = {9001: mover}
        old_db._kills = [{"at": int(time.time()), "area": "field", "enemy_id": "slime", "origin": "",
                          "leader_id": 0, "user_id": 9001, "slot": 1, "verdict": "agreed", "damage": 5,
                          "refused": 0, "hp_left": 0, "max_hp": 5, "seconds": 1.0}]
        await old_db.flush_books()
    finally:
        presence.DB_PATH = real_path
    con = sqlite3.connect(before)
    kills_there = con.execute("SELECT COUNT(*) FROM combat_kills").fetchone()[0]
    con.close()
    check("a database from before the table still gets the death the books judged, and says why it has no positions",
          kills_there == 1 and bool(presence._POSITIONS_TABLE_MISSING), (kills_there, presence._POSITIONS_TABLE_MISSING))


async def health_written():
    """HOW ITS MINUTES WENT (game 0.78.0): a presence_health row a minute -
    the minute's own counts, the biggest crowds, the machine's network out
    and memory - beside the books, pruned after two weeks."""
    section("P-13 HOW ITS MINUTES WENT: A HEALTH ROW A MINUTE, ITS OWN MINUTE'S COUNTS")
    real_net, real_mem = presence.NET_DEV_PATH, presence.MEMINFO_PATH
    net_file = os.path.join(testdir.PATH, "net_dev")
    mem_file = os.path.join(testdir.PATH, "meminfo")

    def net_dev(eth0, eth1=0, lo=999999):
        with open(net_file, "w", encoding="ascii") as handle:
            handle.write("Inter-|   Receive                                                |  Transmit\n"
                         " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
                         "    lo: 5000 10 0 0 0 0 0 0 %d 10 0 0 0 0 0 0\n"
                         "  eth0: 7000 20 0 0 0 0 0 0 %d 30 0 0 0 0 0 0\n"
                         "  eth1: 100 1 0 0 0 0 0 0 %d 1 0 0 0 0 0 0\n" % (lo, eth0, eth1))

    with open(mem_file, "w", encoding="ascii") as handle:
        handle.write("MemTotal:        1009000 kB\nMemFree:          100000 kB\nMemAvailable:     409600 kB\n")
    net_dev(10000, 500)
    presence.NET_DEV_PATH, presence.MEMINFO_PATH = net_file, mem_file
    try:
        check("the machine's network out: every interface's transmit bytes but loopback's",
              presence.net_out_bytes() == 10500, presence.net_out_bytes())
        check("  and memory available, in MB", presence.mem_free_mb() == 400, presence.mem_free_mb())
        check("  neither where there is nothing to read (Windows, a Mac)",
              presence.net_out_bytes(os.path.join(testdir.PATH, "nothing")) is None
              and presence.mem_free_mb(os.path.join(testdir.PATH, "nothing")) is None)

        hub = presence.PresenceServer(sweep_seconds=5, tick_seconds=5)
        sent_to = []
        real_broadcast = presence.broadcast
        presence.broadcast = lambda connections, text: sent_to.append((list(connections), text))

        class Quiet:
            async def send(self, text):
                sent_to.append((["one"], text))
        try:
            hub._out(["a", "b", "c"], "x" * 100)
            hub._out([], "never sent")
            await hub._send(presence.Player(Quiet(), 9100, "hash-9100", {}, time.time() + 60), {"t": "x"})
        finally:
            presence.broadcast = real_broadcast
        check("everything sent is counted: a frame for each socket a text goes to, and its bytes for each",
              (hub.frames_out, hub.bytes_out) == (3 + 1, 300 + len('{"t":"x"}'))
              and [len(c) for c, _t in sent_to] == [3, 1], (hub.frames_out, hub.bytes_out, sent_to))
        hub.frames_out = hub.bytes_out = 0
        check("the first take only marks where the counters stand: no minute behind it yet",
              hub.take_health(now=1000.0, now_mono=50.0) is None)
        people = {}
        for user_id, area in ((9101, "field"), (9102, "field"), (9103, "town"), (9104, "")):
            p = presence.Player(None, user_id, "hash-%d" % user_id, {}, time.time() + 60, slot=0)
            p.area = area
            people[user_id] = p
        hub.players = dict(people)
        hub.rooms = {"field": {people[9101], people[9102]}, "town": {people[9103]}}
        hub.ticks += 600
        hub.late_ticks += 2
        hub.frames_out += 30
        hub.bytes_out += 4500
        net_dev(250000, 500)
        row = hub.take_health(now=1060.0, now_mono=110.0)
        check("a minute's row: its length, who is connected and who is in an area, the areas and their crowds",
              row["at"] == 1060 and row["seconds"] == 60.0 and row["connections"] == 4 and row["players"] == 3
              and row["areas"] == 2 and json.loads(row["crowd"]) == [["field", 2], ["town", 1]], row)
        check("  the minute's ticks and late ticks, frames and bytes sent, and the machine's network out",
              (row["ticks"], row["late_ticks"], row["frames_out"], row["bytes_out"], row["net_out"])
              == (600, 2, 30, 4500, 240000), row)
        check("  this process's share of the core, the load and the memory",
              isinstance(row["cpu"], float) and row["cpu"] >= 0.0 and row["mem_free_mb"] == 400
              and (row["load1"] is None or isinstance(row["load1"], float)), row)
        check("  and every column the table has", set(row) == set(presence.HEALTH_COLUMNS), sorted(row))
        net_dev(90000, 500)
        row = hub.take_health(now=1120.0, now_mono=170.0)
        check("the next row is its own minute's: nothing sent, nothing counted - and a network counter that went "
              "back (a reboot) is no number rather than a wrong one",
              (row["ticks"], row["frames_out"], row["bytes_out"], row["net_out"]) == (0, 0, 0, None), row)
        hub.rooms = {"area%02d" % i: {presence.Player(None, 9200 + i * 10 + j, "h", {}, time.time() + 60)
                                       for j in range(i + 1)} for i in range(12)}
        row = hub.take_health(now=1180.0, now_mono=230.0)
        crowd = json.loads(row["crowd"])
        check("the HEALTH_AREAS (%d) biggest crowds are named, biggest first; every player counted"
              % presence.HEALTH_AREAS,
              len(crowd) == presence.HEALTH_AREAS == 8 and crowd[0] == ["area11", 12] and crowd[-1] == ["area04", 5]
              and row["players"] == sum(range(1, 13)) and row["areas"] == 12, (crowd, row["players"]))

        # WRITTEN, AND PRUNED.
        def health_rows():
            return [tuple(r) for r in sql("SELECT at, players, bytes_out FROM presence_health ORDER BY id")]
        before = len(health_rows())
        hub.rooms = {"field": {people[9101]}}
        hub.bytes_out += 777
        await hub.flush_health(now=time.time())
        rows = health_rows()
        check("a row is written to presence_health", len(rows) == before + 1 and rows[-1][1:] == (1, 777), rows[-1:])
        old = int(time.time()) - presence.HEALTH_KEPT_SECONDS - 60
        con = sqlite3.connect(DB_PATH)
        con.execute("INSERT INTO presence_health (at, seconds) VALUES (?, 60)", (old,))
        con.commit()
        con.close()
        hub._health_pruned_at = 0.0
        await hub.flush_health(now=time.time())
        check("a row older than HEALTH_KEPT_SECONDS (two weeks) is pruned, the rest kept",
              all(r[0] != old for r in health_rows()) and len(health_rows()) == before + 2
              and presence.HEALTH_KEPT_SECONDS == 14 * 86400 and presence.HEALTH_SECONDS == 60.0, health_rows())

        # A DATABASE FROM BEFORE THE TABLE: said once, nothing raised.
        before_db = os.path.join(testdir.PATH, "before_health.db")
        sqlite3.connect(before_db).close()
        real_path = presence.DB_PATH
        presence.DB_PATH = before_db
        try:
            await hub.flush_health(now=time.time())
            await hub.flush_health(now=time.time())
        finally:
            presence.DB_PATH = real_path
        check("a database from before the table: the server's health says why once, and nothing else stops",
              len(presence._HEALTH_TABLE_MISSING) == 1, presence._HEALTH_TABLE_MISSING)

        clocks = presence.PresenceServer(sweep_seconds=5, tick_seconds=5)
        clocks.start_clocks()
        names = [task.get_coro().__name__ for task in clocks._tasks]
        clocks.stop_clocks()
        check("its own clock, books or no books", "health_loop" in names, names)
    finally:
        presence.NET_DEV_PATH, presence.MEMINFO_PATH = real_net, real_mem


def counted_live(server):
    """P-13, against the whole run: every frame the server sent, counted."""
    section("P-13 EVERY FRAME THE RUN SENT WAS COUNTED")
    check("frames and bytes sent, through _out() and _send() - the run's welcomes, joins and moves",
          server.frames_out > 50 and server.bytes_out > 20 * server.frames_out, (server.frames_out, server.bytes_out))
    source = open(presence.__file__, encoding="utf-8").read()
    check("  and nothing goes out past them: broadcast() is called in _out() alone, ws.send() in _send() and"
          " the ticket's refusal",
          source.count("broadcast(connections, text)") == 1 and source.count(" broadcast(") == 1
          and source.count(".send(") == 2, (source.count(" broadcast("), source.count(".send(")))


def ridden(messages):
    """[[maker, attacks], ...] from every moves frame in these messages."""
    return [pair for m in of(messages, "moves") for pair in m.get("x", [])]


async def attacks_ride_the_tick(port, server):
    """P-9b ATTACKS RIDE THE TICK (game 0.76.0): see presence.py. h and i are
    games from before (no "xt") in "town", j one in "elusion"."""
    section("P-9b ATTACKS RIDE THE TICK: ONE FRAME A TICK FOR A GAME THAT SAYS IT CAN, HOWEVER HARD THEY FIGHT")
    good = ["slash", 92000, 500.0, 500.0, 520.0, 500.0, 0, 0]
    h, _ = await enter(port, account("presrh", "mage"), "town", x=500.0, y=500.0)
    i, _ = await enter(port, account("presri", "tank"), "town", x=520.0, y=500.0)
    j, _ = await enter(port, account("presrj"), "elusion", x=10.0, y=10.0)
    LEE = account("presl")
    MAY = account("presm", "mage")
    lee, welcome = await enter(port, LEE, "town", x=530.0, y=500.0, xt=1)
    check("the welcome says attacks can ride the tick (\"x\" %d)" % presence.RELAY_VERSION,
          welcome.get("x") == presence.RELAY_VERSION >= 2, welcome)
    await collect(h), await collect(i), await collect(j), await collect(lee)

    await h.send(json.dumps({"t": "x", "e": [good]}))
    got_lee, got_i, got_j = await collect(lee), await collect(i), await collect(j)
    check("a game that said \"xt\" hears an attack inside the moves, who made it named",
          ridden(got_lee) == [[uid("presrh"), [good]]] and not of(got_lee, "x"), got_lee)
    check("  even from somebody standing still: the frame's steps may be none",
          any(m.get("x") and not [e for e in m["p"] if e[0] == uid("presrh")] for m in of(got_lee, "moves")),
          of(got_lee, "moves"))
    check("  a game from before still hears it on its own, as it came",
          [m["e"] for m in of(got_i, "x")] == [[good]] and not ridden(got_i), got_i)
    check("  and another area hears neither", not of(got_j, "x") and not ridden(got_j), got_j)

    for n in range(3):
        await h.send(json.dumps({"t": "x", "e": [["slash", 93000 + n] + good[2:]]}))
    got_lee, got_i = await collect(lee), await collect(i)
    frames = [m for m in of(got_lee, "moves") if m.get("x")]
    check("three attacks inside one tick are one frame (or two, across a tick), in the order made",
          1 <= len(frames) <= 2 and [pair[1][0][1] for pair in ridden(got_lee)] == [93000, 93001, 93002], frames)
    check("  where a game from before had a frame for each", len(of(got_i, "x")) == 3, got_i)

    await lee.send(json.dumps({"t": "x", "e": [good]}))
    got_lee, got_h = await collect(lee), await collect(h)
    check("a rider's own attack, with nobody else riding, is not sent back to it at all",
          not ridden(got_lee) and not of(got_lee, "x"), got_lee)
    check("  and the games from before hear it as before", [m["id"] for m in of(got_h, "x")] == [uid("presl")], got_h)
    may, _ = await enter(port, MAY, "town", x=540.0, y=510.0, xt=1)
    await collect(lee), await collect(may), await collect(h), await collect(i)
    await lee.send(json.dumps({"t": "x", "e": [good]}))
    got_lee, got_may = await collect(lee), await collect(may)
    check("with another rider there, it hears it in the tick",
          ridden(got_may) == [[uid("presl"), [good]]], got_may)
    check("  and the maker's own comes back in the same frame, for it to pass over (as its own steps do)",
          ridden(got_lee) == [[uid("presl"), [good]]], got_lee)

    odd, _ = await enter(port, account("presn"), "town", x=545.0, y=505.0, xt=True)
    await collect(odd), await collect(h)
    await h.send(json.dumps({"t": "x", "e": [good]}))
    got_odd = await collect(odd)
    check("\"xt\": true is not the number it asks for - that game hears attacks as before",
          len(of(got_odd, "x")) == 1 and not ridden(got_odd), got_odd)
    await odd.close()

    server._tick_attacks["nowhere"] = [[uid("presrh"), [good]]]
    server.tick()
    check("attacks waiting for an area nobody is in any more go with it", "nowhere" not in server._tick_attacks,
          server._tick_attacks)
    await lee.close()
    await may.close()
    for ws in (h, i, j):
        await ws.close()

    # ---- ON THE BEAT ----
    beat = presence.PresenceServer(tick_seconds=0.05)
    stamps = []

    def slow_tick():
        stamps.append(time.monotonic())
        time.sleep(0.02)
    beat.tick = slow_tick
    task = asyncio.create_task(beat.tick_loop())
    await asyncio.sleep(1.0)
    task.cancel()
    check("a tick that takes time does not slow the beat: ~20 a second at a twentieth (it was 14)",
          len(stamps) >= 18, len(stamps))
    stamps.clear()

    def stall_once():
        stamps.append(time.monotonic())
        time.sleep(0.25 if len(stamps) == 1 else 0.0)
    beat.tick = stall_once
    task = asyncio.create_task(beat.tick_loop())
    await asyncio.sleep(0.6)
    task.cancel()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    check("  a stall is not made up for in a burst: the beat starts again from it",
          len(gaps) >= 4 and min(gaps[:4]) >= 0.035 and beat.late_ticks >= 1,
          ([round(g, 3) for g in gaps[:6]], beat.late_ticks))

    # ---- A BROWSER'S SOCKET IS COMPRESSED, at level 1 ----
    async def echo(ws):
        async for raw in ws:
            await ws.send(raw)
    async with serve(echo, "127.0.0.1", 0, extensions=[presence.DEFLATE]) as squeezing:
        squeeze_port = list(squeezing.sockets)[0].getsockname()[1]
        async with connect("ws://127.0.0.1:%d" % squeeze_port, compression="deflate") as browser:
            frame = json.dumps({"t": "moves", "p": [[n, 100.0 + n, 200.0, "walkdown", [], "", 1000 + n, 0, 100]
                                                     for n in range(64)]})
            await browser.send(frame)
            echoed = await asyncio.wait_for(browser.recv(), 2)
            agreed = [type(e).__name__ for e in browser.protocol.extensions]
    check("a browser's socket is still compressed - the two agree on it, and a frame comes back whole",
          agreed == ["PerMessageDeflate"] and echoed == frame, agreed)
    check("  squeezed at level %d, not zlib's default (269 us a 7 KB frame at 6, 78 at 1)" % presence.DEFLATE_LEVEL,
          presence.DEFLATE_LEVEL == 1 and presence.DEFLATE.compress_settings.get("level") == 1
          and "extensions=[DEFLATE]" in open(presence.__file__, encoding="utf-8").read())


async def main():
    await kills_written_soon()
    await positions_written()
    await health_written()
    presence.HELLO_SECONDS = 0.5
    combatbook.HOLD_SECONDS = 0.2
    combatbook.PENDING_SECONDS = 0.3
    combatbook.EMPTY_KEEP = 0.5
    server = presence.PresenceServer(sweep_seconds=0.3, tick_seconds=0.05)
    server.flush_seconds = 0.2
    server.position_sample_seconds = 0.1
    server.start_clocks()
    async with serve(server.handler, "127.0.0.1", 0, max_size=presence.MAX_SOCKET_BYTES) as ws_server:
        port = list(ws_server.sockets)[0].getsockname()[1]
        try:
            await run(port, server)
        finally:
            server.stop_clocks()
    counted_live(server)


asyncio.run(main())

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
if failures:
    for f in failures:
        print("  - " + f)
sys.exit(1 if failed else 0)
