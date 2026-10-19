# Upgrades noticed along the way

18 Oct. The owner, while app.py is being taken apart: "if anything needs
upgraded along the way keep track so i know". This is that list. Each item was
checked before it was written here, and each says where it is, why it matters,
and how big the fix is. The architecture moves themselves are not listed:
those are ARCHITECTURE.md's plan.

When one is fixed, it moves to **Done** at the bottom with the date. New ones
go at the end of **Open**.

## Open

### 12. One startup check for every setting - low, before launch

19 Oct, from the owner's lesson on settings and secrets. Not needed while
the owner is the only one who sets up a server; worth doing before 1.0, or
as soon as anyone else runs one.

What is already right: every setting comes from the environment (envfile.py,
a real variable winning over .env), .env is gitignored, there is no signing
secret to leak (a token is random and stored as a hash), no password or key
reaches a log, and wsgi.py's preflight refuses ELUSION_DEBUG.

What is missing, each checked:

- **No `.env.example`.** The settings' names are spread over app.py's
  comments and DEPLOY.md. One committed file with every `ELUSION_...` key and
  fake values, and a test that fails when the code reads a key it does not
  list.
- **Two "this computer only" switches are not refused on a server.**
  `ELUSION_MAIL_CONSOLE=1` writes login and reset codes into the log, and
  `ELUSION_VOICE_DIRECT=on` shows players each other's addresses. app.py and
  DEPLOY.md both say "never on a public server"; nothing stops it. wsgi.py
  should refuse them as it refuses ELUSION_DEBUG.
- **Half-set settings.** Mail with a host and no From address turns staff
  login codes off (boot prints a line, nothing stops it) - refuse. A voice
  key with no token turns voice off - warn. `ELUSION_SMTP_PORT` or
  `ELUSION_PRESENCE_PORT` that is not a number crashes inside app.py's import
  with a bare ValueError - refuse, naming the setting.

The shape: `envfile.check_env(environ) -> (problems, warnings)`, pure, so a
test hands it a dict instead of booting wsgi.py once per case. wsgi.py calls
it first; the gamedata export check stays in wsgi.py (it needs an import).
Refuse what is unsafe, warn about what only switches a feature off - a
refusal during an update is the server down.

**Before it ships**, check the live settings file holds neither switch, with
a command that answers yes or no and prints no values - or the update that
brings the refusal stops the server. About half a day with its tests.

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

### 6. The test runs went one at a time - fixed 19 Oct

`run_tests.ps1 -Jobs N` runs N suites at once - by default one per CPU, at
most 6; `-Jobs 1` is the old loop itself. Same discovery, one process per
suite (Start-Job, the one way in both Windows PowerShell 5.1 and 7 that keeps
a suite's errors in line with its output), the transcript in name order, the
same exit code. Here, all 61 suites: 316 s one at a time, 156 s four at a
time, the same 5,829 checks either way; CI passes `-Jobs 4`. Proved on
PowerShell 7 (what CI runs); 5.1 is checked by syntax - no 7-only feature is
used - but was not run.

Found with it, and fixed:
- **A runner that could not start a suite passed.** If the interpreter could
  not run at all, the old loop stopped, said "0 failed" and exited 0 - a
  green gate with nothing run. Now a suite that cannot start fails, and a run
  that misses a suite exits 1 and names it.
- **test_presence raced its own sweep.** "A renewed ticket keeps the socket"
  expired the old ticket first and renewed second; on a busy machine a sweep
  landed between the two and dropped the socket. It renews first now, as a
  game does; the check still fails if renewal is ignored, and three copies at
  once passed twice.

### 7. `saves.bank_gold` was a leftover column - fixed 19 Oct

A new database's saves table has no bank_gold. On an old one the boot
migration moves what is left into accounts, as it did, zeroes it, then drops
the column (SQLite 3.35+; where the drop cannot run it says so in the log and
leaves the column zeroed, so nothing is moved twice). test_schema.py boots two
old databases full of rows - one as the live server has it, one from before
the ledger - and checks the gold arrives, the ledger balances, and nothing
else changes; and that no statement anywhere reads the column. (Character
deletions saved before the drop carry `"bank_gold": 0` in their JSON copy: a
hand restore leaves that key out.)

### 10. Four packages wrote a refill's note their own way - fixed 19 Oct

status/ hands back `note_refill()`, the one writer of consume_grants in the
API, and consuming/, death/, kill/ and ownertools/ are handed it. Every row is
what it was, column for column (death's took the defaults by leaving two
columns out; it names them now). On the bare app each of the four is handed a
tagged writer, and one check needs all five kinds of row to have come through
it.

### 11. giftwatch.py kept its own copy of the gifts report's SQL - fixed 19 Oct

It loads ownertools/data.py's `gift_report()` from its file - that file
imports nothing, so no Flask on the server and no app.py start-up - and
prints what it answers. Its output is byte for byte what it was. GL-8 checks
it uses the package's function and that its numbers are the route's.

### Found and fixed with the tables move, 19 Oct

- **The backpack's table comment said the bank was "a set of stacks"**, keyed
  differently; the bank is keyed by cell too, and lives in bank/ now. And a
  comment from the bank's old key sat orphaned above `accounts`. Both fixed.
- **Nineteen package docstrings still said a moved table was app.py's**
  (and killrecord's named the gone `_record_kill()`). They name the package's
  schema.py now.
- **test_world rebuilt world_ticks from a copy of its CREATE.** It calls
  `world.schema.create()` now, so there is one definition.
- **test_ownership's per-user table list read app.py only**, so fifteen moved
  tables would have dropped out of its write scan. It reads the packages'
  schema.py too, and finds the same 33 tables it did.
