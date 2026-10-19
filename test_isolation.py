"""Every suite runs against a throwaway database - checked, not trusted.

THE RULE WAS WRITTEN DOWN AND NOTHING HELD ANYONE TO IT. app.py reads
ELUSION_DB once, at import, and falls back to elusion.db beside the code - the
owner's own copy, real-looking accounts and password hashes. run_tests.ps1
explains why no suite opens it: each one sets ELUSION_DB to a file in its own
temp folder (testdir.py) BEFORE it loads app.py. All of them did. A new suite
that forgot would have opened elusion.db and said nothing. (The owner's lesson,
19 Oct: "rules live as tests, not as comments".)

  I-1  every suite that loads app.py in its own process sets ELUSION_DB first
  I-2  and to a throwaway place: testdir.PATH or the temp folder, never a path
       beside the code
  I-3  no suite names elusion.db beside the code at all
  I-4  the checker itself: a suite that loads app.py first, or sets nothing, or
       points at the real file, is caught - so a green I-1..I-3 means the rule
       held, not that the check could not see

Reads the suites' source and nothing else; no database, no app.
Run: python test_isolation.py
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(__file__)

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


# HOW A SUITE LOADS THE APP IN ITS OWN PROCESS: `import app` / `from app import`
# at the start of a line, or importlib's spec_from_file_location(..., "app.py"),
# over as many lines as its arguments take. Code inside a triple-quoted string
# is not run here: test_deploy.py boots a COPY of the code in a subprocess with
# a .env of its own (and checks no elusion.db appears beside the copy), so its
# probe's `import app` is blanked out before the search (_code_only()).
LOADS = re.compile(r"^[ \t]*(?:import app\b|from app\b)|spec_from_file_location\(\s*[^)]*?[\"']app\.py[\"']",
                   re.M)
SETS = re.compile(r"^[ \t]*os\.environ\[[\"']ELUSION_DB[\"']\][ \t]*=[ \t]*(?P<value>[^\n#]+)", re.M)
THROWAWAY = ("testdir.PATH", "tempfile", "mkdtemp", "gettempdir")
REAL_FILE = re.compile(r"join\(\s*(?:HERE|os\.path\.dirname\([^)]*\))\s*,\s*[\"']elusion\.db[\"']")


def _code_only(source):
    """The source with every triple-quoted string's insides blanked - the same
    length and the same lines, so positions still compare."""
    return re.sub(r"(?s)(\"\"\"|\'\'\')(.*?)\1",
                  lambda m: m.group(1) + re.sub(r"[^\n]", " ", m.group(2)) + m.group(1), source)


def judge(source):
    """What the rule says about one suite's source: None when it does not load
    app.py in its own process, else "" (it holds) or what is wrong with it."""
    source = _code_only(source)
    load = LOADS.search(source)
    if load is None:
        return None
    setting = SETS.search(source)
    if setting is None:
        return "loads app.py without setting ELUSION_DB"
    if setting.start() > load.start():
        return "loads app.py before it sets ELUSION_DB"
    value = setting.group("value").strip()
    if not any(mark in value for mark in THROWAWAY):
        named = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
        defined = None
        if named:
            # `NAME = ...`, or one name of several: `_fd, _db = tempfile.mkstemp()`.
            other = r"[A-Za-z_][A-Za-z0-9_]*"
            for found in re.finditer(r"^[ \t]*(?:%s[ \t]*,[ \t]*)*%s(?:[ \t]*,[ \t]*%s)*[ \t]*=[ \t]*([^\n#]+)"
                                     % (other, re.escape(value), other), source, re.M):
                if found.start() < setting.start():
                    defined = found.group(1)
        if defined is None or not any(mark in defined for mark in THROWAWAY):
            return "sets ELUSION_DB to %s, which is not in testdir.PATH or the temp folder" % value
    return ""


suites = sorted(p for p in glob.glob(os.path.join(HERE, "test_*.py")) if os.path.basename(p) != SELF)
verdicts = {}
for path in suites:
    with open(path, encoding="utf-8") as fh:
        verdicts[os.path.basename(path)] = (judge(fh.read()), path)

loading = {name: v for name, (v, _p) in verdicts.items() if v is not None}
others = sorted(name for name, (v, _p) in verdicts.items() if v is None)

# =============================================================================
print("\n--- I-1, I-2 every suite that loads the app, on a throwaway database ---")
# =============================================================================
check("the suites that load app.py are found - the check is looking at something (%d of %d)"
      % (len(loading), len(suites)), len(loading) >= 50, sorted(loading))
first = sorted("%s: %s" % (name, v) for name, v in loading.items() if v.startswith("loads"))
check("every one sets ELUSION_DB before it loads app.py", not first, first)
where = sorted("%s: %s" % (name, v) for name, v in loading.items() if v.startswith("sets"))
check("  and to a file in testdir.PATH or the temp folder", not where, where)
print("  (%d suites do not load the app in their own process: %s)" % (len(others), ", ".join(others)))

# =============================================================================
print("\n--- I-3 nobody names the real file ---")
# =============================================================================
naming = []
for path in suites:
    with open(path, encoding="utf-8") as fh:
        if REAL_FILE.search(fh.read()):
            naming.append(os.path.basename(path))
check("no suite joins elusion.db onto the code's own folder", not naming, naming)

# =============================================================================
print("\n--- I-4 the checker catches what it is for ---")
# =============================================================================
GOOD = ('import testdir\nDB_PATH = os.path.join(testdir.PATH, "x.db")\nos.environ["ELUSION_DB"] = DB_PATH\n'
        'spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))\n')
LATE = ('spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))\n'
        'os.environ["ELUSION_DB"] = os.path.join(testdir.PATH, "x.db")\n')
NONE = 'import app\n'
SPLIT = ('_fd, _db = tempfile.mkstemp(suffix=".db")\nos.environ["ELUSION_DB"] = _db\n'
         'spec = importlib.util.spec_from_file_location(\n    "elusion_app", os.path.join(HERE, "app.py"))\n')
QUOTED = 'PROBE = """\nimport app\n"""\n'
REAL = 'DB_PATH = os.path.join(HERE, "elusion.db")\nos.environ["ELUSION_DB"] = DB_PATH\nimport app\n'
check("a suite that sets a throwaway database first holds", judge(GOOD) == "", judge(GOOD))
check("  one that loads the app first is caught", (judge(LATE) or "").startswith("loads app.py before"), judge(LATE))
check("  one that sets nothing is caught", (judge(NONE) or "").startswith("loads app.py without"), judge(NONE))
check("  one that points at the real file is caught", (judge(REAL) or "").startswith("sets ELUSION_DB to DB_PATH"),
      judge(REAL))
check("  and one that never loads the app is left out", judge("import gamedata\n") is None)
check("  a load whose arguments run onto the next line is still a load, and a temp file from mkstemp is throwaway",
      judge(SPLIT) == "" and (judge(SPLIT.split("\n", 2)[2]) or "").startswith("loads app.py without"), judge(SPLIT))
check("  code inside a string is not a load - a subprocess's probe has its own environment",
      judge(QUOTED) is None, judge(QUOTED))
check("  and naming the real file is seen", REAL_FILE.search(REAL) is not None)


# =============================================================================
print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
