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
each of five suites. There are twenty-six now, and every one of those numbers
was wrong - `test_api.py` alone had gone from 262 to 454. A number in a comment
cannot fail, so it stays wrong until somebody trusts it. `run_tests.ps1` counts;
this file does not.

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
`gold_delta()` under the reason `death`, empties `carry_items`, refills the
three pools from the class curve, and writes a `consume_grants` row so the
client's next sync is explained instead of clamped.

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

### JSON has no integer type

The client is Godot, which parses every JSON number as a float. `88` goes out
and `88.0` comes back. Anything the client sends is coerced on arrival —
`parse_stat()`, `int()` inside a try — and anything sent to it should be a real
int, because Godot's comparisons are type-strict and a float that should be an
int has cost this project a full day already.

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
- **Item ids are not whitelisted.** Validating them against a list would mean
  every new item in the game needs a matching server deploy. Unknown ids are
  bounded by `QUANTITY_CEILING` instead; known ones by their own `max_stack`.
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
  to name another account.
- **`GET /api/chat/image/<image_id>` has no ownership check on purpose**, and it
  is the only route that does not. The id is the SHA-256 of the bytes, so 256
  unguessable bits *are* the permission. That argument rests entirely on
  properties of the id, so `test_ownership.py` asserts them: 64 hex, 256 bits not
  a truncation, and equal to the hash of the bytes served. Make the ids
  sequential or truncate the hash and the route becomes a textbook IDOR without
  a line of it changing.
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
gamedata.py     loot rolls, XP curve, stat curves - the game's rules
gamedata.json   exported from the Godot project, NOT hand-edited
test_*.py       twenty-six suites, discovered and run by run_tests.ps1. No
                counts here on purpose - see above. What each one is FOR:
  api           the broad one; read its header before adding to it
  economy       the gold ledger and the supply invariant
  security      server authority - inventory, skills, gold, lusions, item
                use, revive, unexplained healing, accepting death
  ownership     no route lets you name somebody else's row
  refusals      which code a refusal answers with, and why 404 not 403
  revocation    bans, demotions and what a token stops buying
  throttle      login defences, rate limits, credential rotation
  gathering     fishing and cooking - the item-minting endpoints
  loot          bags, rolls, and taking things out of them
  equipment     what a worn item is worth, and what the tooltip says
  chat/chatrooms  the feed, the channels, moderation and picture revocation
  broadcast     the server's voice, and the poll that doubles as a heartbeat
  maintenance   the kill switch and its countdown
  guilds / friends / mail / map / teleport / recovery / settings /
  healing / equipmove / skill_train / catalogue / killwatch
set_role.py     sets an account's rank; --list shows every account
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

`PUT /api/character/inventory` predated the rule and has since been brought
under it: `_report_unexplained_gains()` ran in SHADOW MODE first - logging
`[LEDGER]` lines and refusing nothing - until the comparison had been proven
right against real play, and only then started trimming. Shipping the refusal
first would have broken honest saves. `PUT /api/account/bank` has since had the same
treatment and reconciles against `bank_items`: it can reorder the bank, not
stock it. Both share one `_trim_to_recorded()` rather than two copies of the
rule.

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
