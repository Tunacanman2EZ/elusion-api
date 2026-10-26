"""
test_schema.py - the database's shape, and who makes each part of it.

Since 19 Oct the CREATE for a table whose rows one area adds is in that
package's schema.py, and init_db() keeps the core's - what app.py writes
itself, and what more than one area adds rows to (ARCHITECTURE.md). The move
changed where the text lives and nothing else, and this suite is what says so,
then keeps it so:

  S-1  a fresh database has exactly the schema in test_schema.json - every
       column, index, key and stored CREATE - but saves.bank_gold, which is
       gone (UPGRADES.md item 7)
  S-2  a database built the old way, with rows in it, boots on this code: a
       character's banked gold moves to the account, the column goes, the
       ledger balances, and nothing else moves
  S-3  init_db() a second time changes nothing at all
  S-4  every table and every index is made by exactly one owner - the core,
       or one package's schema - found the code map's way, called in the
       order init_db() promises
  S-5  foreign_key_check and integrity_check are clean
  S-6  every column a package's upgrade() adds comes back, the same, on a
       table made before it
  S-7  item 7's way out: where DROP COLUMN cannot run, the column stays,
       zeroed, the boot goes on and the gold is not pooled twice - and no
       statement anywhere else names saves.bank_gold

THE FIXTURE, test_schema.json, is what a fresh database looked like when the
tree BEFORE the move (19 Oct) booted on an empty file. It was made with

    python test_schema.py --write-fixture <that tree's api folder>

and is made again the same way, from this tree, when the schema changes on
purpose - a new table, a new column, a changed default:

    python test_schema.py --write-fixture

READ ITS DIFF BEFORE COMMITTING IT: the fixture's diff is the schema change.
S-1 forgives saves.bank_gold and nothing else, so a fixture made before item 7
or after it serves the same, and S-2 puts the column back to play an old one.

Run:  python test_schema.py
Exits 0 if everything passes, 1 if anything fails.
"""

import ast
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testdir  # noqa: E402  (a private folder per run - testdir.py)

FIXTURE = os.path.join(HERE, "test_schema.json")


# =============================================================================
# READING A SCHEMA
# =============================================================================

def without_comments(sql):
    """A stored CREATE with its -- comments cut and its whitespace collapsed:
    the statement, not how it was laid out. No string literal in this schema
    holds a "--"."""
    if sql is None:
        return None
    return " ".join(" ".join(line.split("--", 1)[0] for line in sql.split("\n")).split())


def read_schema(path):
    """Everything SQLite says about a database's shape: sqlite_master in the
    order it was written, and per table the four PRAGMAs."""
    con = sqlite3.connect(path)
    try:
        objects = [list(r) for r in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid")]
        tables = {}
        for kind, name, _table, _sql in objects:
            if kind != "table":
                continue
            indexes = [list(r) for r in con.execute("PRAGMA index_list(%s)" % name)]
            tables[name] = {
                "columns": [list(r) for r in con.execute("PRAGMA table_info(%s)" % name)],
                # name, unique, origin, partial - not seq, which is only the
                # order SQLite happens to list them in
                "indexes": sorted(r[1:] for r in indexes),
                "index_info": {r[1]: [list(x) for x in con.execute("PRAGMA index_info(%s)" % r[1])]
                               for r in indexes},
                "foreign_keys": sorted(list(r) for r in con.execute("PRAGMA foreign_key_list(%s)" % name)),
            }
        return {"objects": objects, "tables": tables}
    finally:
        con.close()


def boot(folder, db_path):
    """A real boot of `folder`'s app.py on `db_path`, in a child process - what
    a server restart does: import app, which runs init_db() and every
    register()."""
    env = dict(os.environ, ELUSION_DB=db_path, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-c", "import app"], cwd=folder, env=env,
                          capture_output=True, text=True, timeout=300)


if "--write-fixture" in sys.argv:
    rest = sys.argv[sys.argv.index("--write-fixture") + 1:]
    folder = os.path.abspath(rest[0]) if rest else HERE
    made = os.path.join(testdir.PATH, "fixture.db")
    result = boot(folder, made)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        sys.exit("that tree's app.py did not boot")
    schema = read_schema(made)
    schema["sqlite"] = sqlite3.sqlite_version
    with open(FIXTURE, "w", encoding="utf-8") as handle:
        json.dump(schema, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print("wrote %s: %d objects, %d tables, from %s"
          % (FIXTURE, len(schema["objects"]), len(schema["tables"]), folder))
    sys.exit(0)


# =============================================================================
# HARNESS
# =============================================================================

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        failures.append((label, detail))
        print("  FAIL %s   %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


def schema_diff(got, want):
    """Every difference between two read_schema() results, one line each -
    empty when they are the same schema."""
    out = []
    got_objects = {(o[0], o[1]): o for o in got["objects"]}
    want_objects = {(o[0], o[1]): o for o in want["objects"]}
    for key in sorted(set(got_objects) | set(want_objects)):
        a, b = got_objects.get(key), want_objects.get(key)
        if a is None or b is None:
            out.append("%s %s: %s" % (key[0], key[1], "missing" if a is None else "not expected"))
        elif a[2] != b[2] or without_comments(a[3]) != without_comments(b[3]):
            out.append("%s %s: stored as %r, expected %r" % (key[0], key[1], without_comments(a[3]),
                                                             without_comments(b[3])))
    for table in sorted(set(got["tables"]) & set(want["tables"])):
        for part in ("columns", "indexes", "index_info", "foreign_keys"):
            if got["tables"][table][part] != want["tables"][table][part]:
                out.append("%s %s: %s, expected %s" % (table, part, got["tables"][table][part],
                                                       want["tables"][table][part]))
    return out


def minus_bank_gold(schema):
    """The fixture as item 7 leaves it: saves without bank_gold - the column
    out of the PRAGMA, the numbering closed up, the definition out of the
    stored CREATE. Nothing, on a fixture made after item 7."""
    schema = json.loads(json.dumps(schema))
    columns = schema["tables"]["saves"]["columns"]
    if any(c[1] == "bank_gold" for c in columns):
        kept = [c for c in columns if c[1] != "bank_gold"]
        schema["tables"]["saves"]["columns"] = [[i] + c[1:] for i, c in enumerate(kept)]
        for obj in schema["objects"]:
            if obj[0] == "table" and obj[1] == "saves":
                obj[3], n = re.subn(r"\n[ \t]*bank_gold\s+INTEGER NOT NULL DEFAULT 0,", "", obj[3])
                assert n == 1, "the fixture's saves names bank_gold in a way this does not know"
    return schema


def rows(path):
    """{table: sorted rows} for every table - the whole database's content."""
    con = sqlite3.connect(path)
    try:
        out = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cursor = con.execute("SELECT * FROM %s" % name)
            names = [d[0] for d in cursor.description]
            out[name] = sorted((dict(zip(names, r)) for r in cursor.fetchall()), key=repr)
        return out
    finally:
        con.close()


def one(path, sql, args=()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def ledger_balances(path):
    """SUM(gold_ledger.delta) == SUM(saves.gold) + SUM(accounts.bank_gold)."""
    minted = one(path, "SELECT COALESCE(SUM(delta), 0) FROM gold_ledger")
    held = (one(path, "SELECT COALESCE(SUM(gold), 0) FROM saves")
            + one(path, "SELECT COALESCE(SUM(bank_gold), 0) FROM accounts"))
    return minted == held, (minted, held)


def columns_of(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
    finally:
        con.close()


def clean(path):
    """(integrity_check, foreign_key_check rows) - ("ok", []) when sound."""
    con = sqlite3.connect(path)
    try:
        return (con.execute("PRAGMA integrity_check").fetchone()[0],
                con.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        con.close()


# =============================================================================
# THE OLD DATABASE, AND SOMETHING IN EVERY TABLE OF IT
# =============================================================================

# What a generic value would break: a CHECK on a column with no default.
GIVEN = {"trade_items": {"side": "a"}}


def build_old(path, fixture):
    """A database with the fixture's schema, statement for statement, as the
    tree before the move made it - and saves.bank_gold, put back if the
    fixture is from after item 7, so it is always a database from before."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    for kind, name, _table, sql in fixture["objects"]:
        if sql is not None and not name.startswith("sqlite_"):
            con.execute(sql)
    if "bank_gold" not in [r[1] for r in con.execute("PRAGMA table_info(saves)")]:
        con.execute("ALTER TABLE saves ADD COLUMN bank_gold INTEGER NOT NULL DEFAULT 0")
    con.commit()
    con.close()


def fill(path, xp_to_next, booted):
    """A row in every table, its foreign keys pointing at real rows, and an
    economy worth checking: alice has two characters and bob one, and both
    accounts bank some gold of their own (50 and 10).

    `booted`: the old code has booted this file - the case of the live server -
    so its pooling has already moved alice's characters' 550 onto her account
    and zeroed their save rows, and the gold ledger is open. Or it has not: the
    550 is still on the save rows (400 and 150) and there is no ledger yet,
    the only kind of database a character can still hold banked gold in.

    The boot's one-time seeds find their work done - the lusion ledger opened,
    the kill record's and the gifts ledger's dates set, xp_to_next on the
    curve - so what the boot changes is what this suite is about and nothing
    else."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    tables = [n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                        " AND name NOT LIKE 'sqlite_%'")]
    keys = {t: con.execute("PRAGMA foreign_key_list(%s)" % t).fetchall() for t in tables}
    order = []

    def parents_first(table):
        if table not in order:
            for key in keys[table]:
                if key[2] != table:
                    parents_first(key[2])
            order.append(table)

    for table in sorted(tables):
        parents_first(table)

    def insert(table, **given):
        cols = con.execute("PRAGMA table_info(%s)" % table).fetchall()
        rowid_key = sum(1 for c in cols if c[5]) == 1 and any(c[5] and c[2].upper() == "INTEGER" for c in cols)
        refs = {k[3]: (k[2], k[4]) for k in keys[table]}
        row = {}
        for _cid, name, kind, notnull, default, pk in cols:
            if name in given:
                row[name] = given[name]
            elif name in refs:
                parent, column = refs[name]
                row[name] = con.execute("SELECT %s FROM %s ORDER BY rowid LIMIT 1"
                                        % (column or "rowid", parent)).fetchone()[0]
            elif pk and rowid_key:
                continue
            elif (notnull and default is None) or pk:
                row[name] = {"INTEGER": 1, "REAL": 1.0, "BLOB": b"x"}.get(kind.upper(), "x")
        con.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(row), ", ".join("?" * len(row))),
                    list(row.values()))

    insert("users", id=1, username="alice")
    insert("users", id=2, username="bob")
    by_hand = {"users", "saves", "accounts", "gold_ledger", "lusion_ledger", "server_settings"}
    for table in order:
        if table not in by_hand:
            insert(table, **GIVEN.get(table, {}))
    for user_id, slot, gold, banked in ((1, 0, 120, 400), (1, 1, 30, 150), (2, 0, 75, 0)):
        insert("saves", user_id=user_id, slot=slot, gold=gold, bank_gold=0 if booted else banked,
               level=1, xp_to_next=xp_to_next)
    insert("accounts", user_id=1, lusions=3, bank_gold=600 if booted else 50)
    insert("accounts", user_id=2, bank_gold=10)
    insert("lusion_ledger", user_id=None, delta=3, reason="opening_balance")
    insert("server_settings", key="kill_tally_since", value="1")
    insert("server_settings", key="owner_gifts_since", value="1")      # _backfill_owner_gifts()
    if booted:
        supply = (con.execute("SELECT SUM(gold) FROM saves").fetchone()[0]
                  + con.execute("SELECT SUM(bank_gold) FROM accounts").fetchone()[0])
        insert("gold_ledger", user_id=None, delta=supply, reason="opening_balance")
    con.commit()
    con.close()


# =============================================================================
# THE FRESH DATABASE: this tree, imported the way every suite imports it
# =============================================================================

FRESH = os.path.join(testdir.PATH, "elusion_schema_fresh.db")
os.environ["ELUSION_DB"] = FRESH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
CURVE_1 = app_module.gamedata.xp_needed_for_level(1)


def init_db_on(path):
    """init_db() alone, on another file - the second run of a restart."""
    saved = app_module.DB_PATH
    app_module.DB_PATH = path
    try:
        app_module.init_db()
    finally:
        app_module.DB_PATH = saved


with open(FIXTURE, encoding="utf-8") as _handle:
    fixture = json.load(_handle)
expected = minus_bank_gold(fixture)
fresh = read_schema(FRESH)


# =============================================================================
section("S-1  A FRESH DATABASE IS THE FIXTURE'S, BUT FOR saves.bank_gold")
# =============================================================================
check("the fixture has something in it",
      len(fixture["tables"]) >= 40 and len(fixture["objects"]) >= 100,
      (len(fixture["tables"]), len(fixture["objects"])))
check("saves.bank_gold is not in a fresh database (UPGRADES.md item 7)",
      "bank_gold" not in columns_of(FRESH, "saves"), columns_of(FRESH, "saves"))
check("...and accounts.bank_gold, where the bank's gold lives, is",
      "bank_gold" in columns_of(FRESH, "accounts"))
check("the same tables, indexes and trigger, by name",
      sorted((o[0], o[1]) for o in fresh["objects"]) == sorted((o[0], o[1]) for o in expected["objects"]),
      sorted(set((o[0], o[1]) for o in fresh["objects"]) ^ set((o[0], o[1]) for o in expected["objects"])))
for part, label in (("columns", "every column: name, type, NOT NULL, default, key"),
                    ("indexes", "every index on every table: unique, origin, partial"),
                    ("index_info", "every index's columns, in order"),
                    ("foreign_keys", "every foreign key, and what it does on delete")):
    differing = [t for t in sorted(expected["tables"])
                 if fresh["tables"].get(t, {}).get(part) != expected["tables"][t][part]]
    check(label, not differing,
          ["%s: %s != %s" % (t, fresh["tables"].get(t, {}).get(part), expected["tables"][t][part])
           for t in differing][:3])
# What no PRAGMA shows: a CHECK, a COLLATE, a partial index's WHERE, a trigger's
# body. The stored CREATE has them, so it is compared too - without its
# comments, which are prose and not schema.
check("every stored CREATE says the same, comments aside (CHECK, COLLATE, WHERE, the trigger)",
      schema_diff(fresh, expected) == [], schema_diff(fresh, expected)[:3])


# =============================================================================
section("S-2  AN OLD DATABASE, WITH ROWS, BOOTS ON THIS CODE")
# =============================================================================
# TWO OF THEM (fill() has the economy): the one the live server has, booted by
# the old code, and the one only a never-booted file can be, with a
# character's banked gold still on its save row.
BOOTED = os.path.join(testdir.PATH, "elusion_schema_booted.db")
BEFORE = os.path.join(testdir.PATH, "elusion_schema_before.db")
build_old(BOOTED, fixture)
fill(BOOTED, CURVE_1, booted=True)
build_old(BEFORE, fixture)
fill(BEFORE, CURVE_1, booted=False)

booted_before, before_before = rows(BOOTED), rows(BEFORE)
check("the old databases are the old shape: saves.bank_gold, and a row in every table",
      "bank_gold" in columns_of(BEFORE, "saves")
      and all(before_before[t] for t in before_before if t not in ("gold_ledger", "sqlite_sequence")),
      [t for t in before_before if not before_before[t]])
check("...the one from before holds 550 banked on alice's two saves",
      one(BEFORE, "SELECT SUM(bank_gold) FROM saves") == 550)
check("...and its foreign keys point at rows that exist", clean(BEFORE) == ("ok", []), clean(BEFORE))


def saves_without_bank_gold(table_rows):
    return sorted(({k: v for k, v in r.items() if k != "bank_gold"} for r in table_rows), key=repr)


for label, path, before in (("booted by the old code", BOOTED, booted_before),
                            ("from before the ledger", BEFORE, before_before)):
    result = boot(HERE, path)
    check("%s: it boots" % label, result.returncode == 0, (result.stdout + result.stderr)[-600:])
    after = rows(path)
    check("%s: saves.bank_gold is gone" % label, "bank_gold" not in columns_of(path, "saves"))
    check("%s: every character is otherwise as it was" % label,
          after["saves"] == saves_without_bank_gold(before["saves"]))
    accounts = {r["user_id"]: r["bank_gold"] for r in after["accounts"]}
    check("%s: alice's account holds her 50 and the 550 her characters banked; bob's his 10" % label,
          accounts == {1: 600, 2: 10}, accounts)
    check("%s: the gold ledger balances" % label, ledger_balances(path)[0], ledger_balances(path)[1])
    if path == BOOTED:
        check("%s: the ledger is as it was - nothing minted, nothing written" % label,
              after["gold_ledger"] == before["gold_ledger"])
    else:
        opened = [r for r in after["gold_ledger"] if r["reason"] == "opening_balance"]
        check("%s: the ledger opens once, counting the pooled gold (225 carried + 610 banked)" % label,
              len(after["gold_ledger"]) == 1 and len(opened) == 1 and opened[0]["delta"] == 835,
              after["gold_ledger"])
    # sqlite_sequence too, but for the ledger's own counter where it opened
    sequence = [r for r in after.get("sqlite_sequence", []) if r["name"] != "gold_ledger" or path == BOOTED]
    after["sqlite_sequence"] = sequence
    # A column a migration adds (combat_kills.death, 0.86.1) is new, not a
    # row touched: compare what each row held before, column by column.
    def held(table, got):
        if not before[table]:
            return got
        keys = set(before[table][0])
        return sorted(({k: v for k, v in r.items() if k in keys} for r in got), key=repr)
    moved = [t for t in before if t not in ("saves", "accounts", "gold_ledger")
             and held(t, after.get(t) or []) != before[t]]
    check("%s: every other table's rows are untouched" % label, not moved, moved)
    check("%s: and its schema is now a fresh database's" % label,
          schema_diff(read_schema(path), fresh) == [], schema_diff(read_schema(path), fresh)[:3])


# =============================================================================
section("S-3  init_db() AGAIN CHANGES NOTHING")
# =============================================================================
for label, path in (("a fresh database", FRESH), ("an upgraded one", BEFORE), ("the live shape", BOOTED)):
    schema_was, rows_was = read_schema(path), rows(path)
    init_db_on(path)
    check("%s: the schema, the same" % label, read_schema(path) == schema_was,
          schema_diff(read_schema(path), schema_was)[:3])
    check("%s: every row, the same" % label, rows(path) == rows_was,
          [t for t in rows_was if rows(path).get(t) != rows_was[t]])


# =============================================================================
section("S-4  EVERY TABLE AND INDEX HAS EXACTLY ONE OWNER")
# =============================================================================
# THE CORE IS app.py; A PACKAGE IS A FOLDER app.py IMPORTS WITH A routes.py -
# codemap.py's rule, so a package added next month is read here without a
# change. Owners are found where the statements are written, not in a list.
import codemap  # noqa: E402

PACKAGES = [name for name, _rows, _layers in codemap.feature_packages()]
TABLE_AT = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(")
INDEX_AT = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)\s+ON\s+(\w+)")


def source_of(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


APP_SRC = source_of(os.path.join(HERE, "app.py"))
writers = {"app.py": APP_SRC}
for _package in PACKAGES:
    for _name in sorted(os.listdir(os.path.join(HERE, _package))):
        if _name.endswith(".py"):
            writers["%s/%s" % (_package, _name)] = source_of(os.path.join(HERE, _package, _name))

made_by = {}
index_on = {}
for where, text in writers.items():
    for m in TABLE_AT.finditer(text):
        made_by.setdefault(m.group(1), set()).add(where)
    for m in INDEX_AT.finditer(text):
        made_by.setdefault(m.group(1), set()).add(where)
        index_on[m.group(1)] = m.group(2)


def owner(where):
    return "core" if where == "app.py" else where.split("/")[0]


fresh_tables = sorted(o[1] for o in fresh["objects"] if o[0] == "table" and not o[1].startswith("sqlite_"))
fresh_indexes = sorted(o[1] for o in fresh["objects"] if o[0] == "index" and o[3] is not None)
check("the code map finds the packages", len(PACKAGES) >= 20, PACKAGES)
check("every table in a fresh database is made by exactly one file",
      all(len(made_by.get(t, ())) == 1 for t in fresh_tables),
      {t: sorted(made_by.get(t, ())) for t in fresh_tables if len(made_by.get(t, ())) != 1})
check("every index too", all(len(made_by.get(i, ())) == 1 for i in fresh_indexes),
      {i: sorted(made_by.get(i, ())) for i in fresh_indexes if len(made_by.get(i, ())) != 1})
check("an index belongs to its table's owner",
      all(owner(next(iter(made_by[i]))) == owner(next(iter(made_by[index_on[i]]))) for i in fresh_indexes
          if len(made_by.get(i, ())) == 1 and len(made_by.get(index_on.get(i), ())) == 1),
      [i for i in fresh_indexes if i in index_on and index_on[i] in made_by
       and owner(next(iter(made_by[i]))) != owner(next(iter(made_by[index_on[i]])))])
check("nothing is made that a fresh database does not have (a schema nobody calls)",
      not set(t for t in made_by if t not in index_on) - set(fresh_tables),
      sorted(set(t for t in made_by if t not in index_on) - set(fresh_tables)))
owned = {}
for t in fresh_tables:
    owned.setdefault(owner(next(iter(made_by.get(t, {"?"})))), []).append(t)
print("  the owners: " + "; ".join("%s %s" % (k, ", ".join(v)) for k, v in sorted(owned.items())))

# THE ORDER init_db() PROMISES, read off its source: each package with a
# schema.py created once, every create() before the first migration, and an
# upgrade() only after its own create().
INIT_SRC = APP_SRC[APP_SRC.index("\ndef init_db():"):]
INIT_SRC = INIT_SRC[:INIT_SRC.index("\n    db.close()\n")]
WITH_SCHEMA = sorted(p for p in PACKAGES if os.path.exists(os.path.join(HERE, p, "schema.py")))
first_migration = INIT_SRC.index("_migrate_bank_to_account(db)")
create_at = {p: [m.start() for m in re.finditer(r"\b%s_schema\.create\(db\)" % p, INIT_SRC)]
             for p in WITH_SCHEMA}
check("every package's schema.py is created by init_db(), once",
      all(len(v) == 1 for v in create_at.values()), {p: len(v) for p, v in create_at.items() if len(v) != 1})
check("...after the core's block and before the first migration",
      all(v and INIT_SRC.index('        """\n    )\n') < v[0] < first_migration for v in create_at.values()),
      [p for p, v in create_at.items() if not v or not v[0] < first_migration])
check("...and each upgrade() comes after every create()",
      all(m.start() > max(v[0] for v in create_at.values() if v)
          for m in re.finditer(r"\b\w+_schema\.upgrade\(", INIT_SRC)),
      [m.group(0) for m in re.finditer(r"\b\w+_schema\.upgrade\(", INIT_SRC)])

# THE RULE THE OWNERS WERE CHOSEN BY: a package's table gets its rows from that
# package alone, and the core writes it nowhere but a boot migration. A write
# that breaks it is not wrong in itself - it says the table has become shared,
# and its CREATE belongs in init_db() with the others.
BOOT_ONLY = re.compile(r"^(init_db|_migrate_\w+)$")


def statements(text):
    """[(line, SQL)] for every string in `text` that is a statement: adjacent
    literals joined, as Python joins them; comments and prose left out."""
    out, run = [], []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.STRING:
            run.append(tok)
            continue
        if tok.type in (tokenize.NL, tokenize.COMMENT) and run:
            continue
        if run:
            joined = " ".join(re.sub(r'^[A-Za-z]*("""|\'\'\'|"|\')|("""|\'\'\'|"|\')$', "", t.string) for t in run)
            flat = " ".join(joined.split())
            if re.match(r"(SELECT|INSERT|REPLACE|UPDATE|DELETE|ALTER|WITH)\b", flat):
                out.append((run[0].start[0], flat))
            run = []
    return out


def boot_function_lines(text):
    tree = ast.parse(text)
    return [(n.lineno, n.end_lineno) for n in tree.body
            if isinstance(n, ast.FunctionDef) and BOOT_ONLY.match(n.name)]


APP_BOOT = boot_function_lines(APP_SRC)
package_table = {t: owner(next(iter(made_by[t]))) for t in fresh_tables
                 if len(made_by.get(t, ())) == 1 and owner(next(iter(made_by[t]))) != "core"}
server_code = dict(writers)
server_code["presence.py"] = source_of(os.path.join(HERE, "presence.py"))
crossings = []
for where, text in sorted(server_code.items()):
    for line, sql in statements(text):
        adds = re.match(r"(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+(\w+)", sql)
        writes = re.match(r"(?:UPDATE|DELETE\s+FROM)\s+(\w+)", sql)
        table = (adds or writes).group(1) if (adds or writes) else None
        home = package_table.get(table)
        if home is None or where.split("/")[0] == home:
            continue
        at_boot = where == "app.py" and any(a <= line <= b for a, b in APP_BOOT)
        if where == "app.py" and not at_boot:
            crossings.append("%s:%d writes %s/'s %s" % (where, line, home, table))
        elif adds and where != "app.py":
            crossings.append("%s:%d adds rows to %s/'s %s" % (where, line, home, table))
check("a package's table gets its rows from that package, and the core writes it only at boot",
      not crossings, crossings[:5])
check("...a check that read some statements", sum(len(statements(t)) for t in server_code.values()) > 300)


# =============================================================================
section("S-5  INTEGRITY AND FOREIGN KEYS")
# =============================================================================
for label, path in (("a fresh database", FRESH), ("the live shape, upgraded", BOOTED),
                    ("one from before the ledger, upgraded", BEFORE)):
    integrity, orphans = clean(path)
    check("%s: integrity_check says ok" % label, integrity == "ok", integrity)
    check("%s: foreign_key_check finds nothing" % label, orphans == [], orphans[:5])


# =============================================================================
section("S-6  A PACKAGE'S upgrade() BRINGS ITS COLUMNS BACK")
# =============================================================================
# Each column an upgrade() adds is taken off an old database, and the boot must
# put it back with the definition a fresh database has. A column an index in
# SCHEMA names cannot be missing on a database that boots - the index is made
# before any upgrade() runs, in the old code too - so those stay.
LATE = os.path.join(testdir.PATH, "elusion_schema_late.db")
build_old(LATE, fixture)
fill(LATE, CURVE_1, booted=True)
added, dropped_indexes, superseded = [], [], []
scratch = os.path.join(testdir.PATH, "elusion_schema_scratch.db")
shutil.copy(FRESH, scratch)
for package in WITH_SCHEMA:
    module = importlib.import_module("%s.schema" % package)
    if not hasattr(module, "upgrade"):
        continue
    calls = []
    _con = sqlite3.connect(scratch)
    module.upgrade(_con, add_column=lambda _db, table, column, definition: calls.append((table, column)))
    _con.close()
    up_src = source_of(os.path.join(HERE, package, "schema.py"))
    superseded += [(package, m.group(1)) for m in re.finditer(r'DROP INDEX IF EXISTS (\w+)', up_src)]
    for table, column in calls:
        needed = [i for i in fresh_indexes if index_on.get(i) == table
                  and column in [r[2] for r in fresh["tables"][table]["index_info"][i]]]
        if any(re.search(r"INDEX IF NOT EXISTS %s\b" % i, module.SCHEMA) for i in needed):
            continue
        added.append((package, table, column))
        dropped_indexes += needed
check("the packages' upgrade()s add columns this can take away",
      len(added) >= 6, added)
_con = sqlite3.connect(LATE)
for index in dropped_indexes:
    _con.execute("DROP INDEX %s" % index)
for _package, table, column in added:
    _con.execute("ALTER TABLE %s DROP COLUMN %s" % (table, column))
# A superseded index, put back under its old name on one of the package's
# tables: the name is what upgrade()'s DROP INDEX looks for.
for package, index in superseded:
    table = next(t for t in fresh_tables if package_table.get(t) == package)
    _con.execute("CREATE INDEX %s ON %s(%s)" % (index, table, fresh["tables"][table]["columns"][0][1]))
_con.commit()
_con.close()
init_db_on(LATE)
late = read_schema(LATE)
for package, table, column in added:
    want = [c[1:] for c in fresh["tables"][table]["columns"] if c[1] == column]
    got = [c[1:] for c in late["tables"][table]["columns"] if c[1] == column]
    check("%s/: %s.%s comes back as a fresh database has it" % (package, table, column),
          got == want and want != [], (got, want))
for index in dropped_indexes:
    check("...and the index on it, %s" % index, index in [o[1] for o in late["objects"]])
for package, index in superseded:
    check("%s/: the superseded %s goes" % (package, index), index not in [o[1] for o in late["objects"]])
# The tables that lost and regained a column hold it last now, and their
# stored CREATE says so - that much differs, by construction. Nothing else may.
touched = sorted(set(t for _p, t, _c in added))
others = [d for d in schema_diff(late, fresh)
          if not any(d.startswith("table %s:" % t) or d.startswith("%s columns:" % t) for t in touched)]
check("and nothing else about the schema differs", others == [], others[:3])
check("its rows are sound", clean(LATE) == ("ok", []), clean(LATE))


# =============================================================================
section("S-7  ITEM 7'S WAY OUT, AND NOBODY ELSE READS THE COLUMN")
# =============================================================================
# DROP COLUMN refuses a column an index names - which makes the build that
# cannot drop it easy to play: index it, and boot. The column must stay,
# zeroed, the boot must go on, and a second boot must not pool it again.
STUCK = os.path.join(testdir.PATH, "elusion_schema_stuck.db")
build_old(STUCK, fixture)
fill(STUCK, CURVE_1, booted=False)
_con = sqlite3.connect(STUCK)
_con.execute("CREATE INDEX idx_test_holds_bank_gold ON saves(bank_gold)")
_con.commit()
_con.close()
result = boot(HERE, STUCK)
check("a database where the column cannot go still boots", result.returncode == 0,
      (result.stdout + result.stderr)[-600:])
check("...and says why it kept it", "[migrate] saves.bank_gold left in place, zeroed" in result.stdout,
      result.stdout[-400:])
check("...the column is there, zeroed",
      "bank_gold" in columns_of(STUCK, "saves") and one(STUCK, "SELECT SUM(bank_gold) FROM saves") == 0)
check("...the gold is on the account, once", one(STUCK, "SELECT bank_gold FROM accounts WHERE user_id = 1") == 600)
result = boot(HERE, STUCK)
check("a second boot pools nothing twice",
      result.returncode == 0 and one(STUCK, "SELECT bank_gold FROM accounts WHERE user_id = 1") == 600)
check("...and the ledger balances", ledger_balances(STUCK)[0], ledger_balances(STUCK)[1])

# THE SERVER'S CODE, AND THE TOOLS BESIDE IT: no statement names saves and
# bank_gold together outside the migration that empties and drops it.
POOL = [(n.lineno, n.end_lineno) for n in ast.parse(APP_SRC).body
        if isinstance(n, ast.FunctionDef) and n.name == "_migrate_bank_to_account"]
everything = dict(server_code)
for _name in sorted(os.listdir(HERE)):
    if _name.endswith(".py") and not _name.startswith("test_") and _name not in everything:
        everything[_name] = source_of(os.path.join(HERE, _name))
naming = ["%s:%d %s" % (where, line, sql[:90]) for where, text in sorted(everything.items())
          for line, sql in statements(text)
          if "bank_gold" in sql and re.search(r"\bsaves\b", sql)
          and not (where == "app.py" and any(a <= line <= b for a, b in POOL))]
check("nothing outside the migration names saves.bank_gold", not naming, naming)


print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailures:")
    for label, detail in failures:
        print("  %s   %s" % (label, detail))
sys.exit(1 if failed else 0)
