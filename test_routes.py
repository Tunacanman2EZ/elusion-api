"""
The route table, pinned. Run: python3 test_routes.py
                    After a deliberate change: python3 test_routes.py --write

app.py is being taken apart into feature packages (ARCHITECTURE.md), one
area at a time, and the promise of every move is that NOTHING OUTSIDE
CHANGES: the game asks the same paths with the same methods and is answered
by the same rules. This suite is how that is known rather than hoped.

routes.lock holds one line per route the server answers:

    GET /api/kills  kills_mine  auth  rank=-  fresh=-  lock=yes  doc=yes

  RT-1  THE TABLE IS THE LOCK'S. Every method, path and endpoint name - the
        name rate limiting and request timing key on - and what the
        decorators say about each: whether it needs a token (require_auth),
        the lowest rank it lets in (require_role, require_owner), whether it
        asks for the password again (require_fresh_password), whether it
        takes the write lock (no_write_lock), and whether it has a docstring
        for /apidocs. A route gone, added, renamed or loosened is a
        difference here, named line by line.
  RT-2  "auth" IS TRUE. Every route the lock says needs a token answers 401
        to a request without one - so a package handed the wrong decorator
        cannot pass RT-1 on a tag alone.
  RT-3  THE LOCK IS ONE LINE A ROUTE, sorted, no route twice: a hand edit
        that leaves it ragged is caught too.

A change to the table that is meant - a new route, a route moved on purpose -
is written with --write, and the diff of routes.lock in the commit says what
changed for the game.
"""
import importlib.util, os, re, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "routes.lock")
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_routes_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)


def load_app(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app_module = load_app("elusion_app")
app = app_module.app

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


def route_lines():
    """The table as it is, one line a route and method, sorted."""
    out = []
    for rule in app.url_map.iter_rules():
        view = app.view_functions.get(rule.endpoint)
        for method in sorted(m for m in rule.methods if m in METHODS):
            out.append("%s %s  %s  %s  rank=%s  fresh=%s  lock=%s  doc=%s" % (
                method, rule.rule, rule.endpoint,
                "auth" if getattr(view, "_elusion_needs_auth", False) else "open",
                getattr(view, "_elusion_min_role", None) or "-",
                "yes" if getattr(view, "_elusion_fresh_password", False) else "-",
                "no" if getattr(view, "_elusion_no_write_lock", False) else "yes",
                "yes" if (getattr(view, "__doc__", None) or "").strip() else "no"))
    return sorted(out)


now_lines = route_lines()
if "--write" in sys.argv:
    with open(LOCK, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# The route table - test_routes.py. Written with --write; a diff here is a\n"
                     "# change the game can see. One line a route and method:\n"
                     "# METHOD PATH  ENDPOINT  auth|open  rank=  fresh=  lock=  doc=\n")
        handle.write("\n".join(now_lines) + "\n")
    print("wrote %s: %d routes" % (LOCK, len(now_lines)))
    sys.exit(0)

with open(LOCK, encoding="utf-8") as handle:
    locked = [line.rstrip("\n") for line in handle if line.strip() and not line.startswith("#")]

# =============================================================================
section("RT-1  the table is the lock's")
gone = [line for line in locked if line not in now_lines]
added = [line for line in now_lines if line not in locked]
check("%d routes, each as routes.lock has it" % len(locked), not gone and not added,
      "\n        gone or changed:\n          " + "\n          ".join(gone or ["-"])
      + "\n        new or changed:\n          " + "\n          ".join(added or ["-"]))
pairs = [(line.split()[0], line.split()[1]) for line in now_lines]
check("  and no path answers one method twice - the second rule would never be reached",
      len(set(pairs)) == len(pairs), sorted({p for p in pairs if pairs.count(p) > 1}))

# =============================================================================
section("RT-2  \"auth\" is true")
client = app.test_client()
leaks = []
asked = 0
for line in now_lines:
    method, path, _endpoint, auth = line.split()[:4]
    if auth != "auth":
        continue
    concrete = re.sub(r"<(?:int:)?[^>]+>", lambda m: "1" if m.group(0).startswith("<int:") else "x", path)
    res = client.open(concrete, method=method, environ_base={"REMOTE_ADDR": "10.9.%d.%d" % (asked // 200, asked % 200)})
    asked += 1
    if res.status_code != 401:
        leaks.append("%s %s -> %d" % (method, path, res.status_code))
check("every one of the %d routes that needs a token answers 401 without one" % asked, leaks == [] and asked > 50,
      leaks[:8])

# =============================================================================
section("RT-3  the lock is one line a route")
check("sorted, and no line twice", locked == sorted(locked) and len(set(locked)) == len(locked))
check("every line has its eight fields", all(len(line.split()) == 8 for line in locked),
      [line for line in locked if len(line.split()) != 8][:3])

print("\n=== %d passed, %d failed ===" % (passed, failed))
sys.exit(1 if failed else 0)
