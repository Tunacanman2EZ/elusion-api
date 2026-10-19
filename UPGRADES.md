# Upgrades noticed along the way

18 Oct. The owner, while app.py is being taken apart: "if anything needs
upgraded along the way keep track so i know". This is that list. Each item was
checked before it was written here, and each says where it is, why it matters,
and how big the fix is. The architecture moves themselves are not listed:
those are ARCHITECTURE.md's plan.

When one is fixed, it moves to **Done** at the bottom with the date. New ones
go at the end of **Open**.

## Open

### 6. The test runs could now go in parallel

- **Where:** run_tests.ps1, and the CI workflow in the API repo's .github
  folder. Both run the suites one after another.
- **What changed:** since item 2 every suite keeps its databases in a private
  folder (testdir.py), so two suites - or two copies of one - can run at the
  same time without touching each other.
- **Why it matters:** the full API run is about ten minutes one at a time; four
  or so at once would take a few.
- **Fix:** small, but in two files this session has not been able to run (a
  PowerShell script, and CI). Worth doing with a run on the PC to watch it.

### 7. `saves.bank_gold` is a leftover column

- **Where:** the saves table in init_db().
- **What happens:** the bank moved to `accounts` long ago. The only thing that
  still touches this column is the boot migration that moved its gold across
  (and finds nothing left to move); item 5's read was the last other one.
- **Fix:** tiny, but a schema change: it goes with each table's CREATE moving
  into its package (ARCHITECTURE.md step 11), where the order of creation is
  checked once.

### 10. Four packages write a refill's note the same way

- **Where:** consuming/data.py, death/data.py, kill/data.py and
  ownertools/data.py each have their own `INSERT INTO consume_grants`: a
  potion's grant, a revive's and a respawn's, a level-up's.
- **Why it matters:** the healing check (status/) reads those rows to tell an
  earned refill from a claimed one, so they must agree on the shape. Today
  they do, and test_healing drives all of them; a column added to
  consume_grants would have to be added in four places.
- **Fix:** small - status/ hands back one `note_refill()` from register(), and
  the four are handed it. Worth doing with the table batch (step 12), when
  consume_grants' CREATE moves to its owner anyway.

### 11. giftwatch.py keeps its own copy of the gifts report's SQL

- **Where:** giftwatch.py, beside ownertools/data.py's `gift_report()`.
- **What it is:** it was copied because importing it meant importing app.py,
  whose start-up writes. It is a package's now, and importing that runs
  nothing. test_gifts.py GL-8 holds the two copies to the same numbers.
- **Fix:** small - giftwatch prints what `ownertools.data.gift_report()`
  answers instead of summing it again itself (its own sums and the route's
  report are shaped differently, so its printing changes too); GL-8 already
  checks the numbers it prints, and would hold the change.

## Done

### 1. The API docs page was blank - fixed 18 Oct

`POST /api/staff/skill`'s docstring had `description: "One skill, or
\"all\""`. In a Python string `\"` is just `"`, so the docs builder read broken
YAML, `/apispec_1.json` answered 500, and `/apidocs` loaded with nothing in it -
every route's documentation was missing. Now `'One skill, or "all"'`.

Found with it: four owner switches (maintenance, pvp, trade, co-owners)
documented their `on` field as `true`, because YAML reads a bare `on` as a
boolean - and the spec could only be sent by a fallback that logged an ERROR
with a traceback on every fetch. Now `"on":`.

test_routes.py's new RT-4 asks for the spec on every run: it must answer 200,
hold every documented route and method (136), and name no field true or false.
It fails against the code before the fix.

### 2. The test suites could not run side by side - fixed 18 Oct

52 of the 61 API suites put their database at a fixed name in the shared temp
folder, so two runs at once wrote over each other. Shown, not supposed: two
copies of test_chardelete.py at once, and the second died on "That username is
already taken". Every suite now asks testdir.py for a private folder, made per
run and removed after it; the same two copies both pass. (Running them in
parallel is item 6.)

### 3. `parse_equipment()` was called by nothing - removed 18 Oct

A guard that guarded nothing since the save stopped writing equipment. Gone,
with the places that described it corrected: wsgi.py's refusal (which also
said /api/save "will accept any item in any slot" - today the equip route
refuses every piece in that state), the boot check's description, the equip
route's comment, test_catalogue.py, test_api.py, test_equipment.py and
SECURITY_NOTES.md.

### 4. A leftover in the save route - fixed 18 Oct

`stored_level` in `PUT /api/save` was read and never used, under a comment
saying equipment was gated on it. Dropped, and the two comments beside it that
still described parsing equipment there are corrected.

### 5. Small tidy-ups - fixed 18 Oct

- test_ownership.py: the unused regex `pattern` in `sql_statements()`.
- test_gifts.py: the unused `json` import.
- `GET /api/character` no longer reads `saves.bank_gold` (see item 7).

### Found and fixed with the status move, 18 Oct

- **Stale comments on the status rules.** The note above SERVER_OWNED_STATS
  said gold was "NOT IN THIS LIST" because the loot pickup was the client's -
  directly above a list that has held gold since that hole was closed, and
  with the hp/mana/stamina lines written out twice. Rewritten as one accurate
  note in status/services.py. The healing check's docstring said a 400 would
  lose "XP, gold, position and inventory", which a status write has not
  carried for a long time; it now says what it does carry.
- **`REVIVE_EXPLAINS_EVERYTHING`** was defined and read by nothing (the check
  names the refill grants directly). Removed.

### Found and fixed with the who-is-around move, 18 Oct

- **Unused imports, 60 of them.** `logging` in app.py; `tempfile` in 50
  suites, which item 2 left behind when they moved to testdir.py (that one was
  this work's own leftover); and nine more in the suites and tooltip_sim.py,
  plus a second `import subprocess` in test_security.py. Each removal was only
  the import, and every suite passes as before. Keeping it that way is item 8.
- **The nearby list read the clock four times** for one query (the online
  cutoff and "now", twice each). It reads it once now, so the two halves of
  the online test can never straddle a second.

### Found and fixed with the switches move, 18 Oct

- **The trade switch counted open trades with its own query** into trade/'s
  table, the one place outside trade/ that still read it that way. trade/
  answers it now (`open_count()`), and the bare-app check runs that count.
- **The co-owners' switch cleared another function's cache by its private
  name** (`g._co_owners_on`, co_owners_on()'s). app.py has
  `_co_owners_changed()` beside co_owners_on() for that now, and the switch
  calls it.

### 8. Nothing checked for unused imports - fixed 18 Oct

The 60 cleared with the who-is-around move had built up because an unused
import breaks nothing. The API's CI workflow (.github/workflows/tests.yml) now
has a second job, `imports`: flake8's pyflakes checks alone (`--select=F` -
unused imports, names that do not exist, a name defined twice; no style
rules), pinned to flake8 7.1.1. It honours the `# noqa` marks the packages'
`__init__.py` files and wsgi.py carry where an import is the export. Run here
on the whole API with that version: no findings; a planted unused import and
an undefined name each fail it. A job of its own, so it never hides how the
suites went. Its first run on GitHub is the first push after this one.

### Found and fixed with the kill move, 18 Oct

- **The kill bucket's comment described the old bucket.** It said twenty
  tokens and one a second - sixty kills a minute - directly above the fifty
  and five the code has; the paragraph after it gave the new reasons without
  taking the old numbers back. It now says what shipped first and why it is
  fifty and five (kill/services.py).
- **killwatch.py's copy of the gate's numbers was held by a comment only.**
  It keeps its own window and respawn floor so it runs without the server,
  and said "move them here in lockstep". test_killwatch.py now checks both,
  and the ceiling they give for any count of monsters, against kill/'s; a
  window changed in one place fails it.

### 9. A loot bag was still made in app.py - fixed 18 Oct

Found with the kill move: `_create_loot_bag()` - stamp the roll's cells, mint
the bag's id, store it, commit - stayed in app.py when loot/ moved, because it
was the kill's last step and the kill was still there. It is loot/'s now,
`create_bag()`, handed back from register() (as carry/ hands back its grants)
and handed to kill/ through app.py, which keeps the old name. The bare-app
run makes a kill's bag with it, and checks that nothing dropped makes no bag
and a roll past nine cells is cut and committed; three sabotages in it each
fail a check.

### Found and fixed with the last three moves, 19 Oct

- **`SNAPSHOT_REASONS`** in app.py was defined and read by nothing. Removed.
- **The comment that explains the recovery answer** ("the one answer
  /api/auth/recover ever gives") sat above `needs_recovery_email()`, a
  different thing. It is above `RECOVER_GENERIC_MESSAGE` now.
- **test_refusals' "the server still uses 403" read app.py alone**, and the
  password confirm's 403 moved to auth/. It reads the packages too now, and
  counts code rather than comments.
- **Nothing proved the mail was handed in as a lookup.** The suites replace
  send_mail and send_mail_async with the same recorder, so a route handed the
  real send_mail_async once still landed in it - through the real one's own
  lookup of send_mail. test_accounts A-6 replaces only send_mail_async now:
  an address confirmed and a reset asked for must both reach it. Handing
  either package the real one by value fails it.
- **CLAUDE.md named `_give_to_player()`** in three places; it is ownertools/'s
  `give_to_player()`, and `gift_report()` is ownertools/data.py's.
