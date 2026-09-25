"""
test_chatrooms.py - channels, and pictures posted by link.

    venv\\Scripts\\python.exe test_chatrooms.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db. Never reaches the internet either: the one test that needs
a web server starts one on loopback and tears it down again.

WHAT THIS IS GUARDING, in order of how bad it would be to get wrong:

  - A PRIVATE MESSAGE IS PRIVATE. Nobody but the two accounts named can read a
    line from the private channel, whatever they ask for.
  - THE RELAY CANNOT BE POINTED AT THIS MACHINE. "Post a picture" must not be
    a way to make the server read 127.0.0.1, a router, or a metadata service -
    including through a redirect, which is the trick that gets forgotten.
  - WHAT COMES BACK IS REALLY A PICTURE. Bytes that merely claim to be an
    image never reach a client.
  - The friends channel follows the friends table, and stops following it the
    moment somebody is removed.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import io
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_chatrooms_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "ROOMOWNER"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

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


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}
                       ).get_json()["token"]


def say(token, body, channel=None, to=None, image=None):
    payload = {"body": body}
    if channel is not None:
        payload["channel"] = channel
    if to is not None:
        payload["to"] = to
    if image is not None:
        payload["image"] = image
    return client.post("/api/chat/send", json=payload, headers=auth(token))


def read(token, channel=None, with_name=None, since=None):
    url = "/api/chat"
    bits = []
    if channel is not None:
        bits.append("channel=%s" % channel)
    if with_name is not None:
        bits.append("with=%s" % with_name)
    if since is not None:
        bits.append("since=%s" % since)
    if bits:
        url += "?" + "&".join(bits)
    return client.get(url, headers=auth(token))


def bodies(res):
    return [m["body"] for m in res.get_json()["messages"]]


def refill(*names, world=True):
    """Put a test account back to having spent nothing.

    `world=False` leaves the half-hour world-picture clock alone. Only the
    tests for that limit pass it: everywhere else, "refill" means every
    throttle, and a helper that quietly left one of them running is how an
    unrelated test starts failing when a new limit is added.
    """
    with app_module.app.app_context():
        db = app_module.get_db()
        for name in names:
            db.execute("UPDATE users SET chat_tokens = ?, last_chat_at = 0,"
                       " last_image_at = 0 WHERE username = ?",
                       (app_module.CHAT_BUCKET_CAPACITY, name))
            if world:
                db.execute("UPDATE users SET last_world_image_at = 0"
                           " WHERE username = ?", (name,))
        db.commit()


print("\n--- setup ---")
owner_token = register("roomowner")
amy_token = register("amy")
bob_token = register("bob")
cal_token = register("cal")
check("accounts created", all([owner_token, amy_token, bob_token, cal_token]))

# amy and bob are friends; cal is nobody's friend.
client.post("/api/friends/request", json={"username": "bob"}, headers=auth(amy_token))
client.post("/api/friends/respond", json={"username": "amy", "accept": True},
            headers=auth(bob_token))
check("amy and bob are friends",
      len(client.get("/api/friends", headers=auth(amy_token)).get_json()["friends"]) == 1)


print("\n--- the world channel still behaves ---")
refill("amy")
check("a line with no channel goes to the world",
      say(amy_token, "hello world").get_json()["channel"] == "world")
check("and everyone sees it", "hello world" in bodies(read(cal_token)))
check("an unknown channel is refused",
      say(amy_token, "hi", channel="dungeon").status_code == 400)
check("and so is reading one", read(amy_token, channel="dungeon").status_code == 400)


print("\n--- private messages are private ---")
refill("amy", "bob", "cal")
sent = say(amy_token, "just between us", channel="private", to="bob")
check("a private line is accepted", sent.status_code == 200, sent.status_code)
check("and says who it went to", sent.get_json()["to"] == "bob", sent.get_json())

check("the recipient can read it",
      "just between us" in bodies(read(bob_token, "private", with_name="amy")))
check("and so can the sender",
      "just between us" in bodies(read(amy_token, "private", with_name="bob")))

# THE ONE THAT MATTERS. cal asks for the conversation every way there is.
check("a third party reading amy's side sees nothing",
      bodies(read(cal_token, "private", with_name="amy")) == [])
check("nor bob's side", bodies(read(cal_token, "private", with_name="bob")) == [])
check("and it does not leak into the world channel",
      "just between us" not in bodies(read(cal_token)))
check("nor into the friends channel",
      "just between us" not in bodies(read(bob_token, "friends")))

check("a private line needs somebody to send it to",
      say(amy_token, "hi", channel="private").status_code == 400)
check("and you cannot message yourself",
      say(amy_token, "hi", channel="private", to="amy").status_code == 400)
check("messaging a name that does not exist is a 404",
      say(amy_token, "hi", channel="private", to="nobody").status_code == 404)
check("reading a private channel needs a 'with'",
      read(amy_token, "private").status_code == 400)
check("and a real account to read with",
      read(amy_token, "private", with_name="nobody").status_code == 404)

refill("amy", "bob")
say(amy_token, "second one", channel="private", to="bob")
pair = read(bob_token, "private", with_name="amy").get_json()
check("the conversation reads in order",
      [m["body"] for m in pair["messages"]] == ["just between us", "second one"],
      pair["messages"])
check("and names who it is with", pair.get("with") == "amy", pair)


print("\n--- the friends channel follows the friends table ---")
refill("amy", "bob", "cal")
say(amy_token, "hello friends", channel="friends")
check("a friend sees it", "hello friends" in bodies(read(bob_token, "friends")))
check("the author sees it", "hello friends" in bodies(read(amy_token, "friends")))
check("a stranger does not", "hello friends" not in bodies(read(cal_token, "friends")))

refill("cal")
say(cal_token, "cal talking to nobody", channel="friends")
check("and a stranger's line does not reach them either",
      "cal talking to nobody" not in bodies(read(amy_token, "friends")))
check("though it is there for its own author",
      "cal talking to nobody" in bodies(read(cal_token, "friends")))

# REMOVED MEANS REMOVED, RETROACTIVELY. The audience is worked out when the
# channel is read, not when the line was written.
client.post("/api/friends/remove", json={"username": "bob"}, headers=auth(amy_token))
check("removing a friend hides what they already said",
      "hello friends" not in bodies(read(bob_token, "friends")))


# THIS SECTION USED TO ASSERT THAT GUILDS DID NOT EXIST. They do now, and the
# channel's rule changed with them: it is no longer "nobody may use this", it
# is "you need a guild". What has NOT changed, and is the part worth keeping,
# is that somebody who cannot use the channel is told why rather than shown an
# empty room they have to guess the meaning of.
#
# The guild rules themselves - who reads whose lines, and the leak when
# somebody moves between guilds - live in test_guilds.py, next to the routes
# that make them.
print("\n--- the guild channel needs a guild ---")
answer = read(amy_token, "guild").get_json()
check("reading it works", answer["messages"] == [], answer)
check("and says it is not available to you", answer.get("available") is False,
      answer)
check("with something the client can show",
      "guild" in str(answer.get("notice", "")).lower(), answer)
check("writing to it without one is refused",
      say(amy_token, "anyone there?", channel="guild").status_code == 409)
refused = say(amy_token, "anyone?", channel="guild")
check("writing to it is a 409, not an error", refused.status_code == 409,
      refused.status_code)


print("\n--- each channel has its own cursor ---")
refill("amy", "bob")
world_head = read(amy_token).get_json()["latest_id"]
say(amy_token, "private only", channel="private", to="bob")
after = read(amy_token, since=world_head).get_json()
check("a private line does not move the world cursor",
      after["messages"] == [], after["messages"])
check("and the world cursor does not jump past it",
      after["latest_id"] == world_head, (world_head, after["latest_id"]))


print("\n--- the tail is kept, the rest is dropped ---")
real_keep = app_module.CHAT_KEEP_PER_CHANNEL
app_module.CHAT_KEEP_PER_CHANNEL = 5
refill("amy")
for n in range(8):
    refill("amy")
    say(amy_token, "line %d" % n)
with app_module.app.app_context():
    left = app_module.get_db().execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE channel = 'world'").fetchone()["n"]
check("the world channel is trimmed to its cap", left == 5, left)
kept = bodies(read(amy_token))
check("and what is kept is the NEWEST, not the oldest",
      kept == ["line 3", "line 4", "line 5", "line 6", "line 7"], kept)
app_module.CHAT_KEEP_PER_CHANNEL = real_keep


print("\n--- the relay refuses to look inside this network ---")
# EVERY ONE OF THESE IS A REAL TARGET. Loopback is the server's own API,
# 169.254.169.254 is the cloud metadata service that hands out credentials,
# and the 10/192.168 ranges are whatever else is on the machine's LAN.
for url, why in [
    ("http://127.0.0.1:5000/api/status", "loopback"),
    ("http://localhost/secret.png", "localhost by name"),
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
    ("http://10.0.0.5/private.png", "a private range"),
    ("http://192.168.1.1/router.png", "a home router"),
    ("http://[::1]/x.png", "loopback over IPv6"),
]:
    refill("amy")
    res = client.post("/api/chat/image", json={"url": url}, headers=auth(amy_token))
    check("refused: %s" % why, res.status_code == 400, res.status_code)

for url, why in [
    ("file:///etc/passwd", "a local file"),
    ("ftp://example.com/x.png", "a non-web scheme"),
    ("gopher://example.com/", "gopher, of all things"),
    ("not a url at all", "nonsense"),
]:
    refill("amy")
    res = client.post("/api/chat/image", json={"url": url}, headers=auth(amy_token))
    check("refused: %s" % why, res.status_code == 400, res.status_code)

refill("amy")
check("an empty url is refused",
      client.post("/api/chat/image", json={}, headers=auth(amy_token)).status_code == 400)


# ---------------------------------------------------------------------------
# A REAL SERVER, ON LOOPBACK, FOR THE FETCH TESTS.
#
# The relay refuses loopback on purpose, so these tests patch the address check
# to allow it - and ONLY here. That is the honest way to test a fetch without
# reaching the internet: the guard stays exactly as it ships, and the test
# says out loud that it is stepping around it.
# ---------------------------------------------------------------------------
PNG_BODY = None
GIF_BODY = None
if app_module.PILLOW_AVAILABLE:
    from PIL import Image as _Image
    buf = io.BytesIO()
    _Image.new("RGB", (400, 250), (40, 90, 140)).save(buf, format="PNG")
    PNG_BODY = buf.getvalue()
    frames = [_Image.new("RGB", (60, 40), c) for c in
              [(200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40)]]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=80, loop=0)
    GIF_BODY = buf.getvalue()

    # SEPARATE BYTES FOR THE UPLOAD TESTS. An id is the hash of the re-encoded
    # copy, so uploading PNG_BODY would hit the row the relay tests already
    # created and inherit its `source` - which would make the filename check
    # below fail for a reason that has nothing to do with uploading. Different
    # pixels, different id, its own row.
    buf = io.BytesIO()
    _Image.new("RGB", (400, 250), (150, 60, 30)).save(buf, format="PNG")
    UPLOAD_PNG = buf.getvalue()
    up_frames = [_Image.new("RGB", (60, 40), c) for c in
                 [(10, 10, 10), (90, 90, 90), (170, 170, 170)]]
    buf = io.BytesIO()
    up_frames[0].save(buf, format="GIF", save_all=True, append_images=up_frames[1:],
                      duration=120, loop=0)
    UPLOAD_GIF = buf.getvalue()


class _Quiet(HTTPServer):
    """
    Swallows the broken-pipe noise from the size-cap test.

    The relay stops reading at IMAGE_MAX_BYTES and hangs up, which is exactly
    what it should do - and which makes http.server print a ConnectionReset
    traceback in the middle of a passing suite. That traceback is the guard
    working; it is not a failure, and it is not worth looking like one.
    """

    def handle_error(self, request, client_address):
        pass


class _Serve(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/pic.png":
            self._send(200, "image/png", PNG_BODY)
        elif self.path == "/anim.gif":
            self._send(200, "image/gif", GIF_BODY)
        elif self.path == "/notanimage":
            self._send(200, "text/html", b"<html>nope</html>")
        elif self.path == "/liar.png":
            # Claims to be a PNG and is not. The decode is what catches this.
            self._send(200, "image/png", b"MZ\x90\x00 this is an executable")
        elif self.path == "/huge.png":
            self._send(200, "image/png", b"\x00" * (app_module.IMAGE_MAX_BYTES + 512))
        elif self.path == "/inward":
            # The trick that gets forgotten: an allowed host redirecting home.
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret.png")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"no")

    def _send(self, code, kind, body):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


print("\n--- relaying a real picture ---")
if not app_module.PILLOW_AVAILABLE:
    check("Pillow is installed so pictures can be tested", False,
          "run: pip install Pillow")
else:
    httpd = _Quiet(("127.0.0.1", 0), _Serve)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    real_check = app_module._address_is_public
    app_module._address_is_public = lambda host: True     # ONLY for these tests

    refill("amy")
    res = client.post("/api/chat/image", json={"url": base + "/pic.png"},
                      headers=auth(amy_token))
    check("a PNG relays", res.status_code == 200, res.get_json())
    still = res.get_json() if res.status_code == 200 else {}
    check("and is stored as a still", still.get("kind") == "still", still)
    check("with an id that is the hash of the bytes",
          len(str(still.get("id", ""))) == 64, still)
    check("scaled inside the ceiling",
          still.get("width", 9999) <= app_module.IMAGE_MAX_SIDE, still)

    fetched = client.get("/api/chat/image/%s" % still["id"], headers=auth(amy_token))
    check("the stored picture can be read back", fetched.status_code == 200)
    # PNG OR WebP, and the header has to agree with the bytes.
    #
    # A still is kept in whichever of the two is smaller, so asserting PNG
    # here would be asserting a detail of one picture rather than the rule.
    # What matters, and what this checks, is that what comes back is a real
    # picture this server wrote - never the bytes that were handed to it -
    # and that a client reading Content-Type is told the truth.
    served = fetched.headers.get("Content-Type")
    check("as a picture", served in ("image/png", "image/webp"), served)
    is_png = fetched.data[:4] == b"\x89PNG"
    is_webp = fetched.data[:4] == b"RIFF" and fetched.data[8:12] == b"WEBP"
    check("and it is a picture, not the original bytes", is_png or is_webp,
          fetched.data[:12])
    check("with a Content-Type that matches the bytes",
          (served == "image/png") == is_png, "%s vs %s" % (served, fetched.data[:4]))
    check("reading one needs a token",
          client.get("/api/chat/image/%s" % still["id"]).status_code == 401)
    check("a made-up id is a 404",
          client.get("/api/chat/image/%s" % ("0" * 64),
                     headers=auth(amy_token)).status_code == 404)
    check("and so is a malformed one",
          client.get("/api/chat/image/..%2f..%2fapp.py",
                     headers=auth(amy_token)).status_code == 404)

    print("\n--- a GIF becomes something Godot can play ---")
    refill("amy")
    res = client.post("/api/chat/image", json={"url": base + "/anim.gif"},
                      headers=auth(amy_token))
    check("a GIF relays", res.status_code == 200, res.get_json())
    anim = res.get_json() if res.status_code == 200 else {}
    check("and comes back animated", anim.get("kind") == "animated", anim)
    check("with every frame", anim.get("frames") == 4, anim)
    check("laid out in a grid", anim.get("columns", 0) >= 1, anim)
    check("and a frame time to play it at", anim.get("frame_ms", 0) > 0, anim)
    sheet = client.get("/api/chat/image/%s" % anim["id"], headers=auth(amy_token))
    check("the spritesheet is one PNG", sheet.data[:4] == b"\x89PNG")

    print("\n--- what the relay will not accept ---")
    for path, why in [
        ("/notanimage", "a page that is not an image"),
        ("/liar.png", "bytes that only claim to be a PNG"),
        ("/huge.png", "something over the size cap"),
        ("/inward", "a redirect pointing back inside"),
        ("/missing.png", "a link that 404s"),
    ]:
        refill("amy")
        # The redirect case has to face the real guard, or it proves nothing.
        if path == "/inward":
            app_module._address_is_public = real_check
        res = client.post("/api/chat/image", json={"url": base + path},
                          headers=auth(amy_token))
        check("refused: %s" % why, res.status_code == 400, res.status_code)
        app_module._address_is_public = lambda host: True

    print("\n--- posting one into a channel ---")
    refill("amy")
    res = say(amy_token, "look at this", image=still["id"])
    check("a message can carry a picture", res.status_code == 200, res.get_json())
    line = [m for m in read(bob_token).get_json()["messages"]
            if m["body"] == "look at this"]
    check("and the picture id reaches everyone", line and line[0]["image"] == still["id"],
          line)
    feed = read(bob_token).get_json()
    check("along with how to draw it", still["id"] in feed.get("images", {}), feed.keys())
    check("which says whether it moves",
          feed["images"][still["id"]]["kind"] == "still")

    refill("amy")
    res = say(amy_token, "", image=still["id"])
    check("a picture on its own is a message", res.status_code == 200, res.get_json())

    refill("amy")
    check("but a made-up picture id is refused",
          say(amy_token, "hi", image="f" * 64).status_code == 400)
    refill("amy")
    check("and an empty message with no picture still is",
          say(amy_token, "   ").status_code == 400)

    print("\n--- the relay has a cooldown of its own ---")
    refill("amy")
    first = client.post("/api/chat/image", json={"url": base + "/pic.png"},
                        headers=auth(amy_token))
    second = client.post("/api/chat/image", json={"url": base + "/pic.png"},
                         headers=auth(amy_token))
    check("the first goes through", first.status_code == 200)
    check("the second is made to wait", second.status_code == 429, second.status_code)
    check("and is told how long", int(second.headers.get("Retry-After", 0)) >= 1)

    print("\n--- the same picture twice is stored once ---")
    refill("amy")
    again = client.post("/api/chat/image", json={"url": base + "/pic.png"},
                        headers=auth(amy_token))
    check("it relays again", again.status_code == 200)
    check("with the same id", again.get_json()["id"] == still["id"])
    with app_module.app.app_context():
        rows = app_module.get_db().execute(
            "SELECT COUNT(*) AS n FROM chat_images WHERE id = ?",
            (still["id"],)).fetchone()["n"]
    check("and one row, not two", rows == 1, rows)

    app_module._address_is_public = real_check
    httpd.shutdown()

check("the guard is back to how it ships",
      app_module._address_is_public("127.0.0.1") is False)


# ---------------------------------------------------------------------------
# UPLOADING A FILE, which is the door most pictures will actually come in by.
#
# THE POINT OF THESE: the upload route skips _fetch_image_bytes entirely, so
# every guard the link relay gets for free from that function has to be
# re-proved here. The bytes arrive already inside the building. What must
# still hold is that they are verified as a picture, re-encoded rather than
# stored as sent, capped, cooled down, and refused to anybody without a token.
# ---------------------------------------------------------------------------
print("\n--- uploading a file ---")

def upload(token, body, name=None):
    headers = auth(token)
    headers["Content-Type"] = "application/octet-stream"
    if name is not None:
        headers["X-Picture-Name"] = name
    return client.post("/api/chat/upload", data=body, headers=headers)

# THE ROUTE IS NOT SHADOWED BY THE ONE THAT SERVES PICTURES.
#
# This lived at /api/chat/image/upload and was captured by
# /api/chat/image/<image_id> on any server that did not yet have it - "upload"
# is a fine value for image_id, that rule is GET only, and the answer was 405
# Method Not Allowed for a route that did not exist. Moving it out from under
# that prefix is the fix; these two checks are what stop it drifting back.
print("\n--- the upload route stands on its own ---")
_rules = {str(r): sorted(r.methods - {"HEAD", "OPTIONS"})
          for r in app_module.app.url_map.iter_rules()}
check("POST /api/chat/upload exists",
      _rules.get("/api/chat/upload") == ["POST"], _rules.get("/api/chat/upload"))
# THE PATH THE GAME POSTS TO is what matters, not whether the old one still
# exists - it does, on purpose, as an alias. What must stay true is that the
# game's own path sits outside /api/chat/image/, so a server too old to have
# this route answers 404 ("restart it") rather than 405 ("method not allowed"),
# which names nothing.
check("the path the game uses is outside the picture-serving prefix",
      not "/api/chat/upload".startswith("/api/chat/image/"))
check("so an older server cannot capture it with the image-id rule",
      "/api/chat/upload".count("/") == 3, "/api/chat/upload")
check("a POST there is never answered 405",
      client.post("/api/chat/upload", data=b"x",
                  headers={"Content-Type": "application/octet-stream"}
                  ).status_code != 405)
# The old path stays wired so a game build from before the move still works
# against a current server. It is only the STALE-server case the move fixes.
check("the old path still reaches the same handler",
      _rules.get("/api/chat/image/upload") == ["POST"],
      _rules.get("/api/chat/image/upload"))
check("and a POST there is not 405 either",
      client.post("/api/chat/image/upload", data=b"x",
                  headers={"Content-Type": "application/octet-stream"}
                  ).status_code != 405)

check("uploading needs a token",
      client.post("/api/chat/upload", data=b"x").status_code == 401)

if not app_module.PILLOW_AVAILABLE:
    print("  (Pillow not installed - upload tests skipped)")
else:
    refill("amy", "bob")

    res = upload(amy_token, UPLOAD_PNG, "holiday snap.png")
    check("a PNG uploads", res.status_code == 200, res.get_json())
    shot = res.get_json() or {}
    check("and comes back a still", shot.get("kind") == "still", shot)
    check("with its size", int(shot.get("width", 0)) == 400, shot)

    # THE SAME BYTES BY EITHER DOOR MAKE THE SAME PICTURE. The id is the hash
    # of what the server re-encoded, so an uploaded file and the identical
    # file fetched from a link have to land on one row rather than two.
    check("the id is the hash of the stored copy, not of what was sent",
          shot.get("id") != __import__("hashlib").sha256(UPLOAD_PNG).hexdigest(), shot)

    with app_module.app.app_context():
        source = app_module.get_db().execute(
            "SELECT source FROM chat_images WHERE id = ?", (shot["id"],)).fetchone()["source"]
    check("the filename is kept as a label", source == "upload:holiday snap.png", source)

    refill("amy")
    res = upload(amy_token, UPLOAD_GIF, "wave.gif")
    check("an animated GIF uploads", res.status_code == 200, res.get_json())
    anim = res.get_json() or {}
    check("and comes back animated", anim.get("kind") == "animated", anim)
    check("with every frame", int(anim.get("frames", 0)) == 3, anim)

    # A NAME IS A LABEL AND NOTHING ELSE. Anything that could be read as a
    # path is stripped before it is written down, and the bytes decide the
    # format regardless of what the name claims.
    refill("amy")
    from PIL import Image as _Img
    _buf = io.BytesIO()
    _Img.new("RGB", (120, 90), (7, 130, 77)).save(_buf, format="PNG")
    res = upload(amy_token, _buf.getvalue(), "../../../etc/passwd.png")
    check("a filename that looks like a path is scrubbed", res.status_code == 200)
    with app_module.app.app_context():
        source = app_module.get_db().execute(
            "SELECT source FROM chat_images WHERE id = ?",
            (res.get_json()["id"],)).fetchone()["source"]
    check("with no traversal left in it",
          ".." not in source and "/" not in source, source)

    refill("amy")
    check("an empty body is refused", upload(amy_token, b"").status_code == 400)

    refill("amy")
    res = upload(amy_token, b"MZ\x90\x00 this is an executable", "sneaky.png")
    check("bytes that are not a picture are refused", res.status_code == 400,
          res.get_json())

    refill("amy")
    res = upload(amy_token, b"\x00" * (app_module.IMAGE_MAX_BYTES + 1), "huge.png")
    check("over the byte cap is refused",
          res.status_code in (400, 413), res.status_code)

    # THE COOLDOWN IS ONE COOLDOWN. Uploading has to consume the same budget
    # the link relay does, or "one picture every eight seconds" becomes two.
    refill("amy")
    # Deliberately bytes already in the store: the cooldown must bite on a
    # picture the server already has, not only on new ones.
    check("the first upload passes", upload(amy_token, UPLOAD_PNG).status_code == 200)
    check("a second straight after is refused",
          upload(amy_token, UPLOAD_PNG).status_code == 429)
    check("and so is a link, from the same budget",
          client.post("/api/chat/image", json={"url": "http://x/y.png"},
                      headers=auth(amy_token)).status_code == 429)

    # AND IT IS POSTABLE, with a caption, which is the whole point.
    refill("amy")
    _buf = io.BytesIO()
    _Img.new("RGB", (200, 150), (222, 111, 0)).save(_buf, format="PNG")
    shot = upload(amy_token, _buf.getvalue(), "pic.png").get_json()
    res = say(amy_token, "look what I found", image=shot["id"])
    check("a picture posts with a caption", res.status_code == 200, res.get_json())
    feed = read(bob_token).get_json()
    line = [m for m in feed.get("messages", []) if m.get("image") == shot["id"]]
    check("and everyone gets both",
          len(line) == 1 and line[0]["body"] == "look what I found", line)


# ---------------------------------------------------------------------------
# STILLS ARE KEPT IN WHICHEVER FORMAT IS SMALLER
#
# A photograph re-encoded as a PNG is several times larger than it needs to be,
# and the cost of that is not storage - it is that EVERY person in the channel
# downloads it. An animation stays PNG on purpose: its frames are capped at
# 256px and are usually flat or pixel art, which lossy compression is worst at.
# ---------------------------------------------------------------------------
print("\n--- how a picture is kept ---")

if not app_module.PILLOW_AVAILABLE:
    print("  (Pillow not installed - format tests skipped)")
else:
    from PIL import Image as _PImage, features as _features
    refill("amy")

    noisy = _PImage.new("RGB", (600, 400))
    px = noisy.load()
    seed = 12345
    for y in range(400):
        for x in range(600):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            px[x, y] = (seed & 255, (seed >> 8) & 255, (seed >> 16) & 255)
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    photo_bytes = buf.getvalue()

    res = upload(amy_token, photo_bytes, "photo.png")
    check("a noisy still uploads", res.status_code == 200, res.get_json())
    if res.status_code == 200:
        shot_id = res.get_json()["id"]
        with app_module.app.app_context():
            row = app_module.get_db().execute(
                "SELECT format, bytes FROM chat_images WHERE id = ?",
                (shot_id,)).fetchone()
        if _features.check("webp"):
            check("it is kept as WebP, because WebP is smaller here",
                  row["format"] == "webp", row["format"])
            check("and that really is smaller than the PNG it came from",
                  row["bytes"] < len(photo_bytes),
                  "%d stored vs %d sent" % (row["bytes"], len(photo_bytes)))
        else:
            check("no WebP in this Pillow, so PNG stands",
                  row["format"] == "png", row["format"])

    refill("amy")
    res = upload(amy_token, UPLOAD_GIF, "anim.gif")
    if res.status_code == 200:
        with app_module.app.app_context():
            row = app_module.get_db().execute(
                "SELECT format FROM chat_images WHERE id = ?",
                (res.get_json()["id"],)).fetchone()
        check("an animation is always kept as PNG", row["format"] == "png",
              row["format"])


# ---------------------------------------------------------------------------
# ONE PICTURE IN WORLD CHAT PER HALF HOUR
#
# A DIFFERENT LIMIT FROM THE UPLOAD COOLDOWN, and these tests exist to keep
# them apart. The upload cooldown is about server load and bites on the act of
# handing over a file. This one is about the room - world chat is every player
# at once - and bites only when a picture actually reaches world.
#
# THE FOUR THINGS THAT MUST HOLD:
#   - the second world picture inside the window is refused, and says when
#   - whispering or posting to friends does NOT spend it, and is not spent BY it
#   - a plain line of text in world is never affected
#   - the owner is exempt, and nobody else is - not mod, not dev
# ---------------------------------------------------------------------------
print("\n--- one picture in world per half hour ---")

if not app_module.PILLOW_AVAILABLE:
    print("  (Pillow not installed - world limit tests skipped)")
else:
    def a_picture(token, tint):
        """A fresh picture each time, so dedup never masks a refusal."""
        from PIL import Image as _I
        b = io.BytesIO()
        _I.new("RGB", (80, 60), tint).save(b, format="PNG")
        # world=False: this helper is used INSIDE the world-limit tests, and
        # resetting the very clock under test would make all of them pass.
        refill("amy", "bob", "roomowner", world=False)
        res = upload(token, b.getvalue(), "p.png")
        assert res.status_code == 200, res.get_json()
        return res.get_json()["id"]

    def clear_world_clock(*names):
        with app_module.app.app_context():
            db = app_module.get_db()
            for n in names:
                db.execute("UPDATE users SET last_world_image_at = 0"
                           " WHERE username = ?", (n,))
            db.commit()

    # The earlier sections posted world pictures of their own, so start from a
    # known clock rather than from whatever they left behind.

    clear_world_clock("amy", "bob")

    first = say(amy_token, "", channel="world", image=a_picture(amy_token, (10, 90, 10)))
    check("the first world picture goes through", first.status_code == 200,
          first.get_json())

    second = say(amy_token, "", channel="world", image=a_picture(amy_token, (90, 10, 10)))
    check("the second is refused", second.status_code == 429, second.status_code)
    check("and says how long", "minutes" in str(second.get_json().get("message", "")),
          second.get_json())
    check("with a Retry-After a client can use",
          int(second.headers.get("Retry-After", 0)) > 0,
          second.headers.get("Retry-After"))

    # THE REFUSAL COSTS NOTHING ELSE. Being told to wait must not also eat the
    # ordinary chat allowance, or one blocked picture silences you for a bit.
    words = say(amy_token, "but I can still talk", channel="world")
    check("words in world still work", words.status_code == 200, words.get_json())

    # OTHER CHANNELS ARE UNTOUCHED.
    whisper = say(amy_token, "", channel="private", to="bob",
                  image=a_picture(amy_token, (10, 10, 90)))
    check("a whispered picture is not blocked by it", whisper.status_code == 200,
          whisper.get_json())

    clear_world_clock("amy")
    friendly = say(amy_token, "", channel="friends",
                   image=a_picture(amy_token, (90, 90, 10)))
    check("a friends picture is not blocked either", friendly.status_code == 200,
          friendly.get_json())
    check("and posting to friends did NOT start the world clock",
          say(amy_token, "", channel="world",
              image=a_picture(amy_token, (40, 40, 40))).status_code == 200)

    # ONE PERSON'S CLOCK IS THEIR OWN.
    clear_world_clock("bob")
    check("somebody else is unaffected",
          say(bob_token, "", channel="world",
              image=a_picture(bob_token, (20, 60, 120))).status_code == 200)

    # THE OWNER IS EXEMPT, AND ONLY THE OWNER.
    check("the owner posts one", say(owner_token, "", channel="world",
          image=a_picture(owner_token, (120, 20, 60))).status_code == 200)
    check("and another straight after", say(owner_token, "", channel="world",
          image=a_picture(owner_token, (60, 120, 20))).status_code == 200)

    app_module.get_db  # keep the import obvious
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE users SET role = 'mod', last_world_image_at = 0"
                   " WHERE username = ?", ("bob",))
        db.commit()
    check("a MOD gets one", say(bob_token, "", channel="world",
          image=a_picture(bob_token, (11, 22, 33))).status_code == 200)
    check("and a mod waits like everyone else", say(bob_token, "", channel="world",
          image=a_picture(bob_token, (33, 22, 11))).status_code == 429)
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE users SET role = 'player' WHERE username = ?", ("bob",))
        db.commit()

    # THE POLL SAYS SO IN ADVANCE, which is what stops somebody spending an
    # upload to find out.
    feed = read(amy_token).get_json()
    check("the poll reports the wait", int(feed.get("world_image_wait", -1)) > 0,
          feed.get("world_image_wait"))
    clear_world_clock("amy")
    feed = read(amy_token).get_json()
    check("and reports zero once it is clear",
          int(feed.get("world_image_wait", -1)) == 0, feed.get("world_image_wait"))
    check("the owner is always told zero",
          int(read(owner_token).get_json().get("world_image_wait", -1)) == 0)


print("\n--- nothing without a token ---")
check("relaying needs one",
      client.post("/api/chat/image", json={"url": "http://x/y.png"}).status_code == 401)
check("reading a channel needs one",
      client.get("/api/chat?channel=private&with=amy").status_code == 401)


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
