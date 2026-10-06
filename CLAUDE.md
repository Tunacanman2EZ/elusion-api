# Working on the Elusion API

Read this before changing anything. Everything in it cost real hours to learn.

This is the Flask service behind Elusion RPG, a Godot game in a separate
repository. It owns the things a client must not be trusted with: level, XP, the
derived stat maxima, every loot roll, and loot bags. `docs/apicontract.md` in
the game repo is the agreement between the two — change that first, then both
sides.

**The security model is one page: [SECURITY.md](SECURITY.md).** Read it before
changing anything that touches authentication, ranks, the economy or chat
moderation. Every promise on it names the test that holds it, and
`test_security_doc.py` fails if a suite it names has gone or a check it quotes has
been renamed — so the page cannot quietly drift out of date the way two comments
in this repository already did.

## How to work in here

**Run the tests before and after. Every time.**

```
.\run_tests.ps1
```

It discovers every `test_*.py` beside it, runs them all, and exits non-zero if
any one of them does. **The exit code is the source of truth**; the total it
prints is scraped from each suite's own summary line, and it says so when a
scrape misses rather than reporting a confident zero.

One suite on its own:

```
.\venv\Scripts\python.exe test_economy.py
```

Every suite builds a throwaway database in your temp folder, so none of them
ever touches `elusion.db`.

**THERE IS NO CHECK COUNT WRITTEN DOWN HERE, AND THAT IS DELIBERATE.** This
line used to say "262 checks" and the Layout below used to give a figure for
each of five suites. Every one of those numbers was wrong - `test_api.py` alone
had gone from 262 to 454. A number in a comment cannot fail, so it stays wrong
until somebody trusts it. `run_tests.ps1` counts; this file does not.

**AND THIS PARAGRAPH BROKE ITS OWN RULE.** It went on to say "there are
twenty-six now" - a count, in the passage explaining why counts do not belong
here - and by the time anybody read this sentence there were twenty-seven. The
suite number is as perishable as the check number and for the same reason, so
it is gone too. `run_tests.ps1` prints `Suites: N` at the top of every run.

**`elusion.db` holds real accounts and password hashes.** It is gitignored and
must stay that way. Do not paste rows from it anywhere.

**Kill the old process before starting a new one.** Windows will let a second
process bind port 5000 without an error. The symptom is a 404 on a route that
plainly exists in the file in front of you, because the process actually
answering is running last week's code. This cost an hour once.

**Re-read a file immediately before editing it**, not at the start of the
session. Working from a stale copy has silently reverted finished work here.

## Traps

### CREATE TABLE IF NOT EXISTS does nothing to an existing table

Adding a column to the schema block changes nothing for a database that already
has that table. A real schema change needs a migration that rebuilds it — see
`_migrate_bank_to_account()`, which re-keys `bank_items` and pools the old
per-character gold onto the account.

What makes this dangerous is that **151 tests passed while the live server was
broken**. The suite builds a fresh database every run, and the fresh one got the
new schema. If you change a table, add a migration test that starts from the old
shape — the `MIGRATION` section exists for exactly that.

### A counter added by migration starts at zero and looks broken

`_migrate_add_column(db, "users", "deaths", "INTEGER NOT NULL DEFAULT 0")` gives
every existing account a zero, and there is nothing to backfill it from — a
death before that line existed left no row anywhere. So the first thing the
kingdom board said after the column shipped was **"Nobody has died yet"**, on a
world whose ledger plainly showed thousands of gold *paid to cheat death*.

Both figures were right. The ledger is a LOG and had the history; the counter is
a TALLY and started the day it was added. A tally and a log disagreeing about
the past is not a bug, it is the difference between the two shapes — and it will
happen again the next time a counter is added to a live database.

**The distinction to hold on to: a counter can only ever describe the future.**
If the history matters, derive the figure from the log instead (the kingdom
total is derived for exactly this reason and says so), or accept that the
counter has a birthday and say so on screen.

`deathwatch.py` is the read-only script that tells these apart on a live
database — deaths per account, the stored hp of every character, and every
revive in both ledgers with its date. It is worth running before believing a
counter is broken, because the answer is usually "the deaths are older than the
column".

### A screen with two exits is two migrations, and only one got done

`POST /api/character/revive` was moved onto the server under a comment that
begins **"THE SERVER DOES ALL THREE THINGS THAT USED TO HAPPEN HERE"**. The
button next to it on the same screen - the one that pays nothing and loses
everything carried - was left exactly as it was, writing full hp into the save
slot on the client and zeroing the carry gold in the same local dictionary.

Both halves were wrong, and in **opposite** directions, which is why neither
was noticed:

- **The heal was a client decision.** The next status push arrived as a rise
  from hp 0 to hp 504 with nothing authorising it, and `_reconcile_heals()`
  clamped it to what five seconds of regeneration could produce:

      unexplained heal: hp +504 vs regen 53 + granted 0
      heal clamped: hp 504 -> 52

  Full bars on screen, a corpse on 52 hp after a relog. **The reconciler was
  not the bug.** It was the only part of the system telling the truth, and the
  log line named the missing piece precisely - `granted 0`, meaning no route
  had authorised anything.

- **The penalty was also a client decision, and it did nothing at all.** `gold`
  is in `SERVER_OWNED_STATS`, so `PUT /api/player/status` ignores whatever
  balance a client sends. The carry gold was zeroed locally, never destroyed
  here, and came back in full on the next login. The empty inventory *did*
  stick, because losing items is a loss and only gains are reconciled. So true
  death took the items, refunded the gold, and left the character unplayable.

`POST /api/character/respawn` is the other half of the migration. It refuses a
living character (409, same as revive), burns the carry gold through
`gold_delta()` under the reason `death`, empties `carry_items`, takes off and
destroys everything worn (`saves.equipment` becomes `{}`, named in `gear_lost`),
refills the three pools to the **bare** class curve, and writes a
`consume_grants` row so the client's next sync is explained instead of clamped.

**Worn gear is lost with the bag** since day 2. The owner, after dying: "gear
is not dropping on full death" - it never had. Nothing is exempt, mythic weapons
included; a paid revive keeps everything, and the bank is the only thing a full
death cannot reach. The maxima are derived as if wearing nothing, because after
the commit that is the truth: deriving from the row would refill to a ceiling an
amulet set and leave hp above the new one. `test_gearbonus.py`, "ACCEPTING DEATH
TAKES EVERYTHING WORN", holds it, including a stale save that names the old
gear and changes nothing.

**The lesson is about forks, not about death.** When a decision moves from the
client to the server, the thing to grep for is not the function you moved - it
is every other branch that reached the same state. Two buttons on one screen
both ended with a character alive at full health; one of them was migrated and
the other kept its own copy of the answer for months.

**And: a clamp is a symptom report, not a failure.** `_reconcile_heals()` logs
before it trims, with the numbers. Any `unexplained heal` line in a live log is
either a cheat or a path that has not been migrated yet, and this one had been
printing the answer every time somebody died.

### Routes below app.run() never register

`app.run()` blocks, so anything defined after it is parsed and then never
reached. The entry point sits at the very end of the file with a comment saying
why. A route that 404s despite existing is either this or a stale process.

### init_db() runs at import time

Helpers defined further down the file do not exist yet when it runs. Table
creation belongs in the schema block, not in a function called from it. This
produced a `NameError` on startup once.

### A setting read before the .env is a setting the .env cannot set

app.py read its `.env` with a helper four hundred lines down. `import gamedata`
at the top had already taken `ELUSION_GAMEDATA`, `DB_PATH` had taken
`ELUSION_DB` and `TRUSTED_PROXY_HOPS` had taken `ELUSION_TRUSTED_PROXIES`, so
those three in a `.env` did nothing, and `wsgi.py`'s preflight - which reads
everything before importing app - saw none of the file. Found standing up the
live server; the server itself was fine, because systemd sets the environment
before Python starts.

`envfile.py` is the fix, and it is a module so the rule is about position, not
discipline: `import envfile; envfile.load()` are the first two statements of
app.py and come before the preflight in wsgi.py. **Nothing goes above them.**
A real environment variable still wins over the file. `test_deploy.py` checks
the order with `ast` and boots a copy of both files with nothing but a `.env`.

### A WAL database copied is a WAL database

The live database runs in WAL mode, and SQLite's backup API copies the mode with
the pages. Opening a WAL file at all, even read-only, creates a `-wal` and a
`-shm` beside it, so the first night's backup on the live server was three
files and the pruning only knew about one. `backup_db.py` switches every copy to
`journal_mode=DELETE`, reads the header bytes before `verify()` opens anything,
and settles or sweeps leftovers - but only under names it writes, because a
lone `elusion.db-wal` in somebody's folder may be a live database's unfolded
transactions. `test_deploy.py`.

### A whole-bag save built before another change undoes it

`PUT /api/character/inventory` replaced the whole carry, and every other route
that changes the carry - a loot take, a cook, a catch, a purchase, an equip, a
consume - writes it too, while the game saved on a two-second debounce. So a
save built after one of those and landing after the next undid the next one:
on day 1, cooking a stack of twelve, one cooked fish in every few was gone.
`based_on` fixed it then (the save named the bag it was built on, and a stale
one got 409 and the server's bag); `test_gathering.py` still reproduces it.

**Then the trap was removed rather than guarded.** The bag and the bank are
the server's now (ONE CELL AT A TIME in app.py): a drag is
`POST /api/character/inventory/move`, the bin is `.../discard`, a pile is
`.../cash`, and the bank has `/api/bank/move` and `/api/bank/discard`. A
player's whole-bag and whole-bank writes are answered with the server's grid
and `ignored`; build 2 of the game does not send them. The `based_on` and
trade-resync 409s are still in `write_inventory()` for a build-1 game. **A new
route that changes a grid takes one cell and the item the game saw in it** -
`_grid_move()` / `_grid_discard()`, and `_carry_resync()` / `_bank_resync()`
for the 409 - and answers with the whole grid. `test_bagmoves.py`.

### A read before the first write is outside the transaction

sqlite3 opens a transaction at the first INSERT, UPDATE or DELETE, not at the
first SELECT. So a route that read a balance, worked out the new one in Python
and wrote it back could be run twice at once, with both reading the old figure.
On day 1 this lost XP from every pair of kills one swing produced (six kills
sent together banked 99 of 676), and the same shape let one loot cell pay five
times, one potion explain five heals and one purse be deposited twice.

**THE WRITE LOCK** (top of the DATABASE section) is the fix, and it is one rule:
`get_db()` runs `BEGIN IMMEDIATE` for every POST, PUT, PATCH and DELETE, before
the route reads anything, and `_WriteLockedConnection` takes the lock again after
each `commit()`, so a route that commits part-way is still covered. A new write
route gets it without anyone remembering.

- **`@no_write_lock` is for slow work only**: a password hash or a picture
  fetch, which must not make every other player's write wait. There are nine;
  `test_concurrency.py` names them, so adding a tenth fails the suite until
  somebody writes down why. A route that opts out must not add up a balance from
  what it read before its first write.
- **A GET that writes is not covered.** The broadcast poll stamps presence and
  hands over a resync flag; neither adds to anything it read. Do not give a GET
  a read-then-write.
- **It costs nothing measurable.** SQLite already has one writer at a time;
  `loadtest.py` on the kill route was ~550 writes/s before and after.
- **`test_concurrency.py`** sends real requests together on real threads and
  holds each route's read-to-write window open for 50 ms, so the old race fails
  every run rather than now and then.

### The heal check's clock is pools_at, not updated_at

`_reconcile_heals()` allows the regeneration that fits between the stored
pools and the new ones, so it has to know when the stored pools were written.
It read `updated_at`, which a kill, a loot take, a skill tick, a gold move and
an equip all bump without touching hp or mana. A player who stood still for
twenty seconds, killed something and saved a moment later was allowed two
seconds of regeneration; seen live on day 1 as `mana +19 vs regen 8` while
picking up coins, and clamped once the gap was wide enough.

`saves.pools_at` is written by every write that sets hp, mana or stamina - the
status write, revive, respawn, a new character - and by nothing else. A row
from before the column reads 0 and falls back to `updated_at`. The status
write moving it matters as much as the others not moving it: if it stood
still, an hour-old clock would pay for any heal. `test_healing.py`, "Something
else wrote the row in between", holds both directions.

### JSON has no integer type

The client is Godot, which parses every JSON number as a float. `88` goes out
and `88.0` comes back. Anything the client sends is coerced on arrival —
`parse_stat()`, `int()` inside a try — and anything sent to it should be a real
int, because Godot's comparisons are type-strict and a float that should be an
int has cost this project a full day already.

### A lone surrogate is legal JSON and cannot be stored

`"\ud800"` with no partner parses into a Python `str` that cannot be encoded as
UTF-8, so the first database write that touched it raised - a chat message, a
character save or an email address came back as a 500. `_StrictJSONProvider`
(top of app.py, `app.json = ...`) refuses it while parsing, so
`get_json(silent=True)` sees `None` and every route gives its ordinary "not
JSON" 400. A route that parses a body some other way does not get this for
free.

### Use SystemRandom for anything a player benefits from predicting

Python's default RNG is a Mersenne Twister and its state is reconstructible from
624 observed outputs. A loot table is a very long game, so `gamedata.py` uses
`random.SystemRandom()`.

### One rule, two validators

The stack ceiling was added to `_parse_positional_items()` and the backpack
route kept accepting a billion potions, because that route had its own copy of
the same twenty lines. There is now one validator, and a test asserting the bank
and the backpack are bound by the same rule — because that is the failure a
future change would otherwise reintroduce.

### Deleting the row that points at a thing is not deleting the thing

`POST /api/chat/delete` removed the `chat_messages` row and left `chat_images`
alone. The picture went on being served — to everyone who was in the channel when
it was posted, because they already hold the id — with
`Cache-Control: max-age=31536000, immutable` on the response and nothing but the
128MB eviction loop ever removing a row. A mod deleting an offensive picture only
stopped it reaching the people who had never seen it.

Two things to carry from it:

- **Ask what a delete actually revokes.** "The line is gone from the feed" and
  "the bytes are unreachable" are different claims, and the first one looks like
  the second from the moderator's side.
- **Reference count before you drop shared storage.** `store_relayed_image()` is
  `INSERT OR IGNORE` on a content hash, so the same picture posted twice is ONE
  row under two messages, possibly in two channels. Deleting the row on the first
  message would blank the picture under a second, innocent line. Count what still
  names it and drop at zero. The route reports `image_dropped` either way.

The consequence of content addressing that nobody had written down: **a whispered
picture is only as private as its bytes are rare.** Post the same image in world
chat and it is the same row and the same id, so the whisper was never private.
That is the right trade for a game chat — it should just be a known one.

### A feed that can only grow cannot be moderated

The same idea one layer up, and the bigger half of it. `GET /api/chat` answers
*"messages with an id greater than `since`"*. That is the right question for a
growing log and it **cannot express the opposite one**: something you were already
given is no longer true.

So `/api/chat/delete` removed the row, the message stopped reaching anybody who
had not read it yet, and it did nothing at all about the people who had.
`chatpanel.gd` was append-only — `_poll()` only ever called `_add_line()`, and a
line left only by `pop_front()` at `LINES_KEPT`. A mod took a line down and it
stayed on every screen that already had it until a hundred more lines pushed it
off, or the player closed the game. **That is exactly the set of players the
deletion was for.**

The poll now returns `removed`, and three decisions in it are worth keeping:

- **A separate `chat_deletions` table, not a `deleted_at` column.** A tombstone
  column means every existing read has to learn `AND deleted_at = 0` — a dozen
  places to forget one, and forgetting it shows the deleted line again. A separate
  table is additive: nothing that reads `chat_messages` changes, because the row
  really is gone.
- **It stores the deleted line's own `channel`, `user_id` and `target_id`** — the
  three columns the read builds its permission clause from — so the poll filters
  deletions with **the very same `where` and `params`** it filtered messages with.
  Not a second permission rule to keep in step: the same one. The property falls
  out for free — a line you could never have read cannot produce a deletion you
  are told about, and nobody has to remember that. Without it, world chat carries
  the ids of deleted whispers to everyone; no content in an id, but still the fact
  that a line existed between two people and was taken down.
- **By time, not by id, and no second cursor.** The deleted message is almost
  always *below* the client's cursor, where `since` can never reach, so deletions
  are reported by when they happened — anything inside
  `CHAT_DELETION_WINDOW_SECONDS` (120). That window is only sufficient because of
  something the client already did: `chatpanel.gd` starts from the **tail** rather
  than a stored cursor when the window opens, so a client away longer than the
  window is not holding the line anyway. Pruned on write, like `_prune_chat()`.

On the Godot side `_remove_lines()` does the work, and the case that makes it
worth building properly is **the open viewer**. `_sweep_pictures()` deliberately
*spares* `_viewing` — correctly, for its own job — so sweeping first would keep
the picture alive and leave the overlay up in front of the one player most needing
it gone. `close_viewer()` runs **before** the sweep. After that the existing sweep
does the rest unchanged, because it rebuilds the live set from whatever the feeds
still hold.

Two smaller ones, both sabotage-tested: removals run **after** additions, or a
line posted and deleted inside one three-second poll would survive; and an id of
**0** must match nothing, because a system notice carries no server id and `0` is
what `line.get("id", 0)` returns for one.

### Tests that assert magic numbers rot

Checks written against a hardcoded 180 HP or 500 gold break the moment the
system gets more real, and the noise hides genuine failures. Derive from the
curve, or assert conservation — the total before equals the total after. That
pattern caught a real bug: `/api/combat/kill` was changing level without
updating the maxima it implies.

## Design rules that look wrong and are not

- **Server-owned stats are ignored, not refused.** `PUT /api/player/status`
  silently drops `level`, `xp`, `xp_to_next` and the three maxima, and names
  them in an `ignored` array. A 400 would break every honest client, because the
  client sends its whole status block and has no way to know which fields the
  server has taken ownership of since it was written.
- **A pet is stored only when held.** `/api/save` keeps an `active_pet_id`
  only when the character's carry or the account's bank has it
  (`_owns_item()`); otherwise it clears the field and names it in `ignored`.
  Still no list of pets, for the reason below. Decided on day 1; it used to
  store any string, so a modified client could walk out a pet it never won.
- **Item ids are not whitelisted.** Validating them against a list would mean
  every new item in the game needs a matching server deploy. Unknown ids are
  bounded by `QUANTITY_CEILING` instead; known ones by their own `max_stack`.
  The one exception is an id with a quality roll in it (`~`): a staff grant
  of one that is not a real roll is refused, because every other route would
  read it as nothing.
- **Every query scopes on `g.user["id"]` in the SQL itself**, rather than
  fetching and then checking ownership. "Not yours" and "does not exist" return
  the same 404, so there is nothing to learn by asking. (That last clause is the
  half that usually survives broken: a route that finds the row, sees it is
  somebody else's and answers 403 has fixed the write and kept the oracle —
  the attacker now enumerates ids by 403-versus-404.)
- **Better still, where it is available: do not let the client name the row.**
  Not one trade route accepts a `trade_id` — every one calls
  `_trade_find_open(db, g.user["id"])` and works on what comes back. An
  ownership bug needs an id to tamper with, and these routes have none to offer.
  A check can be forgotten on the route added next month; a missing parameter
  cannot. `POST /api/staff/gold` is the same idea: owner-only and *still* unable
  to name another account. So is `POST /api/staff/level` (day 2, so the owner
  can test level 22 gear): it sets the caller's own character, does what a
  level-up does to XP, maxima and pools, records the refill as a level-up
  grant, and logs a `level` line; `test_ownership.py` O-7 holds it. And
  `POST /api/staff/skill` (6 Oct, "full control of my stats ... so i can do
  more testing"): one skill or `"all"` (`STAFF_SKILL_ORDER`, held to
  `VALID_SKILLS`) set to a level 1-`MAX_SKILL_LEVEL` with no XP into it, on the
  caller's own character, with a `skill` line in the log; O-8 holds it.
- **`GET /api/chat/image/<image_id>` is served only to someone who could see
  it** (`_may_see_image`): a line the caller could read shows it, by
  `_can_read_chat_row`; or the caller uploaded those bytes
  (`chat_image_holders`, not `created_by`, which only remembers the first of
  two uploaders of one file); or a mod is judging a report of it. Whisper,
  friends and guild lines get their own copy under a random id
  (`_private_image_copy`), and `/api/chat/send` refuses an id the sender could
  not see. World pictures keep the content id, SHA-256 of the bytes, and
  `test_ownership.py` O-3 still pins that it is 64 hex and the hash of what is
  served. Drop a picture with `_drop_image()`, never a bare DELETE, so its
  holders go with it.
- **Login and unknown-username return byte-identical 401s.** A distinguishable
  answer turns the route into a username enumerator. There is a test asserting
  the two responses are equal.
- **Kill rate limiting is a token bucket, not a minimum gap.** Real combat is
  bursty — an AoE or a splitting slime produces several kills in one frame — and
  a minimum gap can never allow a burst no matter how it is tuned. 50 capacity,
  refilling at 5/second.
- **Gold is a separate endpoint from bank items.** Storing an item is a
  one-sided write the server cannot verify. Gold is a transfer, and the server
  holds both balances, so it can check the move is possible and the total is
  conserved. Folding them together would throw that away for one fewer route.
- **`/api/status` has no auth.** The login screen needs to ask "are you there?"
  before anyone has logged in, and a 401 is not an answer to that question.

## Which refusal code, and why this file breaks the textbook

The textbook is four sentences: authentication is who is asking, authorization is
what they may do, 401 means authentication failed, 403 means authorization
failed. This file obeys the first two and deliberately breaks the third, and a
first read makes it look inconsistent — fifteen `403`s and twelve `404`s that are
plainly authorization refusals. There is one rule underneath, and every one of
those twenty-seven already follows it:

> **The gate that decides whether the route is yours answers 404.
> The check on what you asked for, inside a route already yours, answers 403.**

Read it as a question about the caller: **does their own rank already admit them
here?**

- **No** → the refusal would *be* the disclosure. A 403 confirms the route
  exists and that you are not allowed to use it, which tells someone exactly
  where to push. So it is a 404 and it says nothing: `"Not found."`, no rank
  named. This is `require_role`, `require_owner`, the guild officer/leader
  pre-checks, and owner-only guild naming.
- **Yes** → they are already inside, so the refusal tells them nothing they did
  not know, and it may as well say why. `"A mod may ban for at most 30 days."`
  is useful and leaks nothing, **because only a mod can read it.**

That is why `/api/staff/ban` answers **404 to a player** and **403 to a mod who
asked for a permanent one**. Same route, same gate, different question. Every
403 in the file is a check on an *argument*: permanent vs temporary, which rank,
everyone vs one player, level, class, bait, a fishing rod.

The one 403 that is not about an argument is the **ban at login**, and it is
deliberate twice over: it runs *after* the password check, so the route cannot be
used to find out who is banned, and it includes the reason, because a banned
player has every right to know and silence reads as the game being broken.

**The 401s have no exceptions at all.** All eight are authentication: three are a
token that resolved against a row since deleted (identity can no longer be
established — late, but still authentication), the rest are a password that did
not match. Which produces the fact the **client** depends on:

> **A 401 is not a verdict on the session.**

`/api/auth/password` and `/api/account/email` both require the *current*
password and answer 401 when it is wrong, while the token stays perfectly live —
a valid token is not proof of identity when the token may be the stolen thing. So
a client that signs people out on any 401 signs them out for typos.
`characterhud.gd::_on_unauthorized_seen()` asks `heartbeat()` instead of
deciding, and `test_refusals.py` F-3 is what that handler is standing on.

**`test_refusals.py` enforces all of this, 80 checks.** F-1 does not have a list
of staff routes typed into it — it reads them off `_elusion_min_role`, the same
tags `/api/staff/powers` reports from, so a route added without a rank decorator
fails the suite instead of quietly working for everyone. Sabotage-proven: turn
`require_role`'s 404 into a 403, let the 404 explain itself, add an ungated
`/api/staff/` route, split the login 401, or drop sessions on a wrong current
password, and it fails.

## Ranks

Built. `owner > dev > mod > player`, as `users.role` - one ordered column, not a
pile of booleans. Two booleans is four states; four is sixteen, and most of
those are nonsense.

What each one actually means, because the names do not say it:

| rank | who that is |
|---|---|
| `owner` | The person running the server. Named in the environment, not the database. |
| `dev` | Technical trust - someone hired, or met through a pull request. Knows the code; may never have played seriously. |
| `mod` | Community trust - usually a player, who knows the other players. |
| `player` | Everyone. The default. |

**Those are two different kinds of trust flattened onto one ladder, and it has a
consequence:** dev sits above mod, so a dev inherits every moderation power a
mod has. Someone you met through a pull request can ban your players.

That is currently accepted rather than solved - the audit log makes it
reviewable and demotion is instant, and two separate axes is real machinery for
a team of three. If it ever stops being acceptable, the fix is in the command
registry rather than the ladder: let a command declare **either** a minimum rank
(`{"min": "mod"}`, which most use) **or** an explicit set
(`{"ranks": ("mod", "owner")}`, for the few where inheritance is wrong). One
extra field, and "bans are for mods and me, not the contractor" becomes
expressible.

`role_at_least(user, "mod")` is the test, and `@require_role("mod")` is the
decorator, which sits inside `@require_auth` because that is what puts the user
row on `g`.

**The owner is not in the database.** `ELUSION_OWNER` names the username in the
environment. No request can write to it, it is not in a backup of `elusion.db`,
and `role_for()` consults it before the column - so revoking someone's row
cannot lock the owner out, and a column set to `'owner'` by hand grants nothing.
There is no write that produces the top rank.

**Everything unrecognised fails toward less privilege.** A stored rank this
build has never heard of reads as `player`. An unknown `minimum` passed to
`role_at_least()` denies rather than raising - removing the `admin` rank turned
every surviving `role_at_least(..., "admin")` call into a `ValueError`, and a
route asking "may I" deserves "no" rather than a 500 with a traceback in it.

**There is no `is_admin` anywhere any more.** It was a boolean from before
ranks existed, kept for a while as a compatibility key meaning "dev or above".
The column is dropped by `_migrate_drop_is_admin()`, the key is gone from every
response, and the client no longer has a field to put it in. `role` is the only
answer to "what rank is this", and `role_at_least()` the only way to ask.

The word survives in exactly two places on purpose: the two migrations that
remove it, which have to name what they are removing, and the tests asserting
that `role_at_least(..., "admin")` denies rather than raising. Those tests are
what keep it from coming back.

**The CHECK constraint only reaches fresh databases.** A migrated `users` table
gets `role` via ALTER and carries no constraint, so `'owner'` is storable there.
`role_for()` is the guarantee; the CHECK is defence in depth. There is a test
asserting a column reading `'owner'` grants nothing, precisely because the
constraint cannot be relied on.

**Ranks are set from the machine holding the database**, by `set_role.py`,
never over the network. Keep that property in anything that replaces it.

**`/api/staff/powers` is read by the owner, in words.** The routes on it come
from the decorators and cannot go stale, but the game shows each one as the
**first line of its docstring**, so that line is a sentence a person reads in
the Powers window: "Ban an account", not a summary hidden below a `---` (the
gold supply's was, and the window showed "---"). The notes under each rank -
what a rank brings that is not a route - read every number from the constant
that enforces it (`MAX_MOD_BAN_DAYS`, `MUTE_MAX_MINUTES_MOD`, `MUTE_MAX_MINUTES`,
`TRUSTED_DEVICE_DAYS`) and say whether this server's staff logins take a code.
The words are the part that can drift, and did: it sent the owner to "the
in-game item menu" a day after the menu moved into the GM panel. When a tool
moves in the game, read the notes. `test_moderation.py` M-10.

## Moderation

Built. `POST /api/staff/ban`, `POST /api/staff/kick`, `POST /api/staff/unban`,
`PUT /api/staff/role`, `GET /api/staff/users`. The game's Staff button (HUD,
mods and up) drives all of them; the owner's backquote console still exists.

**Kicks and bans reach a running game through the heartbeat, and the heartbeat
is `GET /api/server/broadcasts`.** Both delete the target's sessions, and the
client polls that route every 10 seconds while a character is in the world - a
401 there sends it to the login screen. Only a 401: no answer, a 500 or a 404
says nothing about the login, and a server restart must not be a mass kick.

**Online means a beat within `ONLINE_WINDOW_SECONDS` (45)**, stamped on
`sessions.last_seen_at` by `stamp_presence()`. Not "holds a session" - sessions
last thirty days and survive the game being closed. A beat does not extend
`expires_at`.

THIS PARAGRAPH USED TO NAME `GET /api/auth/session` AND SAY THE CLIENT CALLED
IT EVERY 15 SECONDS. It does not and never did: `api.gd` declares
`HEARTBEAT_SECONDS = 15`, `characterhud.gd`'s comment says the client beats on
it, and the only caller of `Api.heartbeat()` in the whole project is the 401
handler. So the stamp happened once, at login, and forty-five seconds later
every presence surface in the game - the friends list, the guild roster,
`/api/players/online`, `/api/players/nearby`, the staff panel - read every
player as offline. **It was visible: a guild panel telling the only member of a
guild "0 online of 1" while they sat there reading it.**

Revocation always worked, because that rides the 401 and the poll was always
running. Only presence was broken, which is why it survived - the half of the
sentence that was load-bearing for security was true.

`stamp_presence()` is one place both routes call, and the poll carries it now:
that request already happens every 10 seconds and is already authenticated, so
the beat costs one UPDATE instead of a whole extra round trip per player.

**"staff" is not a fifth rank.** It is the set of ranks above player - mod, dev
and owner - because all three use these routes and they need one word between
them. The ranks are still exactly `player < mod < dev < owner`.

Every one is `@require_role("mod")` at the door and
`can_act_on()` inside - the decorator says whether you are staff at all, the
predicate says whether this particular person is within your reach.

**Refusals are 404, and the same 404** whether the account does not exist or
simply outranks you. A mod who could tell those apart could map out who is above
them by guessing names.

**Ban state is three columns, not one.** A single nullable timestamp cannot say
both "not banned" and "banned forever" - NULL would have to mean both.

    is_banned = 0                       not banned
    is_banned = 1, ban_expires_at NULL  permanent
    is_banned = 1, ban_expires_at set   until that moment

Permanent is a real state, queryable and displayable, rather than 9999 days.
The expiry is absolute and checked against now, so nothing has to run on a
schedule to release people and a server that was off for a week does not keep
anyone an extra week.

**A permanent ban is a higher permission than a temporary one.** A mod can time
someone out up to `MAX_MOD_BAN_DAYS` (30); permanence needs a dev or the owner.
The point is not that thirty days is special - it is that the person having a
bad night at 2am should only be able to make the reversible decision.

**A reason is required, 1-500 characters.** A ban nobody can review later is one
the person who issued it cannot defend either.

**Banning deletes that user's sessions in the same transaction**, and
`user_for_token()` checks the ban again on every authenticated request. Login is
not the only door; `require_auth` wraps them all. Unbanning does NOT hand the
session back - they log in again like anyone else, and there is a test saying so.

**A banned player gets a 403 with the reason**, unlike the routes that hide
behind a 404. They have every right to know they are banned and why; silence
there reads as the game being broken. The check runs AFTER the password check,
so this endpoint cannot be used to find out who is banned.

**You cannot grant a rank at or above your own.** Only the owner makes a dev.
Without it, one compromised staff account spreads sideways for as long as nobody
is looking.

**Every action lands in `staff_actions`**, in the same transaction as the thing
it describes, with names stored alongside ids so the log still makes sense after
an account is gone.

**And the log can be read now, which is what makes it a review rather than a
promise of one.** `GET /api/staff/actions` pages it newest-first, filtered by
player, by member of staff or by kind; `POST /api/staff/note` writes a note or a
warning onto an account's record. `test_moderation.py` is the suite. Four things
in it are decisions, not details:

- **Both lists are pages, with a keyset cursor.** The staff panel used to fetch
  every account every ten seconds. `after=<last name>` and `before=<last id>` are
  one index seek each and cannot skip or repeat anybody when an account
  registers mid-walk, which `OFFSET` can. Every filter is a `WHERE` clause, never
  a Python filter over a fetched page, or a page comes back short and `more` lies.
- **Notes and warnings are read under reach** - `STAFF_PRIVATE_KINDS`, the same
  `can_act_on()` rule as IP addresses. A mod never reads what a dev wrote about
  another mod, nobody reads the notes about themselves, and the player sees
  none of it. `/api/staff/user/<name>`'s history obeys it too; it was the side
  door.
- **"About this player" means the account id when the account exists**, and the
  stored name only when it does not. A guild can share a player's name, and
  guild actions carry no id.
- **`STAFF_ACTION_KINDS` is checked against the source** with `ast`, so a new
  `log_staff_action()` call with a new kind fails the suite until the log's
  filter knows it. The index checks read SQLite's own `EXPLAIN QUERY PLAN` for
  the statement the route builds - a plan, not a stopwatch, because a stopwatch
  is green on a fast machine.

`idx_sessions_seen` is created **after** the migration that adds `last_seen_at`,
not in the schema block: on a database from before presence existed, the schema
block would fail on "no such column" and take the boot with it. The MIGRATION
section in `test_api.py` boots exactly that shape, and fails if the index moves back.

**Ban evasion is checked twice at registration: the address and the computer.**
`_ban_evasion_state()` asks `account_ips`; `_install_evasion_state()` asks
`account_installs`, which holds the SHA-256 of the install id the game sends
as `install` on login, register and resume (INSTALL IDS in app.py). Same rules
for both - registration only, ends with the ban, inert above
`EVASION_BLOCK_MAX_ACCOUNTS` - and `_linked_accounts()` merges both into one
entry per account (`shared_addresses`, `shared_computers`). A missing or
malformed `install` is ignored, never refused: older builds send none. Record
it with `_record_account_install()` wherever `_record_account_ip()` runs, plus
on a banned login with the right password (never a wrong one).
`test_security.py` E-5f.

## Chat is one line, as typed, and a whisper finds you

**`clean_player_text(raw, limit)`** is the one rule for text other players
read: chat (`post_chat()`, and `chat_send()` before its emptiness check) and a
character's name (`write_save()`). Control characters, tabs and line breaks
become a space; format characters are dropped (bidi overrides printed a line
backwards, a line of zero-width spaces was an empty line under a name) except
U+200D, which emoji are built from; three accents at most on one letter; runs
of spaces collapse. What is stored is what everybody reads - the test compares
the two. Clean BEFORE checking for empty, or a message of invisibles gets
through as a blank line. `test_chat.py`, "one line, as typed".

**`chat_news` on `/api/server/broadcasts`** (`_chat_news()`): the newest
whisper TO the caller, and the newest line ids in their guild and among their
friends, each said by somebody else. The chat window only reads the tab that
is open, so before this a whisper sat on the server until the player happened
to open the Whisper tab with the sender's name typed in. It rides the poll the
HUD runs anyway, like `trade`. `idx_chat_to (channel, target_id, id)` is the
index it reads; the test checks the query plan uses it. `test_chatrooms.py`,
"what was said to you".

**`asks` on the same poll** (`_waiting_asks()`): the friend requests and guild
invitations waiting on the caller's answer, a count and the newest of each.
Found on day 1 with two accounts: a request to somebody standing next to you
sat unseen until they happened to open the panel. Four indexed reads
(`idx_friends_addressee`, `idx_guild_invites_user`); `test_friends.py` checks
the plans by catching the real statements with `set_trace_callback`, so the
check cannot drift from the code. `test_guilds.py` holds the invitation half.

**`/api/guild/create` answers with both balances after paying**
(`carried_gold`, `bank_gold`). The game used to read only the sentence out of
the answer, so a founder paid from the bank went on seeing the old purse and
bank until a relog.

## Chat: ignore, report, mute

IGNORE, REPORT, MUTE in app.py (the rules sit above `chat_write_check`, the
routes after `chat_delete`); `test_chatsafety.py` holds all three.

- **Ignore is a WHERE clause.** `IGNORED_AUTHORS_CLAUSE` goes on the end of
  every chat read's `where` (with the reader's id), so the tail, the cursor and
  the `removed` list agree about it, and on `_chat_news()`'s three queries. It
  also refuses whispers, friend requests and trades TO the person who ignores
  (`ignores(db, them, me)`). **Staff cannot be ignored**: a mod telling you to
  stop has to arrive. `IGNORE_LIMIT` rows per player.
- **A report copies the line** into `chat_reports` - body, author, channel,
  when - because what a report usually leads to is the line being deleted.
  `_can_read_chat_row()` is the read's own rules, one line at a time: you can
  only report a line you were shown, so the route is not a way to find out
  which ids are other people's whispers. One report per reporter per line,
  `REPORTS_PER_HOUR` per reporter. `chat_delete` closes a line's reports as
  `deleted`. A mod cannot close a report about another mod - the same
  `can_act_on()` as every sanction.
- **Staff see a card per reported PLAYER** (day 1: the tab would flood).
  `GET /api/staff/reports` answers `players` - who, `line_count`, `people`,
  `reasons`, `reporters` and their newest `REPORT_LINES_PER_PLAYER` lines -
  counted in SQL over every open report (`_report_players()`), worst first:
  the most different people reporting them, then the newest. Ten people about
  one line outrank one person about ten. The per-line `reports` list stays
  for older builds. `resolve` takes `username` as well as `message_id` and
  closes everything open about that player with ONE line in the log.
- **Acting on somebody closes their reports.** A mute, kick or ban calls
  `_close_reports_about()` (outcome `actioned`), says so in the sanction's
  log line ("closed 5 reported lines") and answers `reports_closed`. Nobody
  has to come back and tidy the tab.
- **Closed reports are kept 90 days** (`REPORT_KEEP_SECONDS`), pruned on the
  next report filed or closed (`_prune_reports()`), like chat. What staff
  decided stays in `staff_actions` for good. An open report is never pruned.
- `open_reports` (lines) and `open_report_players` ride the broadcast poll for
  mod and up (0 for everyone else); the Staff button counts players.
- **The log opens on moderation.** `STAFF_ACTION_GROUPS["moderation"]` is
  what staff did about players (bans, kicks, mutes, warnings, notes, ranks,
  reports, deletions, guild renames): `?action=moderation` leaves out the
  server switches and the testing tools (grant, teleport). The answer carries
  `groups` for the client's dropdown. A player's record asks for it too.
- **A mute is three columns on users** (`chat_muted_until`, reason, by), read
  by `chat_mute_state()`, refused in `chat_send` before the flood bucket so a
  muted player's attempts cost nothing. A mod's longest is a day
  (`MUTE_MAX_MINUTES_MOD`), dev and owner thirty days; a reason is required,
  like a ban's. Reads carry `muted` so the box can say so before anyone types.
  Mutes count on the record (`STAFF_RECORD_KINDS`).

## Signing in: one game per account, and what the login screen is told

A sweep of the login screen against the real game found four things; each is
held by `test_accounts.py`.

- **One login at a time** (ONE LOGIN AT A TIME in app.py). A login that gets
  its token ends every other session on the account (`_end_other_sessions()`),
  and so does `POST /api/auth/resume`, which is what the game calls on boot
  with a remembered token - it gets a new token that ends when the old one
  would have. Two games on one account lost items: each held its own copy of
  the bag, and the bag write replaces the whole bag. The ended token's hash
  goes in `ended_sessions` for a day so `require_auth`'s 401 can carry
  `signed_in_elsewhere` - looked up only on a refusal, never on a request that
  works. **A test that needs two live sessions on one account inserts the
  second row by hand** (see `stray_session()` in `test_revocation.py`); a
  second login ends the first, and a check that keeps using the first token is
  reading 401s. That happened in three suites when this landed, and two of the
  checks were passing on the 401.
- **The miss that locks says so.** `_register_failed_login()` returns True on
  the miss that locks, and the login answers 429 at once instead of one more
  401. `_wait_words()` puts every wait in minutes.
- **No mail, no demand.** `needs_recovery_email()` is False when
  `mail_can_send()` is, and `POST /api/account/email` answers 503 instead of a
  200 "check that inbox" for a code that went nowhere. The 503 comes after the
  request's own checks (a malformed address is still 400, a wrong password
  still 401) and before anything is stored.
- **The game never registers from the sign-in button.** It used to on a 401,
  so a typo in your own name made a new account. `/api/auth/register` is
  called only by the "Create an account" form now, so its 409 means what it
  says.
- **A session is stored as its token's SHA-256** (`sessions.token_hash`,
  SECURITY_NOTES E-21, day 2). The game holds the token; the database holds
  nothing that logs anyone in. `_token_hash()` is the one function, defined
  above `init_db()` because the migration that hashed the old rows runs at
  import. **Any query that finds a session by its token hashes it first**, and a
  test that puts a session in the table by hand inserts the hash, not the token
  (every suite that does was changed with it). A query that compares the raw
  token finds nothing and fails quietly, as a 401 or a heartbeat that stamps no
  row - the five ways this was broken on purpose all looked like that.

## Deleting a character

`POST /api/character/delete` (DELETING A CHARACTER in app.py,
`test_chardelete.py`). Four fixed slots, one per class, and on day 1 no way to
start a class again.

- **The character's rows, not the account's.** Its save, `carry_items`,
  `skills`, `consume_grants` and `loot_bags` (their items cascade). The bank,
  lusions, friends and guild belong to the account and stay.
- **The purse goes through `gold_delta()`** under `character-deleted`, before
  the save row goes. The gold lives in `saves`, so a bare DELETE would take it
  out of the supply with no burn row - the note on `gold_ledger` in the schema
  says the same about deleting a user.
- **`confirm` must be the character's name**, any case. The game asks for it
  typed; the server checks it again, so a stale button or a replayed request
  cannot take a character.
- **Not while an open trade names it**, from either side (409). Running that
  trade afterwards would move items to and from rows that are gone.
- **A copy is kept** in `character_deletions` - the save row, bag and skills as
  JSON - the newest `CHARACTER_DELETIONS_KEPT` (10) per account, pruned on
  write. A restore is a decision made by hand, not a route.
- **A stale save cannot bring it back.** `PUT /api/character/inventory` answers
  404 for a slot with no save, and a character made again is a new row at
  level 1 with full pools; a player's bag sent over it is ignored, like any
  player's whole-bag write.
- **`parse_slot()` refuses a fractional slot now.** `int(1.5)` is 1, so
  "delete slot 1.5" deleted slot 1. 1.0 is still slot 1, because Godot sends
  every number as a float.
- **Fixed while here:** `/api/character/respawn` named `MAX_SLOTS`, which does
  not exist, so a bad slot there was a 500. pyflakes finds that class of bug in
  one command; it found only this one. CD-7 now sends a bad slot to every
  character route.

## Staff logins take a code from their email

A staff name is public: the owner's crown, the MOD and DEV badges, the players
list. So everyone knows whose password is worth guessing, and the lockout only
slows guessing down. For a mod, dev or the owner **with a confirmed recovery
address**, a correct password answers **202** with no token and emails a
six-digit code; the same login sent with `"code"` gets the token. STAFF LOGIN
CODES in app.py, `test_staffcode.py`.

- **The code is the reset machinery**: `_store_code()` / `_consume_code()`
  under purpose `staff-login` - hashed, 15 minutes, five misses burn it. A
  reset or verify code does not open a login.
- **A wrong code is 400, never 401.** The game reads a 401 as "Wrong name or
  password", and the password was right - a 401 here would send a staff member
  off to retype it over a mistyped code.
- **A wrong code counts toward the account lockout**, and the streak clears
  only on a login that gets its token. It used to clear on a correct password,
  which is the first half of every code attempt, so guessing codes would never
  have locked anything.
- **Nothing is sent before the password and the ban check pass.** A wrong
  password is the same 401 as a player's; a banned mod gets the ban.
- **At most one new code a minute**, so a leaked password cannot flood the inbox.
  Sending no code asks for a new one.
- **The step stands aside, and says so, when there is nowhere to send a code**:
  no confirmed address, no mail on the server (`mail_can_send()`), or
  `ELUSION_STAFF_LOGIN_CODES=off`. Refusing would lock the owner out of their
  own server. The answer carries `staff_unprotected`, the game tells the player
  once, and the boot log says which. DEPLOY.md has the way back in.
- `code-sent` and `bad-code` are logged to `login_attempts` but are not on
  `THROTTLE_EVIDENCE_REASONS`: the account lockout already bounds code guessing,
  and the per-address rule is for sprays.
- **Once per computer, not once per login** (TRUSTED DEVICES). The owner's
  own complaint the first day he had codes: every sign-in was a trip to the
  inbox. A login that got in with a code answers with a `device_token`; the
  game keeps it (`user://devices.cfg`, apart from the session) and sends it as
  `device`, and a staff login carrying a live one needs no code. Stored as a
  hash, 30 days (`TRUSTED_DEVICE_DAYS`, not sliding), the newest
  `TRUSTED_DEVICES_KEPT` per account, and only at the rank it was trusted at:
  compared with `role_for()`, so a promotion or demotion by any route - the
  staff desk, `set_role.py`, `ELUSION_OWNER` - asks again without any of them
  having to revoke anything. A password change, a recovery reset and
  `/api/auth/logout-all` forget every computer (`_forget_devices()`).

## Name colours, and a guild's history

**A name's colour is the player's; rank is a badge.** `users.name_hue` holds the
hue each player chose (0-359, `PUT /api/account/name-colour`, NULL for "never
chose" so the default lives only in the client). It rides on login, the session
heartbeat, every chat line (a snapshot, like `guild`), the friends list, the
players menu and the guild roster. Staff choose too: rank is shown by the
owner's crown and a MOD / DEV badge the client draws from `role`, which nobody
can choose - so a player who picks the owner's gold is still visibly a player.
`test_namecolour.py`.

**Guilds keep their own history** in `guild_events`: founded, invited, joined,
promoted, demoted, handed on, removed, left, renamed - one line per real change,
in the same transaction, bounded per guild (`GUILD_EVENTS_KEPT`), gone with the
guild, and sent to members only (the newest `GUILD_ACTIVITY_SHOWN` ride on
`guild_dict`). A staff rename is recorded **without the staff member's name**:
members need to know it happened, not whom to be angry at; `staff_actions` keeps
who. The roster also carries each member's latest character, class, level and
area. `test_guildlife.py`.

## Trades

The money of a trade - tax, burn, the supply invariant - is `test_economy.py`.
Everything around it is `test_trades.py`, and each rule below was a defect
found by driving a trade between two real accounts:

- **An offer goes to the character the other player is PLAYING.** The client
  names its slot on the broadcast poll (`stamp_presence(db, slot)` fills
  `sessions.playing_slot`); `PLAYING_SLOT_SQL` is the rule - a fresh session's
  said slot with a save behind it, else the most recent save - and
  `_playing_slot()` runs it. One SQL expression because `/api/players/nearby`
  needs the same answer for twenty accounts inside one query. `to_slot` used to
  default to 0, so a typed name reached the other player's first character.
- **Only to somebody online.** An unanswerable trade blocks both players for
  `TRADE_EXPIRY_SECONDS`.
- **Accept names the revision it saw.** `trades.revision` moves on every real
  change (`_trade_touch`); `trade_confirm` requires it and conditions the UPDATE
  on it - a compare-and-set, not a read-then-write. An identical update is not
  a change and un-accepts nobody.
- **Both bags are flagged when a trade runs** (`saves.resync_trade`, in the
  trade's transaction). Whoever accepted FIRST is not the one whose request ran
  it; their client was told nothing and its next whole-bag
  `PUT /api/character/inventory` deleted what they received. While a flag is up
  that PUT is refused with 409 and the bag the server holds; the trade poll and
  the broadcast poll deliver the same thing (`_take_resync`, cleared on
  delivery). The requester is flagged too, in case their response was lost.
- **Items leave the bag before the hotbar keys** (`keys_last=True`).
- **History** is `GET /api/trade/history`, worded from the caller's side
  (`_trade_record`). Finished trades are kept for good - they are what a
  moderator reads when somebody says they were scammed; cancelled ones are
  pruned after `TRADE_CANCELLED_KEPT_SECONDS`, on the offer route, through the
  partial index `idx_trades_cancelled`. **Partial on purpose:** a plain
  `(state, updated_at)` index was picked by the planner for "find my open
  trade" and turned the most-polled read into a scan of every open trade.
- **Same-second ties break on rowid** in `_trade_find_recent`.

- **Staff read an account's trades** at `GET /api/staff/trades` - every state,
  worded from that account's side, a tally by state, under reach
  (`_moderation_target`: out of reach is the same 404 as no such account).
  Paged by the pair `(updated_at, rowid)` through `_trade_find_page`, one
  bounded index read per (side, state); `_trade_find_recent` is its first page.

**Which character an account is playing** is one rule, `PLAYING_SLOT_SQL`,
and every surface asks it: the trade offer, `/api/players/nearby`, the online
list (and the caller's own "here"), the guild roster, the staff account view's
`playing` flag and a staff teleport's default area. `test_playing.py` holds
the surfaces and scans app.py for any new "saved last" pick.

**Guild chat** has been live since guilds: `chat_write_check` and `read_chat`
gate it on membership, and a line carries its guild in `target_id`. The one
sentence for "no guild", reading or writing, is `GUILD_CHAT_NO_GUILD`. The
client refused the tab locally until it was reported; see the game's CLAUDE.md.

## Trade gates: the owner's switch and the 48-hour hold

E3_SCOPE.md, option B, built 6 Oct. The kill is still the game's word (E-3),
so a cheated kill mints real loot; a trade is the only road from one account
to another, so the gates are on it. `test_tradegates.py`.

- **The switch** is `TRADE_KEY` in `server_settings`, beside maintenance and
  PvP: `POST /api/server/trade` (owner, 404 to everyone else, `{"on": bool}`),
  announced, logged as `trade`, and on `/api/status` as `"trade"`. No row
  reads ON (trading came first); an unreadable row reads OFF.
- **Off refuses only `POST /api/trade/offer`**, with 503 - the door is shut,
  nothing is wrong with the caller, the reading `maintenance_refusal()` made.
  The owner, 6 Oct: "allow trade to finish". An open trade may still be
  changed, accepted, executed or cancelled, and dies after
  `TRADE_EXPIRY_SECONDS` untouched, so off is never a half-done trade. The
  answer says how many are still open.
- **The hold**: a mythic-tier piece or a Perfect roll (`trade_held_kind()`)
  taken from a loot bag or bought from the shop gets a `trade_holds` row per
  piece for `TRADE_HOLD_SECONDS` (48 h) - `place_trade_hold()`, called by
  `/api/loot/take` and `/api/shop/buy`. Not by staff grants or trades. A held
  piece is refused by `/api/trade/update` and again at execution
  (`trade_hold_refusal()`, the same sentence both times, saying when).
- **Counted per account and id, not per cell**: free = owned in every
  character's bag and the bank, less live holds. A piece walked to an alt
  through the bank is still held; an older copy of the same id may go. Two
  pieces with one id are one piece.
- **Placing a hold prunes the account's own finished ones**, never anybody
  else's - so `test_ownership.py` needs no allowlist entry for it.

## Loot: the tier first, then the item

`gamedata.roll_loot_tier()` picks which tier a filled bag slot lands on from
the enemy's `tier_odds` (top first: the enemy's own tier, one below, two
below), and `pick_loot_item()` then picks evenly inside that tier, stepping
DOWN to the nearest tier that has something. It used to be one weight per
item, `2^(max_tier - tier)`, which let the catalogue set the odds: eleven iron
pieces outvoted everything and 58% of a boss bag was iron.

- **Every gear piece can drop.** The amethyst and ember weapons and armour
  were finished items marked not droppable and not sold, so the best reachable
  gear was cobalt. `max_loot_tier` is what keeps them rare: nothing drops above
  it. Fishing rods, cooked fish and pets keep their own `droppable = false`.
- **A boss bag is one piece of gear and sometimes a potion**
  (`slots_are_gear`, `bonus_potion_chance`), at its tier or one below.
  `tier_up_chance` rolls one ABOVE the enemy's tier, for elites.
- **Rarest first.** `rarest_first()` orders a bag pet, items, coins, and
  `LOOT_BAG_CAPACITY` is nine. It was six with the coins first and the pet
  appended last, and a drop's gold is up to four coin entries - so on the old
  rules 76% of boss bags ran past six and 573 of 616 boss pet wins were cut by
  `_create_loot_bag()` without a word. A cut now costs the smallest coin.

**Legendary is rare, and `tier_odds` may hold zeros for it.** Dark normals put
ember on 3% of slots (about one an hour); the fire boss and the Crowned roll
`[0, 0.25, 0.75]` from tier 6 (legendary 1 in 4); the other bosses have a zero
on every ember share. A zero keeps `max_loot_tier` - which also sets gold and
pet odds - unchanged. `test_rewards.py` measures the rates with the real roll.

`test_loot.py` holds the odds, the unlock, the boss bag and the order;
`test_api.py` holds that a boss bag with a pet never loses the pet or its gear
to the cell limit.

**Tier 6 is mythic, and it is a roll of its own.** Day 2 filled it with three
weapons that bring their own attack - the Meteorite (mage), the Double Axe
(warrior) and Dynamite (tank), level 22. No `tier_odds` reaches tier 6 (every
boss whose `max_loot_tier` is 6 has a zero first). Instead every enemy row
carries `mythic_odds`, "one in N", which the game works out
(`EnemyData.mythic_odds()`: a table by tier, bosses apart, and an override) and
exports. The owner's call: regular mobs and bosses both drop them, "mixed
rarity but it should be super rewarding getting 1".

- **`gamedata.roll_mythic(enemy, class_id)`** runs at every kill, beside the
  bag. A win is the killer's own class's piece (`mythic_pool()`). A class with
  no mythic of its own, the healer for now, gets any of them.
- **A win makes a bag** even when the bag roll said no, as a pet does, and
  `rarest_first()` ranks it with the pet, so the cell limit never cuts it.
- **The kill answers `mythic`**, and posts a broadcast of kind `"mythic"`
  (`MYTHIC_BROADCAST_KIND`, `by` = the finder), in the same commit as the bag.
  The game draws it as a red banner for everyone online. The owner's
  announcement route takes only `system` and `shout`, so nobody can type one.
- **The rates**: the Crowned 1 in 150 (about 8 hours of farming it), the
  other bosses 300 to 600, then 20,000 for the dark band down to 500,000 for
  light and wind, with small slimes a quarter of their band's ticket.
  `test_loot.py` holds the order, the rates and the roll; `test_equipment.py`
  holds the kill, the bag, the take and the notice, plus the class and level
  gates. The weapons' damage follows the ladder with the Double Axe as the
  tier's sword.
- **The kill is still the client's word** (SECURITY_NOTES E-3). The ceiling
  bounds how many kills a modified client can claim, so it bounds its mythic
  rolls too, but it does not stop them.

## The store sells iron to amethyst

Decided by the owner on day 1: the general store stocks every weapon and armour
piece from iron to amethyst (tiers 1-4) at its value, so a player farming one
band saves for the next band's set, and the fishing worm and the iron to
amethyst rods, without which only the iron rod could be had and worms came one
at a time from loot. Ember, armour or rod, is still found, never bought. The
stock list is the game's `generalstore.tres`, read here through gamedata.json,
so a stock change is a re-export, a copy and a restart, with no code change.

- **Anyone may buy any piece.** `/api/shop/buy` has no level gate on purpose;
  the equip routes refuse a piece until the character reaches its
  `required_level`.
- **The pace is measured, not assumed.** `test_pacing.py`, "THE STORE SELLS
  IRON TO AMETHYST", rolls kills through the real `roll_kill_rewards()` with a
  seeded generator swapped in for `_rng`, and holds each next set at 5 to 9
  hours of the previous band's gold (about 6 to 8 today). It also holds the
  shelf against the catalogue: every tier 1-4 piece on it, nothing of tier 5.
- **The 5 Oct price pass** (the owner: a few lucky kills or a good fishing
  trip made the old prices trivial): every weapon, armour piece, amulet and
  rod doubled, every potion quadrupled, worms, fish, gold and pets left alone.
  Sets were 2 to 6 hours. Potions were a quarter of what a cooked fish of the
  same tier costs per point healed; now they match, within a fifth, at every
  tier, and `test_pacing.py` holds that too. Gold drops do not read item
  values (`GOLD_TIER_RATIO` in the game), so a price change is a pure sink
  change. It also moves the trade tax, which is 5% of `value`.
- **The shop buys** (5 Oct). `POST /api/shop/sell` takes one carried cell
  and the item the game saw in it (ONE CELL AT A TIME), prices it with
  `gamedata.shop_sell_price()` - `value` times the shop's `sell_multiplier`
  (0.05), rounded down, never at or above what the same shop charges - and
  MINTS the gold through `gold_delta()` under `shop_sell`. Money, pets and
  quest items are refused (`SELLABLE_TYPES`). `GET /api/shop/<id>` lists
  `sell_prices` from the same function. The rate is measured, not guessed:
  `test_pacing.py` holds an hour's drops sold at a tenth to the whole of the
  hour's coin, and the next set four to nine hours away even selling
  everything. **Selling turns an item grant into gold**, which is one reason
  `/api/staff/grant` is owner-only (since 6 Oct 2026; it was mod and up, and a
  mod could give themselves potions and sell them, or a Perfect mythic and
  trade it on). The whole-bag and whole-bank writes went with it - see "The
  owner is the only item source" below.
- **Tests read prices from the catalogue.** `test_economy.py` and
  `test_equipment.py` had a potion's 50 typed in, and the price pass turned
  seven checks red that were about the ledger and the tooltip, not the price.

## Quality rolls: the roll is in the id

The owner, 5 Oct: "random stats on all items because it gives loot a better
value if a rare max roll". A dropped piece of gear rolls every stat it has
above zero - damage, armour, max health, max mana, the damage bonus - each on
its own, `QUALITY_LOW` to `QUALITY_HIGH` (85-115) percent of the catalogue
number, from a triangle peaked at 100. One drop in `QUALITY_PERFECT_ODDS` (100)
is Perfect, every stat at `QUALITY_PERFECT` (120). Gear bought from the shop
rolls the same way, at the till. QUALITY ROLLS in gamedata.py; the numbers and the
letters come from the game through gamedata.json.

- **The roll is part of the item id**: `jadechest~a104h96`. That is the whole
  design, and the reason is everything that did not change: carry, bank, loot
  bag entries, the equipment map, trade rows and staff whole writes all hold
  an id, and every route that moves one checks "the item the game saw in that
  cell" - so a rolled piece moves by the paths that exist, and those checks
  cover the roll. No migration: a piece from before is a plain id, 100%.
- **`gamedata.item_row(id)` is the one way to ask what an id is.** A rolled id
  is not a key of `ITEMS`, and `ITEMS.get("ironsword~d107")` is None - an item
  nobody has heard of: `_stack_limit()` gave it 9,999, a trade offer said "No
  such item", `equip_slot_for()` gave it no slot. Every lookup by id in
  gamedata.py and app.py goes through `item_row()` / `has_item()` now, which
  hand back the base row with the rolled stats scaled, `base_id`, `rolls` and
  `perfect`, and "Perfect " on a Perfect name. Iterating `ITEMS` (the shelf,
  the loot pool) is still right. **`test_quality.py` Q-7 scans app.py and
  gamedata.py for an `ITEMS` lookup by id** and fails on a new one.
- **`split_variant()` is strict**: every stat the piece has, once, in
  `QUALITY_FIELDS` order, 85-115 each or 120 all. One roll has one spelling,
  or two spellings of it would be two items to every cell check.
- **`scale_stat()` is integer arithmetic, halves up** - `ItemRegistry` does
  the same sum in GDScript, and `gear_bonuses()` derives max health from it.
  Both suites hold one table of pairs.
- **A roll changes what a piece does, not its price.** `value` is the
  catalogue's, so `shop_sell_price()` pays a roll its base piece's price
  (capped by what the shelf charges for the base - the shelf never holds a
  roll) and the trade tax is 5% of the same number.
- **Where a roll is made**: `build_bag_contents()` for every gear slot,
  `roll_kill_rewards()` for the mythic, and `/api/shop/buy` for every piece it
  sells (`roll_quality()`, which reads `_rng` when called, so a test's seeded
  generator reaches it).
- **The shop rolls at the till.** The owner, 5 Oct: "item stats say ? and
  are revealed upon buying in shop only". The shelf lists the catalogue id
  and its price (a roll cannot be bought by name: `shop_price()` has none),
  and the purchase hands over `roll_quality()` of it at that price, whatever
  it rolls - so an average purchase is the catalogue piece, as it was, and a
  Perfect is a hundred purchases on average. The answer's `item_id` is the
  roll, `stock_id` the shelf's id, and the ledger line names the roll.
  `/api/staff/grant` takes `"quality": "plain" | "roll" | "perfect"` ("store",
  which the first build-3 game sends, is read as plain), and
  refuses an id that looks like a roll and is not one (an unknown plain id is
  still stored as typed - ids are not whitelisted).
- **The broadcast names the piece, not the id**: `mythic_find_text()` reads
  `item_row()`, so a Perfect find says "found the Perfect Meteorite".
- **`CURRENT_CLIENT_BUILD` is 3**: a build-2 game reads a rolled id as its
  error item. Raise the minimum to 3 once the new game is out.

`test_quality.py`. test_loot, test_rewards and test_equipment read dropped
ids through `item_row()` / `base_id()` for the same reason the app does.

## Seeing each other: presence.py, a second process

The owner, 5 Oct: "i want to make other players see each other" - over a live
connection, showing the body walking and idling the right way round, the name
over the head, their attacks and their pet following. `presence.py` is a small
asyncio WebSocket server on 127.0.0.1:5001, path `/ws/presence`, run as its own
service beside gunicorn. Its header holds the wire; `test_presence.py` holds
every rule.

- **Why not a route.** gunicorn runs two workers that share no memory and
  eight threads between them; a socket held open for a session would hold a
  thread. One asyncio process keeps every connection in one place, in memory.
  A position is worth something for a tenth of a second, so none of it is
  written to the database.
- **It never sees a login.** `POST /api/presence/ticket` (behind
  `require_auth`, `{"slot"}`) writes a ticket to `presence_tickets` - stored
  hashed, tied to the session that asked, two minutes long - and answers
  `{ticket, expires_in, socket_url}`. The socket takes the ticket. Every rule
  about who may sign in stays here in app.py: a ban, a logout, a login
  elsewhere or a password change deletes the session, and presence.py drops
  the connection within `SWEEP_SECONDS` without app.py knowing it exists.
- **Who somebody is comes from the ticket**, written from this file's own
  rows: name, class, level, rank (`role_for`), name hue, guild tag and the
  pets the character holds (carry for that slot, plus the bank). The game says
  only where it is (area, x, y), which animation its body plays, which aura is
  lit and which pet is out - and a pet it does not hold is dropped. The game
  renews the ticket every minute, which is how a level-up or a new guild
  reaches other screens.
- **`socket_url`**: `ELUSION_PRESENCE_URL` when set; behind the proxy
  (`ELUSION_TRUSTED_PROXIES` above 0) the same host the request came to, under
  `/ws/presence`, `wss://` for an https page - one address for the page, the
  API and the socket, which the browser build needs; run locally, the
  presence port on the host the game used.
- **Limits**: one connection per account (a second replaces the first), 30
  messages a second with a burst of 60, 2 KB a message (64 KB for a leader's
  world message; anything else that big is closed with 1009, "too big"), 1000
  connections, hello within 5 seconds. Moves are relayed ten times a second
  per area.
- **And, since 0.7.0, the monsters** - next section. PvP stays in "Decided,
  not built".

## Shared monsters: one game runs them, presence.py passes the notes

The owner, 6 Oct: "shared monsters separate loot bags". Everyone in an area
fights the same monsters. The server does not simulate them - it has no map,
no collision and no AI, and growing those here is E3_SCOPE.md's option C, "a
season". Instead one GAME runs each area's monsters and presence.py decides
only who that is and who hears what. The game side is the game repo's
`src/world/monstersync.gd` and CLAUDE.md, "Shared monsters".

- **Only games that say so share.** A hello with `"v": 2` (`SHARED_VERSION`);
  the welcome says the server shares too. A game without it (a build from
  before 0.7.0) is never made leader, never sent monsters, and carries `"v": 0`
  in its join entry so a leader's monsters do not chase it - it fights its own,
  as before.
- **The leader** of an area is the sharing game that walked in earliest
  (`Player.joined_at`). `self.leaders[area]`. Everyone sharing the area hears
  `{"t": "lead", "a", "id", "n"}` when it changes or when someone comes or goes
  (`n` = how many others share, so a leader alone sends nothing). When the
  leader leaves - another area, a closed socket, a drop - the next in line is
  named at once.
- **`w`, world, is the leader's alone.** `relay_world()` passes it on exactly as
  sent (the raw text, never re-encoded), to everyone else sharing the area or
  to one game (`"to"`); from anybody else it is dropped, so a game that was
  leader a moment ago cannot move monsters on screens it no longer runs. What
  it says is the games' business.
- **`h`, hits, go to the leader and nowhere else**, with who sent them, after
  `clean_hits()`: `[[monster, damage, element]]`, whole numbers (not bools),
  damage 1-`MAX_HIT`, at most `MAX_HITS_PER_MESSAGE`.
- **`need`.** A sharing game entering an area, or saying `sync` (its world was
  rebuilt), makes the server ask the leader to send it everything.
- **Kills are unchanged.** Each game whose player hit a monster reports the
  kill through `/api/combat/kill` itself, and is paid from the server's own
  roll - which is what "separate loot bags" means. The E-3 ceilings are per
  account, so two players sharing a kill each stay inside their own.
- **The honest limit.** The leader is trusted with the monsters the way every
  game is already trusted with its own kills (E-3): a cheating leader can move,
  heal or kill the monsters on everyone's screen, or set the game's own
  monsters on somebody, and nothing more than a cheating game could always do
  to its own. It cannot touch anyone's health, bag or gold - those stay on each
  game and on this server - and each follower builds every attack from its own
  copy of the monster and caps the few numbers a `w` carries
  (`monstersync.gd`, `safe_spike()` and `safe_pillar_damage()`), so it cannot
  make one hit harder either. This server never reads a `w`, so none of that
  is checked here.

`test_presence.py` P-7.

## Gear bonuses: a character's maximum includes what it wears

Amulets add max health (`bonus_max_hp`), max mana (`bonus_max_mana`) or a
damage percent (`bonus_damage_percent`, applied by the client). The first two
are the server's business because `max_hp` and `max_mana` are server-derived
and the status route clamps hp to them.

- `gamedata.gear_bonuses(equipment)` sums the three fields over what is worn,
  counting a piece **only in the slot it is worn in** and flooring negatives.
  `max_stats_for(class_id, level, equipment)` adds hp and mana to the curve.
- **app.py derives maxima only through `_derived_stats(row)`**, which reads the
  row's own `equipment` with `row["equipment"]` - a SELECT that forgot the
  column fails loudly rather than deriving a bare maximum. Save, status, kill,
  revive, respawn and both equip routes use it; `test_gearbonus.py` fails if
  `gamedata.max_stats_for(` is called anywhere else.
- **The equip routes move the maxima in the same transaction**
  (`_apply_worn_maxima`) and bring hp/mana DOWN to them, never up. Taking off a
  Vitality amulet at full health leaves you at the lower full. The response
  carries `stats` so the client can see it agrees. The save route clamps the
  same way when it rewrites the maxima.
- The healing reconciler still applies: raising the ceiling does not grant the
  health in it, regeneration does, at the rate the new maximum allows.
- **It is not only amulets.** Every gear piece from jade up carries a bonus now
  (plate health, cloth mana, weapons damage, plain rings and amulets all
  three), so a full ember warrior is +150 max health on the server too.
  `test_rewards.py` holds the sums.

## Levels: the curve is derived, never read back

The curve is 1,250 x 1.27 (eight hours to level 22) and comes from
gamedata.json's `xp_base` / `xp_growth`.

- **`apply_xp()` derives the requirement from the level** (`need(level)`) and
  ignores the stored `xp_to_next`. A row still holding the table's DEFAULT 100
  would otherwise give a new character a first level twelve times too cheap.
  Skills pass their own curve with `need=`; before that argument existed, a
  skill grant crossing two levels priced the second on the character curve.
- A new character is written `xp_to_next = xp_needed_for_level(1)`, and
  `_migrate_xp_to_next_from_curve()` rewrites every row's display copy on boot
  (idempotent, skipped if gamedata.json did not load, never touches xp).
- **A boss is `slots_are_gear`, not "1,000+ health".** killwatch used health,
  and a normal dark bush mage now has 1,555; `killwatch.is_boss()` falls back
  to health only for a catalogue without the field.
- **A small slime's kill ceiling is eight per placed large** (the exporter's
  `placed_count`). The large grants nothing and a kill claim for it is 400.
- **`placed_count` is every scene added together.** Day 2 the game gained a
  second field, the Big Field, and most ceilings rose about fivefold (one
  light sprite in the world became five). A player is only ever in one area,
  so this loosens the ceiling and never refuses an honest kill. Copy the new
  gamedata.json across whenever a scene gains enemies, or the server judges
  that scene's kills against the old, lower ceiling. A test that needs a
  breach works it out from the file: `test_killwatch.py` said "15 kills, the
  ceiling is 11" and went quiet the day the ceiling became 55.

`test_pacing.py` holds all of it, including the eight hours.

## Attack XP: banked at the kill, with the class specialty

Attack trains only at `/api/combat/kill`. `proficient_amount(class_id, skill,
raw)` is the one rule for `SKILL_PROFICIENCY` - the kill and `/api/skill/train`
both call it - so a warrior banks 1.5x the enemy's `attack_xp_reward`. It used to
be applied only by the train route, which never handles attack, and the client
showed the 1.5 (plus per-hit attack XP of its own) that the server never banked.

The kill answer carries `attack_xp_gained` (what was banked), `attack_level`,
`attack_xp` and `attack_xp_to_next`; the client copies those onto its bar rather
than adding up its own. `kill_reports.attack_xp` records what was paid.
`test_attackxp.py`.

## A signup is not a spray

The client has no "create account" form - it logs in and registers only after
the 401 - so every new account starts with a `no-such-user` row, which is
throttle evidence. Six signups behind one address inside `IP_WINDOW_SECONDS`
tripped `IP_MAX_USERNAMES` and locked that address out of logging in for
`IP_LOCKOUT_SECONDS`: a household, a school, a carrier's NAT - or everyone, if
this runs behind a proxy with `ELUSION_TRUSTED_PROXIES` at 0.

`register()` relabels **that name's** `no-such-user` rows from **that address**
inside the window to `signup-first-login`, which is not on
`THROTTLE_EVIDENCE_REASONS`. Relabelled, not deleted: the log keeps them. A
spray of names that never become accounts, or of wrong passwords against real
ones, still counts - `test_throttle.py`, "A SIGNUP IS NOT A SPRAY".

**The maintenance notice keeps its countdown.** `post_broadcast()` trims to
`MAX_BROADCAST_LENGTH`, so a long owner message used to push "(closing in 60s
- ...)" off the end; the message gives way now, with an ellipsis.
`test_maintenance.py`.

## The owner is the only item source

Every item in the game comes from the server rolling it (a kill, a catch, a
cook, a shop sale) - except what the owner makes for testing. Since 6 Oct 2026
that is the ONLY exception; before, any staff rank had three ways to make an
item from nothing, and each was justified by the others:

- `POST /api/staff/grant` was `require_role("mod")`. Now `require_owner`, with
  the same 404 to everybody else, so a refusal does not confirm the route.
- `PUT /api/character/inventory` wrote a mod's whole bag, "because a mod can
  already self-grant". Now only the owner's (`write_inventory()`); a mod's is
  ignored with `"ignored": ["inventory"]`, exactly as a player's is.
- `PUT /api/account/bank` the same (`write_bank()`).

The skill-ceiling exemption in the skills route moved to the owner too, for
symmetry; that route writes nothing, so it changed no behaviour.

The game follows: `Api.DEBUG_KEYS_MIN_ROLE` is `"owner"`, so F1-F7 and the
Ctrl-letter keys stay quiet for a mod rather than firing requests that 404.
`/api/staff/powers` says it in words on both sides - the mod's notes say they
cannot create items, the owner's that they are the only one who can - and the
route itself moves to the owner's list on its own, because that list is read
from the decorators. `test_api.py` (STAFF GRANTS), `test_security.py` (E-1)
and `test_moderation.py` (M-10).

## Decided, not built: staff commands

**Put the required rank on the command definition, not in the handler.** A
registry of (name, minimum rank, function) gives one place to audit who can do
what, lets help list only what the caller can use, and means adding a command
cannot silently forget the gate. Handlers that each guard themselves are how one
ends up ungated.

**Sort commands by reversibility, not by seniority.** The question is not
whether the person is trusted - the rank already answered that. It is whether
the command can create value from nothing or remove a person. `/who`,
`/inspect`, `/teleport` observe or move and delegate fine. `/give`, `/setlevel`,
`/ban` mint items, XP or absences.

**Item spawning is gated on the SERVER, not on a rank.** The danger is the live
economy, not the person. `/give` should not exist on the production build even
for the owner - an environment flag for a test server, the same trick as
`ELUSION_OWNER`. A rank you hold is a rank you can use at 2am.

*Half of this is built (6 Oct 2026):* no rank below the owner can create an
item - see "The owner is the only item source". The environment flag that
would take it from the owner on production is not; the owner tests on the live
server, and chose to keep the tool there.

**Staff-created danger a player did not choose cannot take anything.** This is
the fairness rule, and it is specific to this game: `gameover.gd` clears carry
items and gold on death, so a boss dropped on top of a town is a staff member
spending players' money on their own entertainment. `EnemyData` already has
`grants_rewards` for an enemy that must not pay out; the symmetric flag is one
that must not charge either.

Danger a player walked INTO can take and give normally. Consent is the whole
difference, which makes an announced portal the right mechanic rather than a
summon - stepping through is the consent, and it sorts the audience for free.

## Layout

```
app.py          every route, the schema, the migrations
envfile.py      reads a .env beside the code; first thing app.py and wsgi.py do
wsgi.py         what gunicorn serves: a preflight of the settings, then app
gamedata.py     loot rolls, XP curve, stat curves - the game's rules
gamedata.json   exported from the Godot project, NOT hand-edited
presence.py     the presence socket, a second process: who stands where,
                and whose game runs each area's monsters
test_*.py       discovered and run by run_tests.ps1, which prints how many.
                No counts here on purpose - see above. What each one is FOR:
  api           the broad one; read its header before adding to it
  economy       the gold ledger and the supply invariant
  security      server authority - inventory, skills, gold, lusions, item
                use, revive, unexplained healing, accepting death
  ownership     no route lets you name somebody else's row
  refusals      which code a refusal answers with, and why 404 not 403
  revocation    bans, demotions and what a token stops buying
  moderation    the paged staff list, the moderation log, staff-only notes,
                and the powers list in words
  namecolour    the colour each player chose, carried with every name
  guildlife     who guild members are playing, and the guild's own history
  throttle      login defences, rate limits, credential rotation, and
                that a signup is not a spray
  staffcode     staff logins need the code from their email
  accounts      signing in: one game per account, the miss that locks,
                no recovery-email demand with no mail
  chatsafety    ignore, report and mute
  chardelete    deleting a character, and only the character
  concurrency   requests that arrive together: the write lock
  attackxp      attack XP banked at the kill, with the class specialty
  pacing        the level curve, the element bands, and the store's saving pace
  gathering     fishing and cooking - the item-minting endpoints
  loot          bags, rolls, and taking things out of them
  equipment     what a worn item is worth, and what the tooltip says
  quality       dropped gear's rolled stats, carried whole by every path
  presence      the presence socket: tickets, areas, what the game may say,
                limits, a login ended anywhere ending the connection, and
                who leads each area's monsters (P-7)
  tradegates    the owner's trade switch, and the hold on fresh mythic and
                Perfect finds
  chat/chatrooms  the feed, the channels, moderation and picture revocation
  broadcast     the server's voice, and the poll that doubles as a heartbeat
  maintenance   the kill switch and its countdown
  security_doc  the docs against the code: SECURITY.md's claims, and that
                DEPLOY.md names the runner rather than a list of suites
  deploy        what the first live deploy found: the .env read first, and
                backups that are one file each
  guilds / friends / mail / map / teleport / recovery / settings /
  healing / equipmove / skill_train / catalogue / killwatch
set_role.py     sets an account's rank; --list shows every account
backup_db.py    the nightly snapshot: consistent, verified, one file each
restore_drill.py  serves a backup copy with a real server and logs in to it
canary.py       reads the LIVE database on a schedule: do the numbers add up
killwatch.py    reads the LIVE database on a schedule: is anyone claiming kills
                the world cannot produce
deathwatch.py   read-only, run by hand: why the board's deaths column says what
                it says - the counter, every character's stored hp, and every
                revive in both ledgers with a date
```

`set_role.py` talks to the database directly rather than through a route, and
that is on purpose: a rank can only be granted from the machine holding
`elusion.db`, never over the network. Whatever replaces it should keep that
property. `owner` is not settable there at all - it comes from `ELUSION_OWNER`,
so no write to this table grants the top rank.

`gamedata.json` is generated by `src/tools/exportgamedata.gd` in the game repo
from the `.tres` resources. If the curves here disagree with the game, that file
is stale — re-run the exporter rather than editing it.

**THE EXPORTER DOES NOT WRITE TO THIS FOLDER.** It writes
`res://data/gamedata.json` inside the Godot project, and the server reads the
copy sitting next to `gamedata.py`. Running the export and then wondering why
nothing changed is the trap; the file has to be copied across afterwards, or
`ELUSION_GAMEDATA` pointed at the game repo's copy.

Two things depend on the copy being current in a way that is quiet when it is
not. `gamedata.regen_rate_for()` and `restore_for()` back the healing
reconciler, and both fall back to defaults when the fields are absent, so a
stale file makes the check measure against numbers nobody is keeping in step
rather than fail. `app.py` logs one warning at boot when the regen constants
are missing — that line is the tell.

## The wire rule

**Anything that creates, destroys or moves an item is a server endpoint.** The
client sends what it DID, never what it now HAS.

`POST /api/loot/take` is the model: it picks the item, writes `carry_items`
itself, deletes the bag entry in the same transaction and returns the resulting
inventory. The client renders the answer rather than computing it.

The test for a new endpoint: **could a malicious client profit by lying in this
request?** If yes, the decision is on the wrong side of the wire. "I fished"
is a fact about the player. "I caught a trout" is a decision, and decisions
belong here.

FISHING AND COOKING WERE BUILT THIS WAY AND IT COST NOTHING. The rod and bait
are checked server-side, the catch is rolled server-side, the XP is granted
against items the server itself consumed - and `PUT /api/character/skills` now
DROPS fishing and cooking from whatever the client sends, because a routine
client sync would otherwise overwrite a grant made seconds earlier. They are the
worked example for every skill that still needs this. Shops are the remaining
one. See `docs/inventoryauthority.md`.

`PUT /api/character/inventory` predated the rule and was brought under it in
two steps. First a ledger: `_report_unexplained_gains()` ran in SHADOW MODE -
logging `[LEDGER]` lines and refusing nothing - until the comparison had been
proven right against real play, and only then trimmed gains (the bank PUT got
the same treatment). Then the rule itself: a player's bag and bank are never
written whole any more, and every change is a one-cell route (ONE CELL AT A
TIME). The ledger functions went with it.

## Known gaps

- ~~Three of the six skills have no server-side XP grant.~~ **This is no longer
  a gap and has not been since `/api/skill/train` landed.** All six are granted
  server-side and `PUT /api/character/skills` drops every name it accepts, so a
  client claim earns nothing. It is struck through rather than deleted because
  the same stale fact was ALSO sitting in the comment above `MAX_SKILL_LEVEL`
  and in the README's findings table, and a list headed "Known gaps" is the
  worst place in the repository to leave one: a reader who checks anything here
  checks it here first. Defense, agility and magic were the hard three —
  defense trains on damage taken, agility on distance moved — and the answer was
  to have the client report raw activity and the server clamp it to a generous
  per-second ceiling times elapsed time, rather than to find an event that did
  not exist.
- The kill EVENT is asserted rather than verified. The server rolls the rewards
  and rate-limits the reports, so this caps the speed of the fraud, not its
  existence. It is now at least RECORDED - `kill_reports` logs every claim the
  server paid out, in the same transaction, and refused kills leave no row. That
  is groundwork, not a fix: verification needs the client to register spawns.
  The deepest open finding; see SECURITY_NOTES.md (E-3).
- `app.run()` is Flask's development server even with debug off. Right for local
  play, wrong for anything public; a real deployment needs a WSGI server, TLS,
  and `ELUSION_TRUSTED_PROXIES` set to match - see `client_ip()`.
- The Werkzeug debugger has ONE switch: `ELUSION_DEBUG=1`, local only.
  `FLASK_DEBUG` or `flask run --debugger` without it, or any debug flag with a
  proxy configured, makes app.py refuse to start; and a request that arrives
  through the interactive debugger anyway gets a 503 before any route runs.
  Keep all three in `debugger_refusal()` / `_refuse_under_unasked_debugger()`
  rather than adding a fourth check somewhere else. SECURITY_NOTES.md, E-4c.
- The owner panel's view button now has its endpoint: `GET /api/staff/user/
  <name>` behind `@require_role("mod")`. It returns rank, ban state, characters,
  grouped kill reports, a login summary and the staff history. IP ADDRESSES ARE
  GATED BY can_act_on(): a mod sees a player's addresses and not another mod's,
  and nobody sees the owner's - the count is always shown, the addresses only to
  someone who could act. The owner panel calls it now (ownerpanel.gd); results still print to the
  console rather than into the panel, which needs scene work.
