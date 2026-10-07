# E-3 before launch — what to do, and what not to bother with

`SECURITY_NOTES.md` already says what E-3 **is**, why three tightenings were
measured and rejected, and why the next move is not a tighter bound. None of
that is repeated here. This answers a different question: **what to do about it
in the last phase before launch**, and it is written to be decided from rather
than read.

Everything below was checked against the code on 2026-09-28 rather than
remembered.

---

## The one thing that changed, and it is not in the code

`SECURITY_NOTES.md` states the current posture and, to its credit, states the
condition it rests on:

> **This is detection, not the prevention E-3 ultimately needs, and that is the
> honest posture *while this is single-player*.** The fraud today is a player
> cheating their own save — so making it visible and bannable is proportionate,
> and the architecture change is the move for the day it stops being self-harm.

That is correct today and **the condition expires the day a second person logs
in.** Not because anything in the kill path changes, but because two things that
already exist start carrying the consequences outward.

**Trade moves the loot.** `_execute_trade()` crosses items between two accounts
and moves gold as a transfer. There is no gate on it — no level floor, no
account age, no switch. `trade_offer` validates the slot and the username and
nothing else. So minted loot is one trade away from somebody else's bank, and at
that point the fraud is no longer self-harm.

**The board ranks by the thing the fraud produces.** `/api/economy/kingdom`
ranks players by gold *destroyed* — spending and death — and a guild ranking is
built from the same rows. Gold minted from invented kills spends exactly like
earned gold, so a cheater climbs the shared board and pulls their guild up with
them. Nothing on that board is personal to the reader.

**`canary.py` will not notice, and is right not to.** It checks that gold and
lusions are conserved. Gold from a fake kill is a *legitimate* ledger event —
the server rolled it and wrote the row — so the supply invariant holds
perfectly. The economy is not leaking; it is being inflated through the front
door. Only `killwatch.py` looks at the door.

So the honest one-line summary: **E-3's current posture is correct, and it is
scheduled to become wrong on a date you choose.**

---

## What is not worth doing, so nobody spends a week on it

`SECURITY_NOTES.md` measured three refinements — per-spawn-point claims,
per-scene attribution, per-enemy time-to-kill — at 9%, 16.7% and 1.4% tighter
against an 18x gap. All three rejected, and the reasoning holds.

**One more idea belongs on that list, because it is the one that sounds newest
and is actually the oldest.** *Server-issued encounter tokens*: the server hands
out a nonce per spawn, and a kill report must carry one it issued and has not
seen. It feels categorically different from a bound — it is verification, not
arithmetic.

It is not. If spawns run on a timer, the server issues nonces at
`placed x (3600 / respawn)` per hour = **5,040/hr against today's 5,544**. That
is the per-spawn-point number, 9% tighter, already measured and already
rejected. The nonce only stops being a restatement of the ceiling if spawns are
**demand-driven** — issued when a player is actually near the spawn point — and
that needs server-owned positions, which is the architecture change it was
trying to avoid.

**The general shape, and it is worth keeping:** every idea that avoids knowing
where the player is collapses into a bound on what the world contains, and the
spawn ceiling already extracts nearly all of that. There is no useful middle
rung. The ladder has two real steps and a gap.

---

## The blast radius, by name

What a fabricated kill actually reaches today, and which code carries it:

| reaches | how | today |
|---|---|---|
| the cheater's own character | XP, level, skills | self-harm — the current posture |
| **other players' inventories and purses** | `_execute_trade()` — no gate of any kind | **turns it into everyone's problem** |
| **the shared leaderboard and guild ranking** | `/api/economy/kingdom` ranks by gold destroyed | **social, visible, and the face of the game** |
| the gold supply | `gold_delta()` writes a legitimate row | `canary.py` correctly sees nothing wrong |
| detection | `kill_reports` + `killwatch.py` | IMPOSSIBLE alarms, SUSPICIOUS review list |

The middle two rows are the launch question. The rest is unchanged.

---

## Three options

### A — ship as it stands, watched

Nothing changes. `killwatch.py` runs hourly for IMPOSSIBLE and weekly as a
digest, and a flagged account gets banned by hand.

**Cost:** zero, it is already built and tested.
**Buys:** the fraud is visible and bannable rather than invisible.
**Does not buy:** anything about the two middle rows above. A cheat that trades
its proceeds away before anybody reads the digest has already landed, and a ban
afterwards does not un-trade it.
**Honest with:** people you know. `killwatch.py` is a review list for a human,
and with a handful of players a human can actually read it.

### B — gate the blast radius instead of the hole

Leave E-3 exactly as it is and stop the fraud *travelling*. Trade becomes a
server switch, in the same shape as maintenance and PvP — one row in
`server_settings`, an owner-only route, carried on `/api/status`, with the
client saying plainly when it is off.

Why that shape and not a level floor or an account-age rule: those are guesses
at a threshold, and this project has already written down what a threshold
picked without data costs (the E-2 interim skill bound, which did not survive
contact with the client). A switch is not a guess. It is off until you have
watched the board for a week, and it goes off again in one request the first
time `killwatch.py` alarms — which is the property you actually want at 3am.

**Cost:** small, and it is a pattern this codebase has done twice. A
`TRADE_KEY`, a refusal on the five `/api/trade/*` routes, the flag on
`/api/status`, a banner on the trade panel, and tests that the refusal holds and
that the switch is read rather than remembered.
**Buys:** the economic row of that table, entirely. A cheat stays inside the
account that made it.
**Does not buy:** the leaderboard row. A cheater still tops the board.
**Note:** this makes trade unavailable to honest players too, which is a real
cost to a feature you built. It is a launch posture, not a permanent one.

### C — the server observes combat

The actual fix. The server owns enemy instances and their hp, receives attack
events rather than kill claims, and decides what died.

**One thing makes this cheaper here than it sounds.** Players cannot see each
other — there are no remote bodies, which `/api/players/nearby` says in its own
comment. So the server does **not** need a shared world simulation. It needs
per-player authoritative encounter state: this player, these enemies, this hp.
That is a materially smaller thing than an MMO combat server, and it is worth
knowing before pricing it.

**Since then (0.7.0, 6 October):** players do see each other (`presence.py`),
and each area's monsters are run by one game for everyone in it - the area's
leader. So C would now mean one server-owned simulation per area, not per
player, which is more than the paragraph above prices. The shape is already
built, though: the game's `monstersync.gd` sends snapshots and events out and
takes hits in, and C is the server taking the leader's seat.

**What it still needs, in order:**

1. **Server-owned player position.** `saves` holds no coordinates at all — not
   stale ones, none. This is a column pair *and* a heartbeat carrying them, and
   it is the first thing in this project that costs real requests per player per
   second. It is also step 1 of the world-boss ladder in `CLAUDE.md`, so it is
   not spent only on this.
2. **Server-owned enemy instances.** Spawn, position, hp, per player. The
   respawner moves server-side, or at least its authority does.
3. **Damage decided server-side.** The client sends "I attacked, facing X, at
   T"; the server checks range against positions it owns and applies damage.
   This is also what closes **E-9** (hp is client-written today and only
   clamped).
4. **Client prediction and reconciliation.** The part that is actually hard. A
   hit that waits for a round trip feels terrible, so the client must predict
   and then be corrected — and *being corrected gracefully* is where the work
   is, not in the server maths.

**Cost:** a season, not an afternoon, and step 4 changes how the game feels. It
is shared work with E-9 and with the world boss, which is an argument for doing
it, not against.
**Buys:** E-3 and E-9 closed, and the world boss becomes possible.
**Risk:** it touches the thing players feel most. A worse-feeling game that is
harder to cheat is not obviously a better game at this stage.

### C, step 1 — built 7 October 2026 (0.10.0): the server watches

The owner chose to start C, in three steps, the first only watching. Steps 1
and 2 of the list above turned out to be already half-paid for: every
position (presence states) and every monster's spawn, position and death (the
leader's world) already passed through `presence.py`. So step 1 reads them.

- **The books** (`combatbook.py`, api CLAUDE.md "The books on every monster"):
  the server's own count of every monster's health, every hit held to what
  that character could deal (`gamedata.combat_bounds()`), every spawn held to
  the area's map (`gamedata.AREAS`, exported with the spawn points and respawn
  times), every death judged AGREED / SHORT / NOT DUE into `combat_kills`, and
  everything that did not fit counted in `combat_flags`.
- **The game's part** (0.10.0): a leader sends its world even alone, with its
  own player's hits inside it, and renews its ticket after an equip.
- **Nothing in play changes.** The relay is untouched; kills are paid as
  before. `killwatch.py` matches every paid kill to the books (tier WATCHED).

**What the first week should answer**, reading `killwatch.py` without
`--quiet`: does honest play ever leave a SHORT, a `hit_too_big`, a `too_fast`
or a `too_quick`? Expected noise, already explained: kills by a game older
than 0.10.0 or with its presence link down are "never seen"; teleporters and
the GM's Go to are `jumped`; a pet's long arrow can be `too_far`.

**Step 2 - enforce.** A kill is paid only with an AGREED row for that
account, character and monster (the receipt), checked by `/api/combat/kill`;
a game not connected to presence earns no kill rewards and says so. The bounds
lose their slack where the week showed honest play never needs it.

**Step 3 - the rest of the list above:** the player's hp on the server (E-9),
then server-side monster AI, which is what the world boss needs.

---

## What I would do

**A + B for launch, C after — and C before the game is public.**

The reasoning is the blast-radius table. With people you know, option A is
proportionate and already paid for. What makes launch different from today is
not the cheating, it is the *transmission*, and B closes transmission for a
fraction of C's cost without touching how the game feels.

B is also the thing you want to already have rather than to need. The switch is
worth building even if it ships in the "on" position, for the same reason the
maintenance switch was worth building before there was anyone to maintain
against: the day you want it, you want it in one request.

**The leaderboard row stays open under A + B,** and that is a deliberate
acceptance rather than an oversight. A cheater tops the board and there is no
cheap fix — the board is built from the ledger, and the ledger is telling the
truth about gold it legitimately minted. The mitigation is social: it is your
board, you can read `killwatch.py`, and you can remove an account.

**C is not launch work.** Starting it now means launching later with a
half-migrated combat path, which is a worse position than launching with a named
open finding and a watch on it. It is the first big thing *after* launch, and it
arrives with E-9 and the world boss attached.

---

## What only you can decide

1. **Who is in the first cohort.** Friends-and-family makes A + B comfortable.
   A public link does not, and would move C in front of launch.
2. **Whether trade ships on or off.** Building the switch is the recommendation
   either way; which position it starts in is a judgement about your players,
   and you know them.
3. **Whether the leaderboard risk is acceptable.** It is the one thing A + B
   leaves open, and it is the most visible part of the game.

---

## If the answer is B, here is the shape

**Built, 6 October 2026.** The owner chose B, with "allow trade to finish" for
the in-flight case: off refuses only a new offer, and an open trade may be
changed, accepted or cancelled until it finishes or expires. Added on top, from
outside advice the owner brought: a fresh mythic or Perfect find waits 48 hours
before it can be traded, which targets exactly what a cheated kill is for and
leaves everyday trading alone. Account-age and volume caps were left for a
public launch, to be sized from real trade logs rather than guessed. See
api/CLAUDE.md, "Trade gates", and `test_tradegates.py`. The shape below is
what was planned.

Kept short deliberately — the pattern already exists twice and the detail
belongs in the commit, not here.

- `TRADE_KEY = "trade"` in `server_settings`, beside `MAINTENANCE_KEY` and
  `PVP_KEY`.
- `POST /api/server/trade`, owner only, 404 to everyone else — the same refusal
  every owner route gives, so a refusal does not confirm the route exists.
- The five `/api/trade/*` routes refuse with **503** while it is off, not 403:
  the credentials were never the problem, and this is the same reading
  `maintenance_refusal()` already made for the same reason.
- Carried on `/api/status`, which needs no token, so the client can say so
  before anyone logs in.
- The trade panel says it plainly, in the wording the PvP banner established —
  what is true, and what is not being claimed.
- Tests: the refusal holds on all five routes, the switch is re-read rather than
  remembered, an in-flight trade is handled (decide: cancel it, or let it
  finish — it must not be left half-executed), and the owner gate answers 404
  to a non-owner.

The in-flight case is the one with a real decision in it, and it is the one
worth arguing about before writing anything.
