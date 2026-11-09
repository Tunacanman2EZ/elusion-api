"""
test_poll_parts.py - the broadcast poll gives up a part, never the answer.

    venv\\Scripts\\python.exe test_poll_parts.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING (game 0.134.6, app.py A POLL THAT ALWAYS ANSWERS).
GET /api/server/broadcasts carries a dozen parts - the lands, events,
teleport, guild, party, trade, chat news, asks, reports - and it is also the
heartbeat: a game whose poll keeps failing signs its player out at 90 s. So a
bug in any one part answered 500 and would have signed out every player
online. Each part that is not the heartbeat is now built by _poll_part():

  - one that raises is left out and named in "missing"; the poll answers 200
  - the parts that did not fail are all still there
  - a part that wrote before it failed has its writes rolled back
  - the failure is logged, with its name, for the owner to find
  - with nothing failing there is no "missing" at all

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_poll_parts_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "CHECKER"

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
        print("  FAIL  %s  %s" % (label, detail))


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def poll(token):
    return client.get("/api/server/broadcasts?since=0", headers=auth(token))


class Caught(logging.Handler):
    """Every [POLL] line the app logs while it is attached."""
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def boom(*_args, **_kwargs):
    raise RuntimeError("a bug in one part")


def broadcast_count():
    with app_module.app.app_context():
        return app_module.get_db().execute("SELECT COUNT(*) AS n FROM broadcasts").fetchone()["n"]


# =============================================================================
print("\n--- setup ---")
token = client.post("/api/auth/register",
                    json={"username": "someplayer", "password": "password123"}).get_json()["token"]
check("an account to poll with", bool(token))
caught = Caught()
app_module.app.logger.addHandler(caught)


# =============================================================================
print("\n--- nothing failing ---")
answer = poll(token)
body = answer.get_json() or {}
check("the poll answers 200", answer.status_code == 200, answer.status_code)
check("  with no \"missing\" when nothing failed", "missing" not in body, body.get("missing"))
for key in ("messages", "latest_id", "maintenance", "world", "events", "party", "trade",
            "chat_news", "asks", "guild", "open_reports"):
    check("  and its %s" % key, key in body, sorted(body.keys()))


# =============================================================================
print("\n--- one part fails ---")
real_chat_news = app_module._chat_news
app_module._chat_news = boom
answer = poll(token)
body = answer.get_json() or {}
app_module._chat_news = real_chat_news
check("a broken chat news still answers 200 - the heartbeat holds", answer.status_code == 200, answer.status_code)
check("  and names it in \"missing\"", body.get("missing") == ["chat_news"], body.get("missing"))
check("  its own field is empty", body.get("chat_news") is None, body.get("chat_news"))
check("  and every other part is still there",
      all(k in body for k in ("messages", "latest_id", "maintenance", "world", "party", "trade", "guild")),
      sorted(body.keys()))
check("  and it was logged by name for the owner to find",
      any("[POLL] chat_news failed" in line for line in caught.lines), caught.lines)


# =============================================================================
print("\n--- a part that wrote before it failed ---")
real_world = app_module.world_public


def writes_then_breaks(db):
    db.execute("INSERT INTO broadcasts (kind, body, created_by, created_at) VALUES ('system', 'half done', '', 0)")
    raise RuntimeError("a bug after a write")


before = broadcast_count()
app_module.world_public = writes_then_breaks
answer = poll(token)
body = answer.get_json() or {}
app_module.world_public = real_world
check("the lands failing half way: still 200, \"world\" missing",
      answer.status_code == 200 and body.get("missing") == ["world"], (answer.status_code, body.get("missing")))
check("  and what it wrote was rolled back, not committed with the beat",
      broadcast_count() == before, (before, broadcast_count()))


# =============================================================================
print("\n--- parts spread into the answer, and two at once ---")
real_guild = app_module._own_guild
real_trade = app_module._trade_summary
app_module._own_guild = boom
app_module._trade_summary = boom
answer = poll(token)
body = answer.get_json() or {}
app_module._own_guild = real_guild
app_module._trade_summary = real_trade
check("guild and trade both failing: still 200, both named",
      answer.status_code == 200 and sorted(body.get("missing", [])) == ["guild", "trade"],
      (answer.status_code, body.get("missing")))
check("  the guild's fields are left out, not sent as \"no guild\"",
      "guild" not in body and "guild_tag" not in body, sorted(body.keys()))

answer = poll(token)
body = answer.get_json() or {}
check("mended, the next poll is whole again", answer.status_code == 200 and "missing" not in body,
      body.get("missing"))


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
