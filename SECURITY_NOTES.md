# Security Notes — Elusion API

This started as a self-audit: I attacked my own API as a logged-in player with a
modified client, wrote down what I could get away with, and named five findings.
This file is the record of that audit **and** of what happened to each finding
since. The original attacker's-eye account is kept below because the reasoning is
the point — the fixes only make sense against the thing they fix.

## Status

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| E-1 | Inventory is client-authoritative | critical (economy) | **Closed** |
| E-2 | Skills are client-authoritative and uncapped | critical (balance) | **Partly closed** — capped, and 2 of 6 skills fully server-owned |
| E-3 | The kill *event* is asserted, not verified | high | **Open** — named, rate-limited, not fixed |
| E-4 | `app.run(debug=True)` | critical if exposed | **Closed** |
| E-5 | No login throttling | medium | **Closed** |
| E-6 | Long token with no credential rotation | low–medium | **Closed** |

Four closed, one partly, one open. **E-3 is the honest one**: it is the deepest
finding, it is still live, and no amount of work elsewhere substitutes for it.

Covered by three suites, all green together — 488 assertions:
`test_api.py` (419) · `test_security.py` (25) · `test_throttle.py` (44).

---

## What changed, per finding

### E-1 — Inventory · CLOSED

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

### E-2 — Skills · PARTLY CLOSED

Two separate pieces of progress, and the gap between them is the finding that
remains.

**The ceiling.** `MAX_SKILL_LEVEL = 99` clamps over-cap claims from non-staff.
This kills the absurd-value cheat — a level-1 character claiming 2^31 attack —
but it is a guardrail, not authority. A client can still claim any level *up to*
the cap.

**Real authority, for two skills.** `fishing` and `cooking` are now genuinely
server-owned: `/api/fishing/catch` and `/api/cooking/cook` grant their XP against
items the server itself consumed, and `PUT /api/character/skills` **drops** those
two from whatever the client sends. They are dropped silently rather than
refused, deliberately: an un-updated client still sends all six every save, and
400-ing an otherwise honest sync over a field it is not allowed to set would
break saving entirely.

**What is left:** attack, defense, agility and magic still have no server-side
grant path, so they remain claimable up to the cap. Closing this means giving
each one a server-observed event that grants its XP — the same shape fishing and
cooking now have. Fishing and cooking are the proof the shape works.

### E-3 — The kill event · OPEN

Unchanged, and deliberately last. `POST /api/combat/kill` still does the right
things *around* the claim — the server rolls the rewards itself, refuses
reward-less enemies (the slime-split exploit), and rate-limits with a token
bucket — but it never verifies the fight happened.

**The rate limit caps the speed of the fraud, not its existence.** Closing this
needs server-side encounter state, or at minimum tying kill reports to
server-known spawns, and that is a larger piece of work than everything above it
combined. It is listed here rather than quietly omitted because an open finding
you have named is a different thing from one you have not noticed.

### E-4 — Debug · CLOSED

`app.run(debug=_debug)`, where `_debug` is off unless `ELUSION_DEBUG=1`. The
Werkzeug interactive debugger turns any unhandled exception on a reachable build
into arbitrary code execution on the box holding `elusion.db` and its password
hashes, so the default had to be the safe one.

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
- **Loot acquisition is server-owned** — `POST /api/loot/take` writes
  `carry_items` itself rather than trusting the client.
- **Kill rewards are server-rolled** and rate-limited — E-3's weakness is the
  event, not the payout.
- **`.gitignore`** uses prefix patterns (`*.db.*`) precisely because a plain
  `*.db` once let a `.db.before-…` backup slip into a commit. That lesson is
  written into the file itself.

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

The original roadmap had six steps. Five are done; this is what remains, in
the order it should happen.

1. ~~`debug=False` by default~~ → **E-4 closed.**
2. ~~Close the client-side item-granting paths~~ → done; every legitimate gain
   flows through the server.
3. ~~Flip the reporter from log to reject~~ → **E-1 closed.**
4. ~~Throttle `login`~~ → **E-5 closed**, per-account and per-IP, with a log.
5. ~~Add `POST /api/auth/password`~~ → **E-6 closed**, rotation and revocation
   in one transaction, throttled like login.
6. **Give attack/defense/agility/magic server-granted XP**, the way fishing and
   cooking now have → closes **E-2** properly rather than capping it. *Cheap
   interim check available today:* reject any skill level the character's own
   level could not support — skill 99 on a level-1 character is a state
   legitimate play cannot produce, so it is both an exploit and a ready-made
   detection signal.
7. **Verify the kill** — server-side encounter state, or at minimum tie kill
   reports to server-known enemy spawns → closes **E-3**, the deepest one,
   correctly saved for last.

The original test still applies to every step: re-run the three `curl` claims
from the audit above. Rows 1 and 2 now come back trimmed or capped, the way
row 3 already refused on day one.
