# Security Notes — Elusion API

> **Looking for the security model?** It is one page: **[SECURITY.md](SECURITY.md)**
> — the premise, who is trusted with what, the invariants, each naming the test
> that holds it, and the honest limits. This file is the *findings log* behind it:
> every finding, what it let an attacker do, and what happened to it since. Long
> on purpose, and not the place to start.

This started as a self-audit: I attacked my own API as a logged-in player with a
modified client, wrote down what I could get away with, and named five findings.
This file is the record of that audit **and** of what happened to each finding
since. The original attacker's-eye account is kept below because the reasoning is
the point — the fixes only make sense against the thing they fix.

## Status

Two columns are there for whoever reads this later — me in six months, or
someone deciding whether to trust the server. **Risk if exploited** is what an
attacker actually gets, in one sentence. **Fix** names the *kind* of control
first, so the pattern is visible at a glance, then says in a line where it
lives.

| # | Finding | Severity | Status | Risk if exploited | Fix |
|---|---------|----------|--------|-------------------|-----|
| E-1 | Inventory is client-authoritative | critical (economy) | **Closed** | A modified client can write any item into its bag or bank — the best gear in the game, in any quantity — on its next save. | **Server-side authority.** Every save was reconciled against what the server actually granted and fabricated items trimmed away (shadow mode first, enforced once proven). Since then the bag and bank are the server's outright: every move, bin and cashed pile is its own request, and a player's whole-bag write is ignored. |
| E-2 | Skills are client-authoritative and uncapped | critical (balance) | **Closed** — all 6 skills server-owned | A player can claim any skill level and skip all progression — level-99 attack on a character made a minute ago. | **Server-side authority.** All six skills are granted only by the server and dropped from client saves. Fishing, cooking and attack ride their own events; defense, agility and magic report activity to `/api/skill/train`, which clamps each to a generous per-second ceiling × elapsed time and applies class proficiency itself. A client claim earns nothing. |
| E-3 | The kill *event* is asserted, not verified | high | **Open** — named, rate-limited, not fixed | A script can report kills it never fought and farm XP and loot without playing. | **Rate limit + content bound — not a fix.** The server rolls its own rewards, refuses reward-less enemies, rate-limits with a token bucket, and caps kills at what the world's respawners can produce (18,000/hr down to 5,208). Closing it needs server-side encounter state. |
| E-4 | `app.run(debug=True)` | critical if exposed | **Closed** — every door, not only `python app.py` (E-4c) | Any crash on a reachable server hands an attacker a Python shell on the machine holding every password hash. | **Safe default + refusal.** The debugger is off unless `ELUSION_DEBUG=1`; the server refuses to start if `FLASK_DEBUG` or `flask run --debugger` asks for it instead, or if any debug flag is set behind a proxy; and it refuses every request if the interactive debugger is attached some other way. |
| E-5 | No login throttling | medium | **Closed** — incl. E-5f, the throttle that fed itself | Passwords can be guessed at machine speed — one account at a time, or one common password across every account. | **Rate limit.** 8 misses lock an account for 15 minutes, checked before the password; per-address ceilings (6 usernames, 25 failures per 10 minutes) stop spraying. E-5f: the gate's own refusals no longer count as evidence, which had locked shared connections out indefinitely. |
| E-6 | Long token with no credential rotation | low–medium | **Closed** | A leaked 30-day token keeps working for a month, and changing the password did not end it. | **Revocation + rotation.** Log out everywhere; a password change re-hashes, ends every session and issues one new token in a single transaction, and needs the current password — a token alone is not enough. |
| E-7 | Item *use* is client-authoritative | low–medium (balance) | **Closed** | Any player can use gear and potions their level or skills do not allow. | **Server-side authority.** `POST /api/character/consume` checks requirements against the server's own rows and destroys the item itself; the client applies the effect only on a 200. |
| E-8 | **A client could set its own gold** | **critical (economy)** | **Closed** | A player can mint infinite gold, destroying the economy and progression. | **Server-side authority.** `gold` joined `SERVER_OWNED_STATS` on the status endpoint (ignored, not refused); `test_economy.py` now attacks it directly and holds the gold ledger to its invariant. |
| E-9 | Current hp/mana/stamina are client-written | high | **Closed** — clamped; a rate bound, not a proof | A patched client heals to full whenever it likes and can never die. | **Extra check (clamp).** Every rise is reconciled against what regeneration plus authorised potions could produce; anything past a 3x margin is trimmed and the save still goes through. Measured log-only before it was enforced. |
| E-10 | **Lusions were client-written, so dying was free** | **high (balance)** | **Closed** | A player can set their own revive currency, so death costs nothing. | **Server-side authority.** Lusion writes are ignored; `POST /api/character/revive` charges the server's own balance, only for a character the server knows is dead, and records the grant. |
| E-11 | A banned player could sign straight back up | medium (moderation) | **Closed** — as far as addresses and computers honestly allow | A ban lasts about thirty seconds — the time it takes to register a new account. | **Extra check + visibility.** Registration is refused from an address or a computer (the game's install id) holding a live ban; staff see linked accounts rated strong or weak, and nothing is banned automatically. A VPN no longer defeats it; deleting the game's install file still does, and this file says so. |
| E-12 | The sanction ladder had only one rung | low (moderation) | **Closed** — a kick is not a ban | Staff had to ban someone to remove them, so the smallest available response was the harshest. | **New tool.** `POST /api/staff/kick` ends every session without banning, under the same rank rules and audit log as a ban. |
| E-13 | **Four protections were unarmed against the shipped catalogue** | **high** | **Closed** — and it is the reason this table needed a footnote | Four fixes marked Closed were silently off in production, so everything they block was open with every test green. | **Deployment check.** `test_catalogue.py` asserts the shipped `gamedata.json` arms each protection, and the server logs an error at boot for any control running unarmed. |
| E-14 | A kick or a ban never reached a game that was already running | medium (moderation) | **Closed** | A kicked or banned player keeps playing for as long as they leave the game open. | **Heartbeat.** The game re-checks its session every 15 seconds and on any 401, and a dead session sends it to the login screen — a kick lands in about a second. Only a 401 signs anyone out, so a server restart is never a mass kick. |
| E-15 | Broken access control (IDOR) — can you name a row that is not yours? | critical if present | **Audited, nothing found** — one adjacent gap closed | A player sends somebody else's record id and edits, empties or deletes it. One Postman request, no modified client needed. | **Structural, two ways.** Every id a client may name is scoped inside the query, so "not yours" and "does not exist" are one 404; and the highest-value rows (trades) accept no id at all — every route resolves them from the caller. `test_ownership.py` runs the attack on every surface and enforces the convention on the source. The gap that was real: a deleted chat message left its picture being served. Since closed as well: the picture route now checks that the caller could see a line showing the picture, and private lines get their own copy. |
| E-16 | **The death screen's other exit was never migrated** | **high (balance + economy)** | **Closed** | Accepting death healed the character on the player's own machine and zeroed the carry gold locally — so the refill was unauthorised, and the penalty never reached the server at all. Death took your items, refunded your gold, and left you on 52 hp. | **Server-side authority.** `POST /api/character/respawn` refuses a living character, burns the carried gold through `gold_delta()` under the reason `death`, empties `carry_items`, refills the three pools from the class curve and writes the grant E-9's reconciler reads. Found by a player report, not by an audit — see below. |
| E-17 | **Two requests sent together could both spend the same thing** | **critical (economy)** | **Closed** | Two requests arriving at once both read the old balance: one loot cell paid five times, one purse was deposited into the bank twice, one potion explained five heals. Two clicks or a short script mints items and gold. | **Serialisation.** Every write request takes SQLite's write lock (`BEGIN IMMEDIATE`) before its first read and holds it to the end; nine slow routes are named exceptions. `test_concurrency.py` fires real requests together on threads. Found by playing: one sword swing killed two enemies and one kill's XP vanished. |
| E-18 | **Accept agreed to whatever was on the table when the trade ran** | **high (economy)** | **Closed** | The other player could swap the sword for a stick between your look and your click, and your accept went through. And the first to accept could lose what they received on their next ordinary save. | **Compare-and-set.** Accept names the revision of the offer it drew, and the write is conditioned on it. Both bags are flagged in the trade's own transaction, and a whole-bag save built before the trade is refused with the bag the server holds. `test_trades.py`. |
| E-19 | Two games on one account each saved their own bag | medium (integrity) | **Closed** | Two copies of the game on one account overwrote each other's bag and lost items, and a stolen token kept working beside the owner's. | **One session at a time.** A login that gets its token, or a game resuming a remembered one, ends every other session on the account; resuming keeps the original end date. `test_accounts.py`. |
| E-20 | **A staff password was the only thing between a guesser and the moderation desk** | **high (moderation)** | **Closed** — with a named limit | Staff names are public (the crown, the MOD and DEV badges), so their passwords are the ones worth guessing; one guessed password gives bans, notes and IP addresses. | **Second factor.** A staff login with a confirmed recovery address answers 202 and emails a six-digit code; only the code gets a token. A computer that proved itself is trusted for 30 days, at the rank it was trusted at. With no address or no mail it stands aside and says so. `test_staffcode.py`. |
| E-21 | **Session tokens were stored as issued** | **high (accounts)** | **Closed** | A copy of `elusion.db` - a backup, a leaked file - logged its holder in as every player signed in, for up to 30 days, with no password needed. | **Hashing at rest.** `sessions` keeps only each token's SHA-256 (`token_hash`) and every lookup hashes the token it is sent, as `trusted_devices` and `ended_sessions` already did and as passwords always were (scrypt). A boot migration hashed every stored token in place, so nobody was signed out. `test_accounts.py` A-5, `test_api.py` MIGRATION. |

Nineteen findings: eighteen closed, one open. Plus E-15, an audit that went
looking and came back empty — kept, because a search that finds nothing is
still a result and the reasoning is the point.

**The Fix column has a shape.** Six of the twenty fixes are *server-side
authority* — something the client could write, which the server now owns. The
rest are limits on rate, checks on what is plausible, and tooling for staff.
Read down that column before reading anything else in this file.

**"Closed" in this table means closed in the code.** E-13 is here because for 46
hours that was not the same thing as closed on the server. `gamedata.json` in
this repo sat behind the Godot export, and four controls that read **Closed**
above — E-1's equipment half, E-7's potion amounts, E-9's regen rates and E-3's
spawn ceiling — were failing open in production with every test green. A
register that does not distinguish *implemented* from *running* is telling you
something slightly false, so `test_catalogue.py` now asserts the second one.

**E-8 was the worst thing in this file and it was never in it.** Every other
finding here was found by attacking the API deliberately; that one turned up by
accident while wiring an unrelated endpoint, which is the part worth sitting
with. `gold` was one of `STATUS_FIELDS` and had never been added to
`SERVER_OWNED_STATS`, so a single `PUT /api/player/status` set any balance an
authenticated player liked — and the supply invariant this whole economy rests
on broke on that one call. The vendor sink, the kingdom tax and every trade
valuation sat on top of it. Closed, and `test_economy.py` now attacks it
directly rather than only walking in through the front door.

**E-3 is now the only one left, and it is the deepest.** E-9 was its other
face — both exist because the server never observes combat — and closing E-9
did not change that. What it did was bound the consequence: a heal the game
cannot explain is trimmed to what it can. E-3's consequence is still unbounded
in kind, only in rate.

**E-10 is E-8 again, one endpoint over**, and that is the pattern worth naming:
both were a currency field the client could write because nobody had marked it
owned. After two, the question stopped being "is this endpoint safe" and became
**"which fields can a client still write, and who decided that?"** — and the
answer for the rest is now a list rather than an assumption.

Covered by every suite in the folder, all green together, run with one command
(`run_tests.ps1`), which prints how many suites and checks there are at the top
of every run. **No count is written here on purpose.** This paragraph used to
give one (twelve suites, 1,635 checks) and both numbers had more than doubled by
the time anybody read it again; CLAUDE.md gives the same rule for the same
reason.

`test_catalogue.py` is the odd one and deliberately so. The others point
`ELUSION_GAMEDATA` at a fixture they build themselves, which is correct — a
test of the spawn ceiling should control how many of an enemy exist. It also
means none of them ever read the file that ships, which is exactly how E-13
survived 1,515 passing checks.

Every suite points `ELUSION_DB` at a throwaway file *before* importing `app.py`,
because `app.py` reads the path and calls `init_db()` at import time. A suite
that imported first and repointed after would run against the real database.

---

## What changed, per finding

### E-1 — Inventory and bank · CLOSED

`PUT /api/character/inventory` now reconciles against what the server actually
granted instead of storing what the client claims. Fabricated items are trimmed
to nothing; reorders, drops and server-granted items pass untouched.

This was only safe to switch on because every real source of an item — loot,
bank withdrawal, staff grant — already writes `carry_items` server-side first.
That was verified before flipping it, not assumed: there is no live client-side
item source. Staff are exempt, because a mod can already self-grant through
`POST /api/staff/grant` and clamping them would close no door.

The sequence mattered. `_report_unexplained_gains()` ran in **shadow mode**
first — comparing the claim to the record and *logging* the difference while
refusing nothing. Shipping the refusal before the log had proven the comparison
was right would have broken honest saves for real players.

**Later: the bag stopped being claimed at all.** The ledger above caught a
*gain*. The arrangement and every *loss* were still the client's word — an
empty array emptied the bag — and `SECURITY.md` listed "the backpack ledger is
still client-declared" as an honest limit until the owner chose to close it.
Every act on a grid turned out to be discrete: a drag is
`POST /api/character/inventory/move` (move, merge up to the stack limit, or
swap, exactly as the game draws it), the bin is `.../discard`, a pile of coins
is `.../cash` (which also fixed a pile being destroyed when used: the purse was
never the client's to raise, so its gold was dropped and the emptied cell was
not), and the bank has `/api/bank/move` and `/api/bank/discard`. Each names the
item the game saw in the cell; a cell holding anything else is a 409 that
carries the grid, so a drag made just after a trade landed cannot move or
destroy the wrong thing. A player's `PUT /api/character/inventory` and
`PUT /api/account/bank` are answered with the server's grid and `ignored`, and
the game no longer sends them; staff still write a bag whole, for tooling.
`_reconcile_inventory()` and `_reconcile_bank()` are gone with nothing left to
call them. `test_bagmoves.py` holds it, and `test_security.py` E-1 runs the old
exploits again (they now write nothing, a fabricated loss included).

### E-2 — Skills · CLOSED

Was two pieces of progress with a gap between them; the gap is now closed by
`/api/skill/train` (below).

**The ceiling.** `MAX_SKILL_LEVEL = 99` clamps over-cap claims from non-staff.
This kills the absurd-value cheat — a level-1 character claiming 2^31 attack —
but it is a guardrail, not authority. A client can still claim any level *up to*
the cap.

**Real authority, for three skills.** `fishing`, `cooking` and now `attack` are
genuinely server-owned. Fishing and cooking grant their XP against items the
server itself consumed. Attack was the easiest of the three and the longest
overlooked: `/api/combat/kill` has *always* rolled `attack_xp` from the enemy's
own profile — the client could never name its own reward — but it handed the
number back and trusted the client to bank it and PUT the total home. The
decision was server-side and the bookkeeping was not. It is now granted at the
moment of the kill, inside the same transaction as the character XP, and
`PUT /api/character/skills` **drops** all three from whatever the client sends. They are dropped silently rather than
refused, deliberately: an un-updated client still sends all six every save, and
400-ing an otherwise honest sync over a field it is not allowed to set would
break saving entirely.

**The last three are now closed too — `POST /api/skill/train`.** Defense,
agility and magic train on continuous play — damage taken, distance moved, magic
damage dealt — not on a discrete moment like a kill, so there was no server event
to hang a grant on. The character-level bound was rejected for good reason
(**agility trains on distance moved**, `agility_xp_per_1000_px = 3`, and
**defense on damage taken** — neither correlates with character level, so a
`character_level + N` bound clamps the honest level-1 who walked far).

The fix is the same posture as the kill event. The client cannot be made to
*prove* it took damage or moved, but it can be stopped from claiming more than
physically possible: it accumulates the raw XP it earned and reports it to
`/api/skill/train`, and the server **clamps each skill to a generous
`MAX_TRAIN_XP_PER_SEC × elapsed`** (a fresh or long-idle character gets at most
`MAX_TRAIN_ELAPSED_SECONDS` of budget), **applies class proficiency server-side**
(the 1.5× specialty, moved off the client), and grants it through the same
`_grant_skill_xp` that attack uses. All six skills are now in
`SERVER_OWNED_SKILLS`, so `PUT /api/character/skills` drops every one — the
client no longer names any skill level.

**What it does and does not close.** "Claim level 99 instantly" is gone — the
rate cap turns it into "report at the honest ceiling for the real number of
hours", and the value is server-decided and server-recorded. What remains is the
same residual as the kill event: a modified client that idles reporting the
ceiling amount every second still trains at the honest *maximum* rate without
playing. Closing that would need the server to observe position and damage
directly, which it does not — so, exactly like E-3, the rate is bounded and the
existence is not. The caps are deliberately generous (tune *down* with playtest
data, never up on a guess), because a tight cap is the very mistake this finding
warned about.

### E-3 — The kill event · OPEN

Unchanged, and deliberately last. `POST /api/combat/kill` still does the right
things *around* the claim — the server rolls the rewards itself, refuses
reward-less enemies (the slime-split exploit), and rate-limits with a token
bucket — but it never verifies the fight happened.

**The rate limit caps the speed of the fraud, not its existence.** Closing this
needs server-side encounter state, or at minimum tying kill reports to
server-known spawns — a change on both sides of the wire, and a larger piece of
work than everything above it combined.

**A second bound arrived since: the spawn ceiling.** The token bucket caps the
*rate* of claims at a number somebody chose. This caps the *count* at what the
world contains — an enemy placed once, on a 30-second respawner, cannot die
more than once per respawn however loudly a client insists. `exportgamedata.gd`
walks the scenes and ships `placed_count`; `/api/combat/kill` counts that
account's paid kills of that enemy out of `kill_reports` and refuses past
`placed x (window / respawn_floor + 1)`.

Measured against the same hour of flat-out farming: **18,000 kills/hour under
the bucket alone, 5,208 with the ceiling**, against roughly 300 for honest
play. Not a fix for E-3 — the fraud still exists, it is simply bounded by
content instead of by a guess.

**The idea it replaced is worth recording, because it sounded better.** The
server knows each player's gear and each enemy's hp, so bounding total hp
destroyed by `dps x elapsed` looks strictly tighter and needs no export at all.
It does not survive this game's classes: the tank's aura damages every enemy in
range and the mage's cast explodes, so a budget that never refuses honest AoE
needs roughly 8x headroom — and a cheater inherits all of it, landing **looser
than the bucket it would have replaced**. Bounding by what exists has no such
slack, because a tank killing six at once is six spawn points that cannot pay
again until they respawn. The general form: *bound by what exists, not by what
the player can do* — a capability bound must fit the best case, and the cheater
gets that whole allowance.

**It fails open twice, deliberately.** A `gamedata.json` from before the
placement export disables the ceiling entirely, so a half-upgraded server keeps
accepting kills rather than refusing every one. And `placed_count == 0` means
"spawned somewhere the export cannot see" — the poison slime's smalls come from
its own script and appear in no scene — where refusing would break a real
fight. The cost is a real exemption for runtime-spawned enemies, and it is
worth naming: today that is `poisonslimesmall` at 25 xp, the lowest-value enemy
in the game.

**The first of those two fail-opens then fired for real, and nobody noticed for
46 hours.** The shipped `gamedata.json` carried no `placed_count` at all, so the
ceiling described above was not running: kills were bounded by the token bucket
alone, at 18,000/hour rather than 5,208. The design was right and the file
disarmed it. See **E-13** — `test_catalogue.py` now asserts the ceiling is
actually armed, because the measurement below is worth nothing otherwise.

**THREE WAYS TO TIGHTEN IT WERE MEASURED AND ALL THREE WERE REJECTED.** Written
down because each sounded convincing, each would have cost a change across the
export tool, the server *and* the client, and the numbers say none of them is
worth it. Measured against the real roster — 42 placed instances, a 300s window
and a 30s respawn floor:

| idea | ceiling | vs today |
|---|---|---|
| today: `placed x (W/respawn + 1)` | 5,544 kills/hr | — |
| **per-spawn-point claims** — the client names *which* spawn point died, so each pays once per respawn | 5,040 | **9% tighter** |
| **per-scene attribution** — a player is in one place, so bound by the best single scene rather than the whole world | 4,200 | **16.7% tighter** |
| **per-enemy time-to-kill floor** — you cannot kill a 8,216 hp boss faster than its hp over your best single-target dps | 4,967 | **1.4% tighter** |

Honest play is roughly 300 kills/hour, so the gap is 18x today and would be 14x
with all three. **That is not a change in posture, and it is three moving parts
for it.**

Why each fails is worth more than the numbers:

- **Per-spawn-point** adds almost nothing because the existing ceiling is
  *already* `placed_count x respawn rate`. Naming the individual point only
  removes the `+1` slack per window. The bound was doing that work already.
- **Per-scene** fails on this world's shape: `field.tscn` holds 35 of the 42
  placed instances, so "one scene at a time" barely constrains anything. It
  would bind on a game with evenly distributed content. This is not one.
- **Time-to-kill** binds on exactly three enemies — `fireboss` (43.1s),
  `earthboss` (38.7s), `iceboss` (32.5s) — because everything else in the game
  dies in under the 30s respawn floor anyway. Note that this is NOT the dps
  *budget* idea rejected above and does not share its flaw: AoE kills more
  things at once, it does not make one thing die faster, so a single-target
  floor is not inflated by the tank's aura. It is simply that almost nothing in
  this game is tanky enough for the floor to matter.

**The general lesson, and it is the same one as the dps budget: the spawn
ceiling already extracts nearly all the signal the content contains.** The
remaining 18x exists because any content-derived bound has to permit the
theoretical maximum — every spawn point killed the instant it respawns, forever
— and nothing in the data distinguishes that from a real player who walks,
misses, and stops for tea. No refinement of *what exists* closes that, because
the gap is not about what exists. It is about what happened, and the server was
not there.

So E-3 stays open, and the next move on it is not a tighter bound. It is the
server observing combat, which is an architecture change and not a refinement.

**What has changed is that it is now measurable.** Until recently nothing
recorded a kill at all, so the fraud was not merely unpunished, it was
*invisible*: a client reporting one boss an hour forever looked exactly like a
player who enjoys the boss. `kill_reports` now stores every claim the server
paid out — enemy, rewards as paid, the level it was claimed at — and refused
kills leave no row, so the log and the payouts cannot drift apart. The same
shape as `login_attempts`, for the same reason.

That is deliberately groundwork and not a fix. **A threshold picked without data
is how honest players get clamped** — the interim skill-level bound proposed
under E-2 was exactly that mistake, and it did not survive contact with the
client. This makes the data exist first:

```sql
-- what is this account killing, and how fast
SELECT enemy_id, COUNT(*) AS n, MIN(at), MAX(at)
FROM kill_reports WHERE user_id = ? GROUP BY enemy_id ORDER BY n DESC;

-- who reports the most valuable enemy, and at what character level
SELECT user_id, level_at, COUNT(*) AS n
FROM kill_reports WHERE enemy_id = 'boss' GROUP BY user_id ORDER BY n DESC;
```

**Those queries are now a tool: `killwatch.py`.** It reads `kill_reports`
read-only (`mode=ro`, the same discipline as `canary.py` — a watch must never be
what changed the thing it watches) and sorts accounts into two piles that are
different in kind:

- **IMPOSSIBLE** — a claim the server's *own* rules should have refused: more
  kills of an enemy inside a window than the spawn ceiling allows, or a kill of a
  reward-less enemy the handler 400s before it writes a row. A row like this does
  not mean a player is fast; it means **a defence that should have been running
  was not** — the ceiling disarmed by a stale `gamedata.json` (E-13, the 46-hour
  gap), or a validation path bypassed. This is **false-positive-free by
  construction**: it replays the live gate's exact arithmetic, so it can only
  flag a kill the live server itself would have refused — never an honest one.
  It sets a non-zero exit and speaks even under `--quiet`. It is an alarm.
- **SUSPICIOUS** — legal under today's bounds but far outside honest play: a
  sustained rate many times the ~300/hr baseline, a boss farmed faster than
  anyone should, a character farming only the ceiling-exempt enemy, a boss killed
  far below the level its killers reach it at (median taken over *killers*, one
  vote each, so a high-volume cheat cannot drag the bar down to itself). This is a
  **review, not a verdict.** The tool ranks these accounts and prints the raw
  number each one tripped on; it bans nothing and enforces no threshold, because
  a threshold picked without data is how honest players get clamped — the same
  lesson that put the groundwork before the rule. Under `--quiet` these stay
  silent; run it as a digest to read them.

**This is detection, not the prevention E-3 ultimately needs, and that is the
honest posture *while this is single-player*.** The fraud today is a player
cheating their own save — so making it visible and bannable is proportionate,
and the architecture change (the server observing combat) is the move for the day
it stops being self-harm: when multiplayer makes one player's invented boss kills
everyone else's inflated economy. `killwatch.py` is what watches the gap until
then, and `test_killwatch.py` proves it fires on breaches and stays quiet on
honest bursts (a slime split, both boss instances down at once).

It is listed as open rather than quietly omitted because an open finding you
have named is a different thing from one you have not noticed.

### E-4 — Debug · CLOSED

`app.run(debug=_debug)`, where `_debug` is off unless `ELUSION_DEBUG=1`. The
Werkzeug interactive debugger turns any unhandled exception on a reachable build
into arbitrary code execution on the box holding `elusion.db` and its password
hashes, so the default had to be the safe one.

#### E-4c — The run block was one door of four · CLOSED

Asked for again, as "fix E-4 immediately", and the right response was to
check whether the first fix held rather than to repeat it. It held - for
`python app.py`, the one way of starting the server it guarded. The finding
said *"must be False anywhere anyone else can reach it"*, and the question
that sentence actually asks is how many ways there are to start it.

Probed on Flask 3.1.3 by fetching the debugger's own stylesheet
(`/?__debugger__=yes&cmd=resource&f=style.css`), which only exists while the
debugger is attached:

```
                                                     before     after
python app.py                                         off        off
python app.py, ELUSION_DEBUG=1                        on         on    - asked for, local
flask --app app run --debug                           ON         refuses to start
FLASK_DEBUG=1 flask --app app run                     ON         refuses to start
flask --app app run --debugger                        ON         refuses to start
ELUSION_DEBUG=1, ELUSION_TRUSTED_PROXIES=1            ON         refuses to start
debug + proxy set in .env rather than the shell       ON         refuses to start
```

`flask run` never executes the run block, so `ELUSION_DEBUG` was never
consulted, and `FLASK_DEBUG=1` is an ordinary thing to have left in a shell
from some other Flask project. Under `--debugger` the debugger's `/console`
page answered 200 - PIN-locked ("console is locked"), and a lock is what you
rely on only when there is no way to not have the door.

**One switch, two locks.**

- **At import** — `debugger_refusal(env, argv)`, a pure function: refuse to
  start if `FLASK_DEBUG` or `--debugger` asks for the debugger without
  `ELUSION_DEBUG`, or if any of them is set while `ELUSION_TRUSTED_PROXIES` is
  above 0. A proxy in front means other people reach this server, which is the
  exact condition the finding names. Judged after `.env` is read, so a flag in
  a copied `.env` is caught like one in the shell.
- **Per request** — if `werkzeug.debug.preserve_context` is in the WSGI
  environ, the request came through the *interactive* debugger, however it was
  attached. Without permission every request gets a 503 before any route runs,
  which leaves the debugger no exception to open a console on. It looks for the
  debugger itself rather than for the ways of asking, so a fifth door is
  covered too.

Both switches together (`FLASK_DEBUG=1` and `ELUSION_DEBUG=1`, no proxy) is
allowed: that is somebody who meant it, on their own machine.

Seventeen checks in `test_security.py` (E-4c): the decision table, the
per-request lock through the test client, and four child processes that try to
import `app.py` the ways above. Four deliberate breakages - the lock removed,
`--debugger` forgotten, the proxy rule removed, the refusal downgraded to a
warning - each caught.

### E-5 — Login throttling · CLOSED

Three parts, because the first one alone was only half the problem.

**Per-account lockout.** `LOGIN_MAX_ATTEMPTS = 8` consecutive misses freeze an
account for `LOGIN_LOCKOUT_SECONDS = 15 * 60`. The lockout is checked *before*
the password, so guessing correctly on the next attempt does not get you in —
a lockout you can guess your way out of is not a lockout. A correct password
resets the streak, so honest fat-fingering never accumulates.

Known, accepted trade: only a real row can be frozen, so a locked username is
distinguishable from a non-existent one. That is the standard cost of
per-account lockout and it is taken knowingly.

**Per-IP throttle**, which is what the account counter could not see. Eight
consecutive misses on one account stops *vertical* brute force. It does nothing
about *horizontal*: one host trying `password123` against a thousand different
usernames never reaches eight consecutive misses anywhere. That is the cheaper
attack and it was invisible. So failures are now counted by source over a
rolling window, with two ceilings:

- `IP_MAX_USERNAMES = 6` distinct usernames per 10 minutes — the spray test, and
  the tighter bound. Six different names failing from one address is not someone
  misremembering their own password.
- `IP_MAX_FAILURES = 25` per 10 minutes — raw volume.

Only failures count, so a shared connection logging in successfully all day
never approaches either. The gate runs **before** the user lookup and before any
password hashing: running scrypt for an attacker is doing 32 MB of work per
guess on their behalf, which would turn the good hash into a denial-of-service
lever.

**The proxy problem, which is the part that breaks deployments.** This is
recorded because getting it wrong fails in two opposite and equally bad ways:

- Trust `X-Forwarded-For` when *not* behind a proxy and the header is
  attacker-controlled. A spray sets a fresh fake address per request, every
  bucket holds one failure, the throttle never fires, and the log fills with
  invented addresses implicating people who did nothing.
- Ignore it when you *are* behind one, and `request.remote_addr` is the proxy —
  the same value for every player alive. One brute-force run then locks out the
  entire player base. **The defence becomes the outage.**

Neither is knowable from inside the app, so it is not guessed.
`ELUSION_TRUSTED_PROXIES` defaults to `0`, meaning "nothing in front of me,
believe the socket". Set it to the number of proxies actually running and
`ProxyFix` reads that many hops back. See `client_ip()`.

**Logging.** `login_attempts` records timestamp, username, IP, success/fail and
a reason for every attempt. Three decisions worth stating:

- **No password field.** Not hashed, not truncated, not "just the first two
  characters". A mistyped password is usually a real password with one character
  wrong, and there is no version of storing it that survives the day it leaks.
- **The reason is logged even though the response cannot show it.** The 401 is
  byte-identical for `no-such-user` and `bad-password` or the endpoint
  enumerates usernames — but the log is on our side of that line, and
  `no-such-user` against forty names is the signature of a spray.
- **Successes are recorded too**, not only failures. A window containing nothing
  but failures cannot distinguish "someone is being attacked" from "the server
  is broken for everybody".

Both questions this log exists to answer are one query each:

```sql
-- one address, many usernames: a spray
SELECT ip, COUNT(DISTINCT username) AS names
FROM login_attempts WHERE ok = 0 AND at >= :since
GROUP BY ip ORDER BY names DESC;

-- one account, many failures: a grind
SELECT username, COUNT(*) AS misses
FROM login_attempts WHERE ok = 0 AND at >= :since
GROUP BY username ORDER BY misses DESC;
```

Rows are pruned past `LOGIN_LOG_RETENTION_SECONDS` (14 days) on the login path,
so the only thing that writes to the table is also the thing that cleans it.

#### E-5f — The throttle was feeding itself · CLOSED

Found by load-testing rather than by attacking: the question was "what happens
when a thousand people log in on day one", and the answer was **nobody logs in
at all**.

`login_attempts` records everything that happens at this door, and most of it
is not evidence. In particular the gate's **own refusals** are written there,
each carrying the username it had just refused. `_ip_throttle_state()` counted
any row that was not `register-%`, so those refusals counted toward the
condition that produced them:

```
six typos behind one address trip the spray rule
  -> every later attempt is refused and LOGGED as a new failed username
  -> the window never falls below six distinct names
  -> the lockout renews for as long as anyone keeps trying
```

**Measured, not reasoned about.** Sixty players with the CORRECT password,
retrying continuously behind one address, were refused for four full windows
and only got in once every client went silent *at the same moment*. Game
clients retry on their own, so that moment does not arrive.

**Why this was a launch-day outage and not an edge case.** Shared addresses are
the normal case — a household, a student hall, a mobile carrier's NAT — and
*every player at once* if this sits behind a proxy with
`ELUSION_TRUSTED_PROXIES` left at 0, which `wsgi.py` only warns about. Six
typos anywhere in the player base and the game is shut.

The fix is a shape change, not a number change: the reason filter is now an
**allowlist** (`THROTTLE_EVIDENCE_REASONS`) of the three reasons that mean a
credential was actually checked and actually failed — `no-such-user`,
`bad-password`, `bad-password-on-change`. Deliberately excluded:

| reason | why it is not evidence |
|---|---|
| `ip-spray`, `ip-volume` | this gate's own refusals — the bug above |
| `register-*` | a clumsy signup must not cost anyone their login |
| `account-locked` | no credential was checked; counting it lets one account's lockout escalate to the whole address |
| `banned` | the password was **correct**; a banned player retrying would lock out their household |

`/register`'s throttle was already an allowlist (`reason = 'register-conflict'`)
and was never affected — which is the argument for the shape. A denylist's
failure mode is silent: a refusal reason added later becomes evidence by
default and nothing says so.

`test_throttle.py` covers it, and those checks are **mutation-tested** — reverted
to the old rule they fail, which is the only way to know a regression test for
"it recovers" is testing anything.

### E-6 — Long token, no credential rotation · CLOSED

Not in the original audit; found later, recorded here rather than fixed quietly.

`TOKEN_TTL` is 30 days. That is a deliberate game-design call — nobody wants to
retype a password to play for twenty minutes — but it was only defensible once
there was a way to end a session early. `POST /api/auth/logout` kills the token
doing the asking, which is no help at all if the token that leaked is a
different one.

**Revocation:** `POST /api/auth/logout-all` deletes every session for the
calling account and reports the count. It kills the caller's own token too —
"log out everywhere" that leaves this device signed in is not what anyone means
by it, least of all someone who pressed it because they think they were
compromised.

**Rotation:** `POST /api/auth/password` re-hashes with scrypt, deletes every
session and issues one fresh token to the caller, **in a single transaction**.
Splitting those steps would leave a window in which the password is new and the
attacker's session is still live — the exact state the endpoint exists to
remove. Revocation without rotation was a door being closed on someone holding
the key: the attacker who knew the password just logged back in and got another
thirty days.

Three decisions in it worth stating:

- **The current password is required**, and a valid bearer token is explicitly
  not enough. A stolen token is the situation this endpoint is *for*; letting
  one set a new password would hand the account to the thief rather than take
  it back.
- **It is throttled like login**, per-account and per-IP, because it is a second
  place to guess a password — and one that answers from inside an authenticated
  session. Leaving it open would put a lock on the front door and a window
  beside it.
- **The caller is signed out and re-issued.** Every session dies including the
  one that made the request, and only then does a new token go back to the
  caller, so "change my password" can never be a way to sign out everyone
  *except* whoever holds the stolen token.

### E-7 — Item use is client-authoritative · CLOSED

Added when the requirement fields were, because writing a gate and not enforcing
it is worse than having no gate: it reads as a control in the source and is not
one.

`ItemData` carries `required_level` (character level, for gear you buy) and
`required_skill` / `required_skill_level` (a named skill, for things you cook or
catch). `inventoryscreen.gd` checked both before the use dispatch — **on the
player's machine**, with no consume endpoint behind it. A potion was drunk
entirely client-side and reached the server as an inventory sync one item
shorter, which E-1's reconciliation accepts, because losing an item is exactly
what an honest use looks like.

`POST /api/character/consume` is the fix. The server reads the character's level
and skills from its own rows, checks both requirements, destroys one from the
stack itself, and the client applies the effect only on a 200. The client check
stays where it was and is now a courtesy rather than the rule — it is the fast
answer, so a player four levels short is told instantly instead of a round trip
later.

**Two things came out of building it, both worth more than the fix.**

The first was a bug in the fix. Skill rows are written when a skill is first
trained, so a brand new character has none — and the first draft read "cooking
is missing from your levels" as "cooking is not a skill" and let it through. A
gate that opens for every new account is worse than no gate. `VALID_SKILLS` is
now passed in so the two questions stay separate: not a real skill passes
loudly, untrained is level 1 and the gate holds. Found by a smoke test, not by
review.

The second was E-8, below.

**What it does not close.** The *effect* is still the client's: the server
destroys the item and says what it should restore, but nothing verifies that the
client applied that and only that. A cheat that heals without drinking anything
never touches this endpoint at all. That is E-9.

~~The server does not know what a potion restores~~ → it does now.
`restore_target` and `restore_amount` are exported, and `/api/character/consume`
writes both into `consume_grants`, which is what let E-9's check become per-pool
instead of per-character. Kept struck through rather than deleted because the
note was true when written and the sequence is the point: the gate came first,
the amount second, and the check that needs the amount is still last.

### E-8 — A client could set its own gold · CLOSED

**Found by accident, which is the part worth recording.** It surfaced while
wiring the client half of E-7, not during any of the deliberate attacks that
produced every other finding in this file.

`gold` is one of `STATUS_FIELDS`. It had never been added to
`SERVER_OWNED_STATS`. So:

```
PUT /api/player/status   {"slot": 0, "gold": 1000000}   ->   200
```

and the invariant the entire `gold_ledger` exists to hold:

```
SUM(gold_ledger.delta) = 0        SUM(saves.gold) + SUM(bank_gold) = 1,000,000
```

Nothing was minted through `gold_delta()`, so the recorded side never moved
while a purse went to a million. The vendor sink, the kingdom tax, every trade
valuation and the whole of `test_economy.py` sit on that equation.

`PUT /api/save` had always refused a client's gold. The two write paths had
simply drifted, and the one that drifted was the one nobody thought of as an
economy endpoint.

**Closed by adding `gold` to `SERVER_OWNED_STATS`** — ignored rather than
refused, like every other owned field, and named in `ignored` so an honest
client can see it is being corrected. Safe to close outright with no shadow
pass, because nothing honest pushes a gold figure: `/api/loot/take` resolves a
currency drop straight into a balance through `gold_delta()` and calls itself
the only place gold is created, and shop, bank and trade all write their own
rows. The one client path that added gold locally was the gold-pile handler in
`inventoryscreen.gd`, and a gold pile cannot reach a backpack at all — `CURRENCY`
is in `EXCLUDED_FROM_LOOT`.

**Why the suite did not catch it, which is the lesson.** `test_economy.py` had
268 checks and a mutation-tested invariant, and every one of them moved gold
through a server path and then asserted the books balanced. Not one tried the
front door of the balance itself. **An invariant only tells you about the paths
somebody walked.** There is now a section that attacks it directly, and the old
`test_api.py` case that asserted gold *was* stored — which passed, and was the
bug written down as an expectation — is now the assertion that it is ignored.

### E-9 — Current hp, mana and stamina are client-written · CLOSED

Named here because E-8 came out of looking at the same endpoint, and this is
what is left of it.

`PUT /api/player/status` accepts any `hp` up to the maximum the server derives
from class and level. That ceiling is real and it is not the interesting part:
underneath it, a patched client heals to full whenever it likes and never dies.
Potions become decoration, and E-7's gate is moot for anyone willing to skip the
potion entirely.

**It is clamped now**, and the rest of this section is how that became safe to
do. What follows was written while it was open; it is kept because the sequence
is the argument.

**It could not be refused then, and the reason was honest rather than lazy.**
`player.gd` regenerates all three stats continuously — a percentage of each
stat's own maximum per second, with a floor — so a character returns to full in
about a minute of standing still. A check that did not model that would flag
every player in the game.

**So it is measured instead.** `_report_unexplained_heals()` compares each rise
against what regeneration could have produced in the elapsed time, and asks
whether an authorised consume explains the rest. It logs and refuses nothing —
the same staging E-1 used, for the reason written into that function: shipping
the refusal before the comparison had proven itself would break honest saves for
real players.

Three things had to happen before it could bite. **All three are done** — what
is left is not a piece of work, it is a decision:

1. ~~Export `regen_percent_per_second` and `regen_minimum_per_second`~~ → done.
   They are `PlayerStats` values read through `gamedata.regen_rate_for()`, so
   the allowance is measured against the game's own numbers rather than against
   copies in `app.py`. Copied constants drift; this project has that scar
   already, in the xp-formula note in `exportgamedata.gd`.
2. ~~Export `restore_amount`~~ → done. A consume grant carries its target pool
   and its size, so the check is per-pool: a cheap stamina potion no longer
   explains an arbitrary jump in health, which is the hole it used to leave.
3. ~~Give revive an endpoint~~ → done, and it turned into **E-10** below. An
   honest revive now leaves a grant row and no longer appears in the log at all.

**So what remains is evidence, not plumbing.** The check has never refused
anything, and flipping it to refuse is only safe once the log has shown it stays
quiet across honest play — all four classes, idle regen, a potion mid-fight, a
level-up refill, a revive, and a client that saves rarely. That is the same
staging E-1 used, and the reason is written into `_report_unexplained_gains()`:
shipping a refusal before the comparison has proven itself breaks honest saves
for real players. A threshold picked without data is how honest players get
clamped.

**And E-13 is why that evidence is worth nothing until the catalogue is
current.** Until recently the shipped `gamedata.json` carried no regen constants
and no restore amounts, so the check was running on `app.py`'s fallback literals
and treating every grant as an unknown quantity — which makes it return early
and explain anything. A quiet log under those conditions was not evidence of
anything at all.

---

**How it closed.** `_report_unexplained_heals()` is `_reconcile_heals()` now —
renamed because it no longer only reports, and because it did the same job as
`_reconcile_bank()` and `_reconcile_inventory()`: compare the claim to the
record and correct it. (Those two have since gone: the bag and the bank are no
longer claimed by the client at all - E-1.)

**There are two lines, and that is the whole design.**

| | | |
|---|---|---|
| `HEAL_ALLOWANCE_MARGIN` | 1.25x | the honest estimate — **logged, never enforced** |
| `HEAL_CLAMP_MARGIN` | 3.00x | **enforced** |

Enforcing at 1.25x on the evidence available would have been the E-2 mistake
in a new place: a threshold taken from eighteen simulated scenarios rather than
from real traffic. Enforcing at 3x cannot plausibly catch an honest player —
it is three times more healing than the game can produce.

The point is what happens *between* them. Every rise landing in that band is
logged as a rise a tight clamp would have caught, and stored untouched. That
is the evidence for closing the gap, gathered **while a control is already
running** rather than instead of one. A band that stays empty across real play
is the argument for lowering the enforced line to 1.25; a band that fills with
honest players is proof that shipping the tight clamp would have broken them.
The ratio is taken against the tight allowance, so widening what is enforced
never widens what is logged — the two numbers stay independent on purpose,
because conflating them is how a temporary margin quietly becomes the
definition of honest.

**It clamps; it does not refuse.** A 400 fails the whole save, and that save
carries XP, gold, position and inventory. Discarding a legitimate half-hour of
play to punish a suspicious hp figure is a worse bug than the cheat, so the
figure is trimmed to what the player could have earned and the write proceeds —
exactly as `_reconcile_inventory()` trims a bag.

**What closing it broke, which was the most useful part.** Four checks in the
existing suites failed, and one of them was this, in `test_security.py`:

```python
check("and the claimed hp was still stored", ... == MAX_HP)
```

That assertion **was the vulnerability, written down as an expectation**, and
it passed for precisely that reason. It is the same shape as the `test_api.py`
case that asserted gold *was* stored while E-8 was open. Twice now, which makes
it worth naming as a class rather than as an anecdote: **a suite only tells you
about behaviour somebody chose to assert, and asserting current behaviour makes
a hole look load-bearing.** The other three were measuring the derived-`max_hp`
clamp and had only ever known about one ceiling on `hp`; they now assert that
both compose and that `hp` lands at the tighter of the two.

**What it still does not do,** and the table says so rather than claiming a
proof: it bounds the *rate* of unexplained healing, not its existence. Measured
against a 180 hp pool, roughly 7 hp per save passes under the tight line, and
the enforced line is three times that until the band gives a reason to close
it. At the client's own ten-second save ceiling that is real healing. It is the
same shape as the kill bucket and it has the same root — **the server does not
observe combat** — which is E-3, and the only finding left.

`test_healing.py` is the evidence: 27 checks driving the real endpoint, not the
function, because the merge and clamp logic above it can change what `after`
even looks like. Ten honest scenarios assert silence — regen after a fight, a
full minute back to maximum, ten minutes offline returning at full, a save
every ten seconds mid-fight, two saves in the same second, a potion, a potion
plus regen, a level-up refill, a revive, mana at the floor rate — and five
cheats assert the opposite, because a check that never fires is quiet for the
wrong reason. The clamp is mutation-tested: disabled, three of its checks fail.

A bug in that suite is worth recording. Its first draft filtered consumables on
`item["restore_target"]`, which is the enum **integer**, so every potion
silently failed to match and the potion scenarios tested nothing. That is the
exact mistake `restore_for()`'s own docstring warns against. It goes through
the accessor now, so the test cannot drift from the server.

### E-10 — Lusions were client-written, so dying was free · CLOSED

Found while building the revive endpoint E-9 asked for, which is the second
time in two days that fixing one thing has walked into a bigger one.

`PUT /api/account/lusions` took an absolute figure and stored it:

```python
lusions = parse_stat(payload.get("lusions"))
db.execute("UPDATE accounts SET lusions = ? WHERE user_id = ?", (lusions, user_id))
```

No provenance, no delta, no ledger — lusions have never had one. **The same
mistake as E-8, one endpoint over.**

**Why a second currency mattered more than its size suggests.** Lusions have
exactly one sink in the game: reviving after death, at `revive_cost` a go. A
currency with one sink *is* that sink, so the balance was the death penalty —
and `gameover.gd` ran the whole transaction locally anyway: it read the balance,
called `add_account_lusions(-cost)`, wrote full hp, mana and stamina into the
save slot itself, and reloaded the world. The server saw a smaller number and a
healthier character arrive by PUT and believed both, because it had no
authoritative balance of its own to disagree with.

So death cost nothing. Not "cost little" — nothing, for any client willing to
skip one line.

**Closed in two halves.** `PUT /api/account/lusions` now ignores what it is sent
and reads back the server's own figure, the same ignored-not-refused treatment
gold got. And `POST /api/character/revive` does the transaction: it refuses
anyone who is not dead *by the server's own stored hp*, charges the cost from
the server's own balance, restores the three resources from the class curve, and
records a grant row.

**Three details worth keeping.**

- **Only the dead may be revived.** Without that check this becomes "pay 20
  lusions for a full heal, any time" — a different feature, with different
  balance consequences, that nobody designed. The client now writes `hp = 0`
  before asking, because the death arrives by a debounced save that may not have
  landed yet, and the record should say so regardless.
- **402, not 403.** Not having the lusions is a price, not a permission. Both
  numbers go back so the screen can show the shortfall.
- **It closes the last big false positive in E-9's log.** An honest revive is a
  jump from zero to full in no time at all — exactly the shape the reconciler
  flags. It now leaves a row in `consume_grants` like a potion does, so the log
  stays about cheating rather than about dying.

### The economy · a surface that did not exist at audit time

Player-to-player trading is new since the original audit and is worth stating
plainly, because it changes the threat model: **two clients can now cooperate.**
Until trading existed, every exploit had one beneficiary and the server only had
to distrust one party per request.

What holds:

- **Gold is double-entry.** Every creation and destruction writes a `gold_ledger`
  row, against the invariant `SUM(delta) == SUM(saves.gold) + SUM(bank_gold)`.
- **A transfer writes no row, deliberately** — both purses are already inside the
  right-hand sum, so a transfer that *did* write would net to zero and pass
  anyway. This makes the invariant alone a **weak** test, which is the trap worth
  naming: `test_economy.py` therefore asserts row counts and the minted total
  beside it, and mutates the ledger on purpose to confirm the check fails when it
  should.
- **The swap is server-side and atomic.** Both parties confirm, then
  `_execute_trade()` moves items and gold inside one transaction.
- **A failed execution kills the trade for both sides.** `db.rollback()` undoes
  the confirmation made in *this* request and not the one the other player
  committed minutes ago, which left trades wedged half-confirmed until
  `_trade_refuse()` existed. Found by a test written on speculation, not by a
  report.
- **Self-trade is refused explicitly** rather than left to chance — it would run
  every step against one account and the arithmetic would look fine.
- **One open trade per player**, enforced with a 409, and offers expire after
  `TRADE_EXPIRY_SECONDS` (600). That bounds the row growth an abusive client can
  cause without a rate limit of its own.
- **The tax is charged server-side**, on what you *receive*, valued from the
  server's own item table rather than anything the client sends. It rounds up
  with a floor of 1, so splitting a trade to dodge it costs strictly more.

What is accepted rather than solved:

- **The trade endpoints have no throttle of their own.** The one-open-trade rule
  and the expiry cap the state a client can accumulate, so this is request churn
  rather than resource exhaustion, but an offer/cancel loop is unbounded in rate.
  Worth a token bucket if abuse ever appears; not worth pre-emptive complexity.
- **`GET /api/players/nearby` discloses who is online and which area they are
  in**, to any authenticated player. That is what the endpoint is *for* — you
  cannot offer a trade to someone you cannot name — and it is the honest maximum
  today: the server stores `saves.area` and nothing finer, so no coordinate
  exists to leak. If real presence arrives, the position it gains is the thing to
  re-examine, not this.

### E-11 — A banned player could sign straight back up · CLOSED (as far as it can be)

Not found by attacking the API. Found by asking what happens on day one with
real players, which is a different question from "can I break this" and turns
up different answers.

A ban did exactly what it said: `is_banned` set, sessions deleted in the same
transaction, `user_for_token()` re-checking it on every authenticated route so a
surviving token grants nothing. All correct, and all about **that account**.
Thirty seconds later the same person registered a new one and the server had no
opinion, because nothing connected the two.

**What "fixed" can honestly mean here.** A dynamic address and any VPN defeat
address matching outright, so a claim to stop ban evasion would be false. What
is achievable is narrower and still worth having: make the lazy case fail, and
make the determined case *visible*. Those are two separate mechanisms and they
are deliberately not the same mechanism.

**1 · `account_ips`, and why it is not `login_attempts`.** A new table of
`(user_id, ip, first_seen, last_seen)`, UPSERTed on successful login and on
registration. Written **only on success** — a failed login says nothing about
who was at the keyboard, and recording those would fill the table with the
addresses of whoever is currently guessing at an account. `first_seen` is the
half that separates "has genuinely played from here" from "appeared today".

**2 · Registration is refused from an address holding a live ban.** 403, not
429: this is "not from here", and a retry timer would invite the retrying it
exists to stop. The banned account's name is not returned — whoever is reading
either already knows it, or is a stranger who should not be told who was banned
on a shared address.

**It expires with the ban, by construction.** The check runs through
`ban_state()`, which reads expiry against now, so a served sentence stops
blocking the moment it is up and no scheduled job has to run for that to happen.
An address cannot be poisoned in two years by something somebody else did on it.

**Logins are deliberately untouched.** Blocking those too would mean banning one
teenager takes out their household, their school, or everyone behind a mobile
carrier — the same failure `_ip_throttle_state()`'s own comment refuses, and
worse than the evasion it would prevent. The sibling keeps playing. The cost is
that the sibling's *friend* cannot sign up from that house while the ban runs,
which is accepted and stated rather than hidden.

**And the block stops applying on a crowded address, because otherwise it is a
weapon.** The first version had no such limit, and the attack against it is
free: get yourself banned on purpose from a campus, a library or a carrier NAT,
and every stranger behind that address is locked out of registering — for good,
if the ban is permanent. Your own account was already gone, so you spent
nothing. Run against this code, forty unrelated accounts on one address and one
deliberate ban made the forty-first person unable to sign up.

Above `EVASION_BLOCK_MAX_ACCOUNTS = 12` distinct accounts, the address is a
building rather than a household and the block does not apply. That is a real
cost — an evader who finds a crowded address registers freely — accepted
because a VPN already sells them that outcome, and because the link view still
surfaces every one of those accounts. The bar for *refusing a stranger an
account* is deliberately higher than the bar for *marking a link weak in a view
a human reads*, which is why this is a separate constant from
`SHARED_ADDRESS_ACCOUNTS`.

**3 · Linked accounts in the staff view, which never ban anything.** The
determined evader gets through step 2, so `GET /api/staff/user/<name>` now
carries the other accounts seen at this one's addresses. That is the half that
survives a VPN: it cannot stop the next account, it makes it arrive visible
instead of arriving unknown.

**The part that took the most thought was not accusing people.** A link through
an address shared by three accounts and one through a carrier pool of two
hundred are different facts, and reporting them identically is how a mod at 3am
bans somebody's flatmate. Each link carries `quietest_address_accounts` — the
smallest crowd on any address the two share — and is marked `strong` or `weak`
against `SHARED_ADDRESS_ACCOUNTS = 6`. The raw counts are returned beside the
verdict so a mod can disagree without reading the source. Banned accounts sort
first, because burying the one relevant name under nine strangers is the same as
not showing it.

Gated behind `can_act_on()`, not the `mod` rank that opens the route: this is a
list of *other people*, most of whom are not the subject of the lookup. A mod
sees a player's links and not another mod's, and nobody sees the owner's.

**A bug worth recording, because the failure mode is the interesting part.**
`_ban_evasion_state()` first shipped selecting only the three columns its own
logic branched on, while `ban_state()` reads five. `sqlite3.Row` raises
`IndexError` for a column the SELECT did not fetch — so **every blocked
registration returned 500**. The decision was right, the refusal happened, and
the response was a crash: the feature looked broken while working perfectly, and
a test asserting only "was it refused?" would have passed. `test_security.py`
asserts the status code, and `ban_state()`'s docstring now names the five
columns it requires.

Fifteen checks cover the link view and thirteen the block, and **most of them
are about who is *not* hit**: the sibling still logs in, the stranger elsewhere
registers, the campus is not lockable, the served sentence stops blocking by
itself, the linked account is not banned by having been linked. A narrowness
guarantee cannot be demonstrated by the blocking behaviour — only by the people
who still get through.

**4 · Later: the computer, because a VPN changes the address and not the
machine.** "A VPN defeats ban evasion" was the honest limit on all of the
above, and the owner chose to close as much of it as can honestly be closed.
The game now makes a random install id the first time it runs, keeps it in
`user://install.cfg` (apart from the session, so logging out does not reset
it), and sends it with every login, registration and resume.
`account_installs` is `account_ips` for computers, holding only the SHA-256 of
the id, and `_install_evasion_state()` is `_ban_evasion_state()` for them:
registration only, expiring with the ban, inert above
`EVASION_BLOCK_MAX_ACCOUNTS` accounts (a library computer), and refused with
"from this computer" so a sibling knows why. The link view merges both sources
into one entry per account, with `shared_computers` beside `shared_addresses`;
a computer link is strong unless the computer itself is crowded.

Two decisions worth keeping. **A banned login with the right password still
records the computer** — a player banned before install ids existed has none on
file until they try once more, and they always do — while a wrong password
records nothing, or typing a banned name would get any computer blocked. And
**`/api/auth/resume` records too**, the address as well as the computer:
"Remember me" carries a player thirty days without `/login`, and recording
only there left that month out of the link view.

What it is not: the id is whatever the client sends. Deleting the file, a
second browser for the web build, or a modified game sending a fresh id gets
past it, and a game that sends none (an older build) is not refused. It turns
"buy a VPN" from the whole answer into half of one. `test_security.py` E-5f
holds it, narrowness first; the game's `_test_install_id_is_kept_and_sent()`
holds the client half.

### E-12 — The sanction ladder had only one rung · CLOSED

Not a vulnerability. A moderation tool that made overreacting the cheapest
option, which produces the same outcome by a different route.

`POST /api/staff/ban` deletes the target's sessions in the same transaction —
correct, and the reason a banned player holding a 30-day token does not keep
playing. But it meant **deleting sessions was only reachable by banning**. A
mod who wanted to interrupt something — a shared account to re-secure, a
session left open on a machine somebody no longer controls, a stuck client, or
simply buying a minute to read a report before deciding — had to choose between
banning and doing nothing.

`POST /api/staff/kick` is that middle rung. It ends every session for an
account and does nothing else: no ban, no rank change, and the player logs
straight back in. It revokes **access**, not permission.

**That reversibility is the whole point and also the honest limit.** Against
someone actively cheating a kick is a speed bump, because they still hold the
password. It must never be presented to players as punishment — if it is, the
reconnect reads as the ban having failed.

Same reach as every other sanction (`can_act_on()` through
`_moderation_target`), so a mod cannot kick a mod and nobody can kick the
owner, and the refusal is the same 404 every out-of-reach target gets rather
than a 403 that would map who outranks whom. Logged to `staff_actions` with the
session count and reason, because a session that ends for no visible reason is
a support ticket.

**Live sessions are now in the staff view**, which is what makes the choice
between a kick and a ban informed. `login_attempts` answered "who has been
trying"; nothing answered "who is holding a key", so a mod could not see
whether there was anything to kick, or confirm afterwards that a banned player
was actually out.

**No tokens in that payload — absent, not masked.** A staff view is the worst
possible place for a readable token, because the people reading it are the ones
with reach. What it carries is the shape: how many sessions exist and when each
expires. There is no `issued_at` because the table holds none, and deriving it
as `expires_at - TOKEN_TTL` would be right only for rows issued under the
current TTL and silently wrong for every older one — against a fixed TTL the
expiry already says how old a session is.

Unlike the addresses and the linked accounts, the session count is **not**
gated behind `can_act_on()`. Those two are facts about other people; a session
count is a fact about this account alone, and gating it would gate the sanction
rather than the personal data.

Twelve checks in `test_security.py`, and most of them assert what a kick does
**not** do: it does not ban, it does not stop the player logging back in, it
does not reach further than a ban would, and kicking an account with nothing
live reports zero rather than erroring.

**Since then the ladder has a rung below the kick, and players can point at
what needs it.** A **mute** stops a player speaking in world chat, whispers
and their guild, and nothing else: they keep playing. A mod can mute for at
most a day (`MUTE_MAX_MINUTES_MOD`), a dev for longer, under the same reach as
every sanction. A **report** names one chat line, and only a line the reporter
was shown, so the route cannot be used to find out which ids are other people's
whispers. Staff see one card per reported player; a mute, kick or ban closes
that player's reports, and every one of those actions is in the moderation log.
`test_chatsafety.py`, and SECURITY.md invariants 23 and 26.

### E-13 — Four protections were unarmed against the shipped catalogue · CLOSED

Not an attack. Found by reading the file the server actually loads, while
starting work on E-9 — which is the second time in this file that opening one
finding has walked into a bigger one.

`gamedata.json` lives in two places: the Godot project exports it, and a copy is
carried across to this folder by hand. The copy was **46 hours behind**. Same
code, two catalogues:

```
                             shipped copy      fresh export
EQUIP_EXPORTED                   False            True
SPAWNS_EXPORTED                  False            True
REGEN_EXPORTED                   False            True
restore_for('tinyhealthpotion') ('', 0)        ('HP', 20)
spawn_count_for('windslime')        0              1
```

Each of those is a gate, so each was open:

- **E-1's equipment half.** Without `equip_slot_name` the server cannot tell a
  helmet from a sword, and `parse_equipment()` falls back to checking only that
  the slot name is in `EQUIP_SLOTS_FALLBACK` — the state that let
  `{"helm": "embersword"}` through in the first place.
- **E-3's spawn ceiling.** Gated entirely on `SPAWNS_EXPORTED`. Kill claims were
  bounded by the token bucket alone: 18,000/hour rather than 5,208.
- **E-9's measurement.** The allowance was computed from `app.py`'s fallback
  literals instead of from `PlayerStats`.
- **E-7's amounts.** `restore_for()` returning `('', 0)` writes a grant with an
  empty target and a zero amount, and `_report_unexplained_heals()` treats an
  unknown amount as *unknown* and returns early rather than calling it a cheat.
  So one potion of any kind explained a heal of any size — the exact hole the
  per-pool rewrite was built to close, reopened by a data file.

**Failing open was correct and is unchanged.** A server whose catalogue predates
a field must keep serving rather than refuse every request; a half-upgraded
deployment that 400s every kill is a worse outage than a temporarily loose
check. The bug was never the fail-open. It was that it was **silent**.

**Why 1,515 passing checks said nothing.** Every suite sets `ELUSION_GAMEDATA`
to a fixture it builds itself, which is right — a test of the spawn ceiling
should decide how many of an enemy exist rather than inherit whatever the last
export produced. The consequence is that all nine tested the code's behaviour
*given* a catalogue and none tested the catalogue. The controls were correct,
tested, and pointed at a file that did not arm them.

That is this project's own recurring failure with a new coat on: **something
that looks finished and does nothing.** It has now appeared in a scene that
drew no pixels, a handler wired to a `print`, a stat that bought attack speed
for the wrong character, and here, in four security controls at once.

**Closed in two halves.**

`test_catalogue.py` reads the real `gamedata.json` — it pops `ELUSION_GAMEDATA`
explicitly, so a developer running the whole directory in one shell cannot
accidentally test whichever fixture ran last. It asserts two things per
protection, because a flag can be true while the data under it is useless:
`EQUIP_EXPORTED` is `any("equip_slot_name" in item ...)`, so one item carrying
the field turns it green while the other 122 fall through the check. Run against
the stale catalogue it fails 6; against the current one it passes 16.

It deliberately asserts **no date, no hash, no byte count**. Those fail on every
legitimate re-export, and a test that cries wolf is a test somebody deletes. An
optional section does compare against the Godot copy directly when
`ELUSION_GODOT_DATA` points at it, and says out loud that it is skipping when it
does not.

`_warn_if_protections_unarmed()` is the production half, since CI cannot see the
file on the deployed box. It replaces two hand-written warnings — and the fact
that the two protections added later never got one is the entire incident in a
sentence, so it is a table now. It logs at **error**: these lines mean a control
the code believes is running is not running, and a warning is precisely what got
scrolled past for 46 hours.

**Refusing to boot is left as a commented one-line flip**, not taken. The case
for it is that a game holding real accounts should not quietly serve with
equipment validation off. The case against is that it converts a missed copy
into an outage at the worst possible moment, and nobody is reading the log at
3am either way. It is a deployment posture question, and recording the argument
is worth more than silently picking a side.

**The general form.** A protection that reads its own configuration has two
failure modes, and the tests only ever covered one. *Is the logic right* is what
a fixture answers. *Is it turned on where it runs* is a different question and
needs a different test.

### E-14 — A kick or a ban never reached a game that was already running · CLOSED

Found while building the staff panel, by asking what a kick looks like from
the other side.

E-12 says a kick revokes access and a ban deletes the target's sessions "in the
same transaction". Both true, and both only on the server. The client never
asked again: it validated its token once, at the login screen, and after that
nothing it did in the world depended on the answer. A kicked player went on
walking, fighting and looting until they happened to restart - their saves
bouncing off a dead token with nothing on screen to say so. A ban kept a player
in the game for exactly as long as they chose to stay.

**The heartbeat.** While a character is in the world the game calls
`GET /api/auth/session` every 15 seconds (`Api.HEARTBEAT_SECONDS`), and a 401
sends the player to the login screen with a line saying the server signed them
out. Any other request that bounces off a dead token triggers a beat at once,
so in practice a kick lands in about a second.

**Only a 401 signs anyone out.** A plain 401 elsewhere is not proof - changing
a password answers 401 for a mistyped current password - so other requests
only ASK for a beat and the beat decides. No answer, a 500, and a 404 from some
other program on the port all mean "no verdict". A server restart must not be
a mass kick.

**The login screen does not say which.** A kick, a ban and an expired login
all look the same from the client: the session is gone. A banned player finds
out the rest on their next login attempt, which now shows the ban's end date
and reason (the 403 always carried both; the screen showed neither).

**Presence came with it.** The heartbeat stamps `sessions.last_seen_at`, and
`GET /api/staff/users` reports `online` for a beat within 45 seconds. "Has a
live session" could not answer that - sessions last thirty days and survive
the game being closed - so a kick list sorted by it would have led with last
week's visitors. The stamp is keyed by token, so one open device cannot vouch
for another, and it never extends `expires_at`.

**Limit, stated plainly:** a modified client can ignore the 401 and keep
drawing the world. It still cannot save, trade, loot or report a kill, because
every one of those is authenticated server-side. What the heartbeat fixes is
the honest client that simply never found out.

Twelve checks in `test_api.py` (presence, the heartbeat, the migration) and
a STAFF PANEL section in the Godot suite, each mutation-tested.

### E-15 — Broken access control · AUDITED, NOTHING FOUND · one adjacent gap closed

Came from a school exercise: a shared board where anyone posts a card, then an
"ownership layer" so only the creator can edit or delete theirs. The failure it
teaches is the one Postman finds in a single request — a route that checks you are
LOGGED IN and forgets to check the row is YOURS. Change the id, keep your own
token, and you are editing someone else's record.

So the attack was run here, against every place in this server where a client is
allowed to name a record. **Nothing was found**, and the reason is worth more than
the result, because this server answers the exercise in two ways and only one of
them is the lesson's.

**1. Scope the lookup, do not check afterwards.** One query names the caller:

```sql
SELECT slot, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?
```

so "not yours" and "does not exist" come back as the *same* 404. There is no
window between finding the row and checking it, because there is no separate
check. Loot bags, pending teleports, friend requests, guild invites, the bank,
the backpack and skills all work this way.

That last point is the half of a fix that usually survives broken. A route that
finds the row, sees it belongs to somebody else and returns 403 has fixed the
write and kept the oracle: the attacker now enumerates which ids exist by whether
they get 403 or 404. `_moderation_target()` says the same thing in its own words —
a distinguishable answer would let a mod map out who outranks them.

**2. Better: do not let the client name the row at all.** A trade is the most
valuable row in this database — it moves items and gold between two accounts —
and **not one trade route accepts a `trade_id`.** Every one calls
`_trade_find_open(db, g.user["id"])` and works on whatever comes back. An
ownership bug needs an id to tamper with; these routes have none to offer.

That is the stronger version of the exercise and it is not what the exercise
teaches. Where it is available, prefer it: a check can be forgotten on the route
added next month, and a missing parameter cannot.

Even the owner's debug mint follows it. `POST /api/staff/gold` is owner-only and
*still* will not name another account — it takes `slot` and `amount` and credits
the caller. Passing `username` does nothing, which is a test in
`test_ownership.py` rather than a claim here.

#### The route that had no ownership check, and why it has one now

`GET /api/chat/image/<image_id>` used to serve any stored picture to any
signed-in caller. It was **capability-based**: the id is the SHA-256 of the
bytes, so 256 unguessable bits *were* the permission — you could not hold an id
without having been shown it. Being signed in kept it from being a public file
host run by accident.

That argument held for *guessing* and had two holes, both written down as
honest limits in `SECURITY.md` until they were closed:

- **A whispered picture was only as private as its bytes were rare.** The store
  deduplicates on the content hash, so the same image posted in world chat was
  the same row and the same id, and the whisper was never private.
- **An id, once seen, worked for as long as the row lived.** A player who left a
  guild, or stopped being somebody's friend, kept loading what had been posted
  there.

**Now the route asks the read's own question.** `_may_see_image()` serves a
picture to someone who could read a line showing it, by the chat read's own
rules (`_can_read_chat_row`, the same function a report uses); to an account
that uploaded those bytes (`chat_image_holders`, one row per uploader, because
the shared row's `created_by` remembers only the first); or to a mod judging a
report of it. Anything else is the same 404 as a missing picture. And
`POST /api/chat/send` gives every whisper, friends or guild line **its own copy
under a random id**, so a private picture never shares an id with a world one,
and refuses an id the sender could not see — otherwise an id glimpsed in a log
could be posted to world and served to everyone. `Cache-Control` went from
`public` to `private`, because the answer now depends on who is asking.

The cost: a picture lasts as long as a line showing it (the chat's 24 hours)
rather than until the 128 MB store happened to evict it. The game fetches a
picture when it draws the line and keeps it in memory, so nothing on screen
goes missing.

The id properties are still tested — 64 hex, 256 bits, and **equal to the
SHA-256 of the bytes served** for a world picture — because they are still what
stops guessing. `test_ownership.py` O-3 holds the rest: bob holding alice's id
is refused until a line he can read shows it, the whisper's id is new and random,
carol (who uploaded the very same file) is refused the whisper's copy, the owner
is refused it until it is reported, and bob stops being served a friends-channel
picture the moment he unfriends its poster.

#### The gap that was real: delete was not revocation

256 unguessable bits stop somebody *guessing* a picture. They do nothing about
taking one back from the people who were already shown it — and everyone in the
channel when it was posted holds the id.

`POST /api/chat/delete` removed the `chat_messages` row and left `chat_images`
untouched. The response for a picture carries
`Cache-Control: public, max-age=31536000, immutable`, and the only thing that ever
removed a row was the 128MB oldest-first eviction loop. So a mod deleted an
offensive picture, the line vanished from the feed, and the bytes went on being
served — for a year, cacheably — to everyone who had already seen it. The only
thing that had actually happened was that it stopped being visible to the people
who never saw it.

**Fixed, and reference counted, which is not optional.** `store_relayed_image()`
uses `INSERT OR IGNORE` on the content hash, so the same picture posted twice is
ONE row addressed by two messages — possibly in two different channels. Dropping
the row on the first delete would blank the picture under a second, innocent line.
The route counts the remaining `chat_messages` rows naming that image and deletes
it only at zero, reporting `image_dropped` either way.

A row deleted this way takes its `chat_image_holders` rows with it, so
revocation reaches the uploaders too.

#### And the same idea one layer up: the feed could not be moderated

`GET /api/chat` answers *"messages with an id greater than `since`"* — the right
question for a growing log, and one that **cannot express the opposite**: something
you were already given is no longer true.

So deleting a message stopped it reaching anybody who had not read it yet and did
nothing about the people who had. `chatpanel.gd` was append-only: `_poll()` only
ever called `_add_line()`, and a line left only by `pop_front()` at 100. A mod took
a line down and it stayed on every screen that already had it until a hundred more
pushed it off, or the player closed the game — **exactly the set of players the
deletion was for.** The picture revocation above has the same hole from the client
side: the bytes 404 now, but a client that already decoded one holds the texture.

Closed on both sides. The poll returns `removed`, and the decision that carries the
security weight is that `chat_deletions` stores the deleted line's own `channel`,
`user_id` and `target_id` — the three columns the read builds its permission clause
from — so **deletions are filtered by the very same `where` and `params` as the
messages.** Not a second permission rule to keep in step with the first: the same
one. A line you could never have read cannot produce a deletion you are told about.

Without that, world chat would carry the ids of deleted whispers to everybody. An
id is not content, but it still says a line existed between two people and was
taken down. Sabotage-tested: bypass the clause and O-6 goes red naming both the
world leak and the third party who was never in the conversation.

Reported by time rather than by id, because the deleted message is almost always
*below* the client's cursor where `since` cannot reach — anything inside
`CHAT_DELETION_WINDOW_SECONDS` (120). No second cursor, and the window is enough
only because `chatpanel.gd` starts from the tail when the window opens, so a client
away longer than that is not holding the line. Pruned on write.

#### What holds it

`test_ownership.py`, 79 checks, and the source-level half is the point: for every
table carrying a `user_id`, every `UPDATE` and `DELETE` naming it must scope on
that column or appear on an allowlist — ten entries, each with the reason it
reaches past the caller (four retention sweeps, your own session by bearer token,
the mod delete under `@require_role`, two guild-rank-gated writes, and the loot-bag
deletes that are gated by the `SELECT` above them rather than in the statement).
**That scan then caught the very next thing built in this repo** — `chat_deletions`
carries a `user_id`, so its retention prune had to be justified on the allowlist in
writing before the suite would go green again. Which is the job.
**The allowlist is the most useful thing the suite produces:** it is the complete
list of writes that legitimately touch somebody else's row. Anything not on it is
the bug.

Sabotage-proven eight ways, and two are worth naming because they are the exercise
exactly: drop `AND user_id = ?` from the loot bag lookup and Bob reads Alice's
bag; drop it from `/api/loot/take` and Bob walks off with her potions.

The first version of that source check reported five false alarms, all because
Python concatenates adjacent string literals and the scanner read one at a time —
so `"UPDATE saves SET gold = gold + ?" " WHERE user_id = ?"` looked like an UPDATE
with no WHERE. A check that cries wolf five times is a check somebody switches
off. It joins them now, the way the interpreter does.

### Already solid — credit where due

Worth being explicit so the migration does not accidentally regress them:

- **Password storage** — `scrypt` (Werkzeug's default at 3.x: `32768:8:1`) with a
  per-password random salt and the cost parameters stored in the hash itself, so
  the work factor can be raised later without invalidating old rows. Nothing in
  `app.py` touches `hashlib`.
- **Authentication** — bearer tokens from `secrets.token_urlsafe(32)`,
  server-side sessions with expiry, sessions deleted on ban.
- **Authorization** — a real role ladder; **`owner` is not storable**. It comes
  from the `ELUSION_OWNER` env var, so no request can grant it, with a DB `CHECK`
  as defence in depth and `can_act_on()` guarding staff actions.
- **Character level is server-owned** — proven below (claimed 99, stayed 1).
- **Gold is server-owned** — on both write paths now, and attacked directly by
  `test_economy.py` rather than only implied by the invariant. See E-8 for why
  that distinction cost something.
- **Lusions are server-owned** — created only by a duplicate-pet conversion,
  spent only at `/api/character/revive`, and writable by nobody. See E-10.
- **Death has a price again** — the revive is a server transaction, so the
  penalty cannot be skipped by a client that simply declines to pay it.
- **Loot acquisition is server-owned** — `POST /api/loot/take` writes
  `carry_items` itself rather than trusting the client.
- **Kill rewards are server-rolled** and rate-limited — E-3's weakness is the
  event, not the payout.
- **Gold cannot be minted undetectably** — every creation and destruction is a
  ledger row against a stated invariant, and the suite that checks it is
  mutation-tested rather than merely passing.
- **The vendor destroys gold rather than moving it**, and the kingdom board sums
  the ledger on request instead of keeping a running total. A second place the
  truth lives is a first disagreement nobody can resolve.
- **Two requests cannot spend the same thing** — every write holds the
  database's write lock from its first read (E-17).
- **Staff logins take a second factor** when there is an address to send it to
  (E-20).
- **`.gitignore`** uses prefix patterns (`*.db.*`) precisely because a plain
  `*.db` once let a `.db.before-…` backup slip into a commit. That lesson is
  written into the file itself.

### E-16 — The death screen's other exit was never migrated · CLOSED

**Not found by an audit.** Found by dying: *"I died, came back with full health,
logged out, logged back in and my hp was nearly empty."* 52 out of 504.

The death screen has two exits. `POST /api/character/revive` was moved onto the
server under a comment that begins *"THE SERVER DOES ALL THREE THINGS THAT USED
TO HAPPEN HERE."* The button beside it — the one that pays nothing and loses
everything carried — was left exactly as it was: `gameover.gd` wrote full hp,
mana and stamina into the save slot itself, and zeroed the carry gold in the
same local dictionary.

Both halves were wrong, in **opposite** directions, which is why neither was
noticed for months.

**The heal was a client decision.** The next status push arrived as a rise from
hp 0 to hp 504 with nothing authorising it, and `_reconcile_heals()` — E-9 —
trimmed it to what five seconds of regeneration could produce. The log line had
been printing the answer every single time anybody died:

```
unexplained heal: user=1 slot=0 elapsed=5.0s  hp +504 vs regen 53 + granted 0
heal clamped: user=1 slot=0  hp 504 -> 52
```

`granted 0` is the whole diagnosis. No route had authorised anything, because no
route was involved.

**The penalty was also a client decision, and it did nothing at all.** `gold` is
in `SERVER_OWNED_STATS`, so `PUT /api/player/status` ignores whatever balance a
client sends — E-8's fix, working correctly. The carry gold was zeroed in the
local slot, never destroyed here, and came back in full at the next login. The
empty inventory *did* stick, because losing items is a loss and only gains are
reconciled. So true death took the items, refunded the gold, and left the
character unplayable.

**What this says about the two findings above it.** E-9 and E-10 were marked
Closed and both were accurate *about the endpoints they named*. Neither was a
statement about every path that reaches the same state, and this was a second
path. The word "closed" against a finding means the route is authoritative; it
has never meant nobody can arrive at the outcome another way, and this entry is
here so that distinction is not left implicit.

It is also the clearest evidence yet for **why E-9 is filed as a bound and not a
proof**. The clamp did exactly its job: it refused to store a heal it could not
explain, and it wrote down that it had. It could not make the game stop asking.
A control tells you the truth; it does not fix the thing that is lying.

**And it says something about logs.** The tell was in production for however
long this path existed. Nobody read it. A warning that is only ever read after a
player complains is a warning that is not doing its job — the follow-on is
`deathwatch.py` and whatever else makes a line like that arrive somewhere
instead of waiting.

**The fix.** `POST /api/character/respawn`, beside revive and shaped like it:
refuses a living character with a 409 against the server's own stored hp; burns
the carried gold through `gold_delta()` under the reason `death`, so the biggest
sink in the game lands in the ledger rather than breaking the supply invariant
on the first death anybody took; empties `carry_items` in the same transaction,
so a crash cannot take one and not the other; refills the three pools from the
class curve; and writes a `consume_grants` row under `RESPAWN_GRANT_ID` so the
client's next sync is explained instead of clamped.

`gameover.gd` calls it and applies the answer. `_clear_carry_on_death()` writes
no hp at all any more, and a failed request does **not** fall back to the local
restore — that fallback *is* the bug, and the Godot suite checks for its absence
specifically.

**21 checks in `test_economy.py`**, sabotage-proven four ways: a bare `UPDATE`
instead of `gold_delta()` (the invariant goes red in four places), no grant row,
no dead check, and leaving `carry_items` alone. One of those checks was itself
wrong first time round — it measured the character whose only burn *was* the
death, so a `lost` column summing every burn passed it; `tradealice`, who has
spent and never respawned, is the control that separates them.

### E-17 — Two requests sent together could both spend the same thing · CLOSED

**Found by playing.** A warrior's slash wave killed two enemies at once, the
game sent both kill reports together, and one kill's XP was gone. Six kills
sent together banked 99 XP of the 676 their answers had promised; the game
added up all 676, levelled up before the server did, refilled its mana, and
the heal check (E-9) clamped that as a cheat.

**The cause was one shape in many routes.** sqlite3 opens a transaction at the
first INSERT, UPDATE or DELETE, not at the first SELECT. A route that read a
balance, worked out the new one in Python and wrote it back could run twice at
once, and both copies read the old figure. Lost XP was the harmless version.
The same shape let **one loot cell pay out five times, one purse be deposited
into the bank twice, and one potion explain five heals**. That is item and gold
duplication for anyone who can send two requests at once, which is anyone.

**The fix is one rule, not a list.** `get_db()` runs `BEGIN IMMEDIATE` for
every POST, PUT, PATCH and DELETE before the route reads anything, and
`_WriteLockedConnection` takes the lock again after each `commit()`, so a route
that commits part-way is still covered (THE WRITE LOCK in app.py). A new write
route gets it without anyone remembering. Nine routes opt out with
`@no_write_lock` because they do slow work first, a password hash or a picture
fetch; `test_concurrency.py` names all nine, so a tenth fails the suite until
somebody writes down why. It cost nothing measurable: `loadtest.py` on the kill
route ran at about 550 writes a second before and after.

**What holds it.** `test_concurrency.py` sends real requests together on real
threads against a real server, and holds each route's read-to-write window open
for 50 ms, so the old race fails every run rather than now and then.

**The honest limit is the database.** The lock is SQLite's. A move to Postgres
or anything else has to keep this promise with row locks or serialisable
transactions, because Postgres, like most databases, does not make every write
wait its turn by default. Run `test_concurrency.py` against the new database
before anything else.

### E-18 — Accept agreed to whatever was on the table when the trade ran · CLOSED

Found by driving a trade between two real accounts, as were the other trade
fixes in CLAUDE.md.

**The swap.** Accept meant "run the trade as it stands", not "run the trade I
looked at". An offer changed a moment before the click was accepted anyway, so
the other side could turn "accept the sword" into "accept the stick" by being
faster than a poll. `trades.revision` now moves on every real change, and
`POST /api/trade/confirm` must name the revision the client drew. The UPDATE is
conditioned on it: a compare-and-set, not a read-then-write.

**The lost item.** Whoever accepted first is not the one whose request runs the
trade, so their game was told nothing, and its next whole-bag save deleted what
they had just received. Both characters are now flagged in the trade's own
transaction, a whole-bag save is refused while the flag is up and answered with
the bag the server holds, and the trade poll and the broadcast poll both
deliver it.

`test_trades.py`; SECURITY.md invariants 19 and 20.

### E-19 — Two games on one account each saved their own bag · CLOSED

Each copy of the game held its own bag, and the bag write replaces the whole
bag, so two games on one account overwrote each other and lost items. The same
gap let a stolen token keep working beside the owner's own.

**One login at a time.** A login that gets its token ends every other session
on the account, and so does `POST /api/auth/resume`, which a game calls on boot
with a remembered token. Resuming keeps the login's original end date, so a
remembered login cannot renew itself forever. The ended token's hash is kept
for a day so its next request can be told `signed_in_elsewhere` rather than a
bare 401. A wrong password, a ban or a staff code still owed ends nothing.

`test_accounts.py`; SECURITY.md invariant 22.

### E-20 — A staff password was the only thing between a guesser and the moderation desk · CLOSED

The lockout (E-5) slows guessing; it does not change whose password is worth
guessing, and staff names are on every screen. One guessed mod password is the
staff desk: bans, notes, linked accounts and IP addresses.

**A second factor.** For a mod, dev or the owner with a confirmed recovery
address, a correct password answers **202** with no token and emails a
six-digit code; the same login sent with the code gets the token. Codes are
hashed, live 15 minutes, burn after five misses, and count toward the account
lockout; at most one is sent a minute. A wrong code is 400, never 401, which
the game would read as a wrong password. Nothing is sent before the password
and the ban check pass.

**Once per computer, not once per login.** A login that got in with a code is
given a device token, stored as a hash. The same computer sending it back needs
no code for 30 days (`TRUSTED_DEVICE_DAYS`), and only at the rank it was
trusted at, so a promotion or demotion asks again. A password change, a
recovery reset and "log out everywhere" forget every computer.

**The named limit.** With no confirmed address, no mail on the server, or
`ELUSION_STAFF_LOGIN_CODES=off`, the step stands aside rather than lock the
owner out of their own server. The login answer says so (`staff_unprotected`),
the game tells that player once, and the boot log says it for the server.

`test_staffcode.py`; SECURITY.md invariant 21 and its honest limits.

---

### E-21 — Session tokens were stored as issued · CLOSED

Found on day 2 by the owner's security course, which asked for an audit of how
credentials are stored. Passwords passed: scrypt, salted, never logged, the same
cost for a wrong name as a wrong password. Recovery codes, staff login codes,
trusted-device tokens and the record of ended sessions were all hashes too.
The one secret that logs a player straight in was not: `sessions.token` held
every live bearer token exactly as `issue_token()` minted it.

**What it cost if exploited.** Anyone holding a copy of the database - a backup
sent somewhere, a disk image, a file read through some other hole - could send
any stored token as `Authorization: Bearer` and be that player, for the rest of
the token's thirty days. Staff included, and a staff token skips the emailed
code (E-20), because the code guards the login, not the session. E-6's rotation
and E-19's one-session rule shortened the window; neither closed it.

**The fix.** `sessions.token_hash` holds `SHA-256(token)`; `issue_token()`
stores the hash and returns the token, and every lookup - `user_for_token()`,
the presence stamp, logout, `_end_other_sessions()` - hashes what it was sent.
SHA-256 rather than scrypt on purpose: a token is 32 random bytes, so there is
nothing to guess and nothing a slow hash would protect, and it is checked on
every request.

**Nobody was signed out.** `_migrate_hash_session_tokens()` replaces each stored
token with its own hash and renames the column, in one transaction, so every
game that was open keeps working. It was run on a database with eight live
sessions and a login made on the old code: the same token was accepted
afterwards, no raw token was left in the table, and the next login still told
the old game it had been signed in somewhere else.

**Held by.** `test_accounts.py` A-5 reads the table the way a thief would: no
`token` column, the row holds the SHA-256, no cell anywhere is a live token, and
the stored hash sent as a bearer token gets 401. `test_api.py` MIGRATION boots
an old-shaped table holding a raw token and checks it is hashed, still signs in,
and is not hashed twice on the next boot. Broken five ways on purpose (raw token
stored, migration skipped, renamed without hashing, the kept session compared
unhashed, the heartbeat stamping by the raw token) - each one went red.

## The security bot — attacking a running server, not the source

`security_bot.py` is a red-team harness that asks a different question from
`test_security.py`. That suite imports `app.py` and drives it through Flask's
test client; it goes red when a fix is deleted from the code. The bot spawns a
real server on a socket, registers fake players, and attacks it over HTTP with
nothing an outside attacker would not have. It goes red when the *running*
server lets an attack through — a bad deploy, a stale binary, a config that
skips a guard — which is the failure the source-level suite cannot see.

    python3 security_bot.py                 # spins up its own throwaway server
    python3 security_bot.py --players 20

**It re-derives the invariants from ground truth, not the server's report.**
Each attack is verified by reading the SQLite rows directly — `saves.gold`,
`accounts.lusions`, `carry_items`, `skills` — never through a read endpoint,
because a read endpoint can 404 in a broken state (a giant gold write does
exactly that) and a readback that silently returns zero would call a landed
attack "blocked". The gold check is the full ledger equation from ground truth,
and it is cross-checked against the dev `/api/economy/supply` endpoint: if the
two disagree, that disagreement is itself a finding.

**The attacks, each tied to a finding it would reopen:** mint gold via
`/player/status` (E-8), mint lusions via `/account/lusions` (E-10), write hp
above the maximum (E-9), fabricate an item in the backpack (E-1), claim a skill
past the cap (E-2), equip an item you do not own (E-1). Kill farming (E-3,
open) has no invariant to break — the server pays out a bounded number on
purpose — so it is reported as a measured sustained rate against the content
ceiling, never as a pass or fail.

**Safe by construction.** The bot mutates whatever it points at, so by default
it starts its own server on a scratch database and deletes it. `--server` needs
`--allow-live`, and is refused outright against a database named `elusion.db`.

**It was validated by breaking the server on purpose.** Building it turned up
three attacks that could not actually fail — a skill claim in the wrong shape,
an item claim refused on the stack ceiling before the guard it meant to test,
and an equip that named a slot the server derives for itself — each of which
would have reported "blocked" forever against a vulnerable server. All three
were corrected, then confirmed: with E-1, E-2, E-8, E-9 and E-10 each
reintroduced one at a time, the matching attack line and the matching invariant
both go red and the run exits non-zero. An attack that cannot detect its own
target is worth less than no attack, because it reads as coverage.

---

## Not code — required before this is reachable by anyone else

These cannot be closed in `app.py` and are listed so they are not mistaken for
done:

1. **HTTPS.** Tokens and passwords must never cross plain HTTP. TLS terminates
   at the same reverse proxy that `ELUSION_TRUSTED_PROXIES` describes, so these
   two are one deployment step, not two.
2. **A real WSGI server.** `app.run()` is Flask's development server; correct for
   local play, wrong for anything public.
3. **Set `ELUSION_TRUSTED_PROXIES`** to match the deployment, or the per-IP
   throttle is either useless or catastrophic — see E-5.
4. **If the database changes, the write lock has to come with it.** E-17's fix
   is SQLite's `BEGIN IMMEDIATE`. On another database it must become row locks
   or serialisable transactions, proven by running `test_concurrency.py`
   against it before the first player connects.

---

> Everything below is the original audit as written. Line numbers and the
> `app.py` size refer to the file at that time, and its "already solid"
> list has been moved up and updated rather than repeated here. The
> findings it names are tracked in the table above.

## The original audit

**Status (as audited): authenticated, and partly server-authoritative — but the
client is still trusted about most of its own game state.** Unlike the recipe
skeleton, this API is *not* wide open: every mutating route is `@require_auth`,
staff routes add `@require_role`, passwords are hashed, and some state (character
level, kill rewards, loot) is genuinely server-owned. The remaining exposure was
one specific seam — **what an authenticated player is allowed to *claim* about
their own inventory and skills** — which the code itself flagged:
`_report_unexplained_gains()` described itself as *"SHADOW MODE … REFUSES
NOTHING … step one of moving the backpack to server authority."* This file
records where that migration stood, attacker-first (and the update above records
where it went).

Verified against `app.py` (3883 lines) by reading the routes and by running an
**identical copy on a throwaway database** — the real `elusion.db` was never
touched. Every result below is reproducible with the `curl` calls shown.

---

## Threat model

The recipe app's attacker was "anyone who can reach the port." Here that door is
closed, so the attacker is different and more realistic for an MMO:

- **Attacker:** a **logged-in player running a modified client.** They have a
  real account and a valid token — they got both legitimately. The question is
  no longer "can a stranger get in" but **"how much can a real player lie to the
  server about what happened in the game."**
- **Why this is the right model:** the game client runs on the player's machine.
  Anything the client computes, the player can forge. The only facts that are
  true are the ones the *server* establishes or checks. Every endpoint is a
  claim the client is making; security is which claims the server verifies.

---

## Proven, right now — an authenticated player can fabricate items and skills

Reproduced against a local copy: register → log in (a **real, valid session**) →
create a level-1 warrior with an empty backpack → then lie.

| # | What a logged-in player claimed | Request | Result |
|---|---|---|---|
| 1 | "My backpack holds 99 ember swords + 99/99 potions" | `PUT /api/character/inventory` | `200` — **stored verbatim.** No kills, no shop, no loot roll. |
| 2 | "My attack and magic skills are level 99" | `PUT /api/character/skills` | `200` — **stored verbatim.** Only check is `level ≥ 1`; no ceiling. |
| 3 | *(contrast)* "My character level is 99" | `PUT /api/save {…,"level":99}` | `200` but level **stayed 1** — server ignored the claim. |

```bash
# after registering + logging in as a normal player and creating a character:
curl -X PUT $API/api/character/inventory -H "Authorization: Bearer $TOK" \
  -H 'Content-Type: application/json' \
  -d '{"slot":0,"inventory":[{"item_id":"embersword","quantity":99}]}'
#   -> 200, and GET /api/character?slot=0 now shows embersword x99
```

Rows 1 and 2 are the finding. Row 3 is the proof that the server *can* hold a
line when it decides to — and mostly does elsewhere — which is what makes 1 and 2
fixable rather than fundamental.

---

## Findings, named

### E-1 — Inventory is client-authoritative · *critical (economy)*
`PUT /api/character/inventory` replaces the whole backpack from the request body.
The server validates the backpack's **shape** impeccably — valid slot, ≤ 20
cells, known `item_id`s, stack ceilings — and then stores whatever passed. It
never asks **where the items came from.** `_report_unexplained_gains()` compares
the claim to what it last recorded and *logs* a gain; it refuses nothing (by
design, for now — see its own comment). Effect: the entire item economy is
forgeable by any player. Ember swords, stacks of greater potions, anything in
`gamedata.json`, in any quantity the cell/stack limits allow.

### E-2 — Skills are client-authoritative and uncapped · *critical (balance)*
`PUT /api/character/skills` replaces skills from the body. The only guard is
`level ≥ 1` and `xp ≥ 0` — **no upper bound.** A player sets attack/magic/any
skill to 99 (or higher) instantly. Because skill proficiency feeds combat power,
this is a direct power cheat, not a cosmetic one.

### E-3 — The kill *event* is asserted, not verified · *high*
`POST /api/combat/kill` is the **best-defended** of the three and still trusts
the core claim. The server does the right things around it — it **rolls the
rewards itself** (the client can't name its XP), refuses reward-less enemies (the
slime-split exploit), and rate-limits with a token bucket. But it never verifies
the *fight happened*. A modified client reports kills of the highest-value
killable enemy at the maximum sustained token rate, having fought nothing. **The
rate limit caps the speed of the fraud, not its existence** — and E-1 makes even
that moot for *items*, since a cheater who wants an ember sword just PUTs it
rather than farming for it. This matches the honour-system caveats already
written into the client's own respawner and `report_kill` comments.

### E-4 — `app.run(debug=True)` · *critical if ever exposed* · **fix on its own terms**
**Closed — see E-4 and E-4c above.** Kept as written at audit time; line 3883 was
the run block then, and it is the last line of `app.py` now.

Line 3883. Identical footgun to the recipe app: the Werkzeug interactive debugger
turns any unhandled exception on a reachable-off-localhost build into **arbitrary
code execution on the host** — which here is the box holding `elusion.db` and its
real password hashes. Must be `False` anywhere anyone else can reach it. This is
unrelated to the game-state work and worth doing immediately.

### E-5 — No login throttling · *medium*
The only rate limit in the app is the combat token bucket. `POST /api/auth/login`
has no lockout, delay, or attempt cap, so passwords can be guessed online as fast
as the server answers. With `TOKEN_TTL` set to "games shouldn't log people out,"
a guessed password is a durable session.


## Why "it returns the right data" ≠ "it's secure" — the Elusion version

The inventory endpoint is a *better* example of this than the recipe app was,
because it validates so much. It checks the slot, the array length, every
`item_id`, every stack ceiling — and that's exactly why it returns a clean,
well-formed backpack every time. It answers **"is this a valid backpack?"**
flawlessly.

It just never asks the only question that matters for an economy:

> **"Did this player come by these items legitimately?"**

Shape validation is not provenance. A perfectly-formed request describing 99
ember swords is *well-formed* and *unearned* at the same time — the same way the
recipe API's `DELETE` was *correct* and *unauthorized* at once. "Returns the
right data" is a statement about the data's format. Security is a statement about
the **caller's authority to bring that data into existence** — and for a
player-run client, the server is the only place that authority can live.

---

---

## Fix roadmap — what is left

The original roadmap had six steps and grew to ten. Every step but the last is
done.

1. ~~`debug=False` by default~~ → **E-4 closed.**
2. ~~Close the client-side item-granting paths~~ → done; every legitimate gain
   flows through the server.
3. ~~Flip the reporter from log to reject~~ → **E-1 closed.**
4. ~~Throttle `login`~~ → **E-5 closed**, per-account and per-IP, with a log.
5. ~~Add `POST /api/auth/password`~~ → **E-6 closed**, rotation and revocation
   in one transaction, throttled like login.
6. ~~Give **attack** server-granted XP~~ → done; it is granted at the kill and
   dropped from client syncs.
7. ~~Add a consume endpoint~~ → **E-7 closed**; and ~~gold made server-owned~~ →
   **E-8 closed**, the one that was never on this list because nobody had
   noticed it.
8. ~~Export the regen constants and `restore_amount`, then give revive an
   endpoint~~ → done, and **E-9 closed** on top of them. Clamped at a loose
   margin with the honest one still logging, so the band between the two is the
   evidence for tightening it later. The one follow-up worth remembering is not
   code: **watch that band, and lower `HEAL_CLAMP_MARGIN` to 1.25 once it has
   stayed empty across real play.**
9. ~~Give defense, agility and magic server-granted XP~~ → done, as E-2
   records: the client reports raw activity to `/api/skill/train` and the
   server clamps each skill to a per-second ceiling times elapsed time. That is
   a bound, not an observed event; server-observed events arrive with the same
   encounter state that step 10 needs.
10. **Verify the kill** — server-side encounter state → closes **E-3**, the
    last one open. Note that tying kills to the character's *area* is not a
    substitute: `saves.area` is written by the client, so a client that wants
    boss kills simply claims to be on the boss floor first.

    ~~or at minimum tie kill reports to server-known enemy spawns~~ →
    **measured and rejected.** It is 9% tighter, and two neighbouring ideas
    (per-scene attribution, a per-enemy time-to-kill floor) are 16.7% and 1.4%.
    The table under E-3 has the numbers. Nothing short of observing the fight
    moves this meaningfully, so the next step on E-3 is the architecture
    change or nothing — there is no cheap intermediate left to buy.

The original test still applies to every step: re-run the three `curl` claims
from the audit above. Rows 1 and 2 now come back trimmed or capped, the way
row 3 already refused on day one.
