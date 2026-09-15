# Working on the Elusion API

Read this before changing anything. Everything in it cost real hours to learn.

This is the Flask service behind Elusion RPG, a Godot game in a separate
repository. It owns the things a client must not be trusted with: level, XP, the
derived stat maxima, every loot roll, and loot bags. `docs/apicontract.md` in
the game repo is the agreement between the two — change that first, then both
sides.

## How to work in here

**Run the tests before and after. Every time.**

```
.\venv\Scripts\python.exe test_api.py
```

365 checks, exits non-zero on any failure, and it builds a throwaway database in
your temp folder so it never touches `elusion.db`.

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
  the same 404, so there is nothing to learn by asking.
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

Built. `POST /api/staff/ban`, `POST /api/staff/unban`, `PUT /api/staff/role`,
`GET /api/staff/users`.

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
test_api.py     365 checks; read the header before adding to it
set_role.py     sets an account's rank; --list shows every account
```

`set_role.py` talks to the database directly rather than through a route, and
that is on purpose: a rank can only be granted from the machine holding
`elusion.db`, never over the network. Whatever replaces it should keep that
property. `owner` is not settable there at all - it comes from `ELUSION_OWNER`,
so no write to this table grants the top rank.

`gamedata.json` is generated by `src/tools/exportgamedata.gd` in the game repo
from the `.tres` resources. If the curves here disagree with the game, that file
is stale — re-run the exporter rather than editing it.

## Known gaps

- The backpack ledger is still client-asserted. `POST /api/loot/take` closed
  where items come from, not what a client claims to hold.
- `app.run(debug=True)` is Flask's development server. Right for local play,
  wrong for anything public; a real deployment needs a WSGI server in front.
- There is no staff read endpoint yet. `GET /api/staff/user/<name>` behind
  `@require_role("mod")` is what the owner panel's view button needs; it
  currently says it is not built rather than showing an empty result.
