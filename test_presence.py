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


async def enter(port, headers, area=None, **kw):
    """A connected game: hello with a fresh ticket, welcome read, optionally
    standing somewhere."""
    raw, _ = ticket(headers)
    ws = await connect("ws://127.0.0.1:%d/ws/presence" % port)
    await ws.send(json.dumps({"t": "hello", "ticket": raw}))
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
    check("a real ticket is welcomed, by account id", welcome == {"t": "welcome", "id": uid("presa")}, welcome)
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
    async with serve(server.handler, "127.0.0.1", 0, max_size=presence.MAX_MESSAGE_BYTES) as ws_server:
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
