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

262 checks, exits non-zero on any failure, and it builds a throwaway database in
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
test_api.py        422 checks; read the header before adding to it
test_economy.py    279 checks; the gold ledger and the supply invariant
test_security.py   131 checks; server authority - inventory, skills, gold,
                   lusions, item use, revive, unexplained healing
test_throttle.py    44 checks; login defences, rate limits, credential rotation
test_gathering.py   44 checks; fishing and cooking - the item-minting endpoints
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

- Three of the six skills - defense, agility, magic - still have no server-side
  XP grant, so a client can claim any level up to `MAX_SKILL_LEVEL`. They are
  the hard three: defense trains on damage taken and agility on distance moved,
  so neither has a server-visible event to grant against, and a bound derived
  from character level would clamp honest play. Attack, fishing and cooking are
  server-owned; see SERVER_OWNED_SKILLS and its comment.
- The kill EVENT is asserted rather than verified. The server rolls the rewards
  and rate-limits the reports, so this caps the speed of the fraud, not its
  existence. It is now at least RECORDED - `kill_reports` logs every claim the
  server paid out, in the same transaction, and refused kills leave no row. That
  is groundwork, not a fix: verification needs the client to register spawns.
  The deepest open finding; see SECURITY_NOTES.md (E-3).
- `app.run()` is Flask's development server even with debug off. Right for local
  play, wrong for anything public; a real deployment needs a WSGI server, TLS,
  and `ELUSION_TRUSTED_PROXIES` set to match - see `client_ip()`.
- The owner panel's view button now has its endpoint: `GET /api/staff/user/
  <name>` behind `@require_role("mod")`. It returns rank, ban state, characters,
  grouped kill reports, a login summary and the staff history. IP ADDRESSES ARE
  GATED BY can_act_on(): a mod sees a player's addresses and not another mod's,
  and nobody sees the owner's - the count is always shown, the addresses only to
  someone who could act. The owner panel calls it now (ownerpanel.gd); results still print to the
  console rather than into the panel, which needs scene work.
