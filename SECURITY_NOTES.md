# Security Notes — Elusion API

> ## Update — hardening shipped
>
> The migration this file used to describe as "shadow mode" is now enforcing.
> What changed, all covered by `test_security.py` (25 checks) with the existing
> `test_api.py` still green (419):
>
> - **E-1 Inventory — CLOSED.** `PUT /api/character/inventory` now clamps every
>   item to what the server actually granted. A regular client's fabricated
>   items are trimmed to nothing; reorders, drops and server-granted items pass
>   untouched. Safe because every real source (loot, bank withdraw, staff grant)
>   already writes `carry_items` first — verified there is **no** live
>   client-side item source (no shop/craft/cook exists yet). Staff are exempt
>   (they can already self-grant).
> - **E-2 Skills — MITIGATED.** A server-side cap (`MAX_SKILL_LEVEL = 99`) now
>   clamps over-cap claims from regular clients. This kills the absurd-value
>   cheat. It is *not* full authority: skills still have no server-granted-XP
>   path, so a client can still claim any level *up to* the cap. Full close needs
>   each skill's XP granted server-side (only attack-XP has a server source
>   today) — that's the remaining work below.
> - **E-4 Debug — CLOSED.** `debug` now defaults off; on only with `ELUSION_DEBUG=1`.
> - **E-5 Login throttle — CLOSED.** 8 consecutive failures freeze an account for
>   15 minutes; a correct password resets the streak.
> - **E-3 Kill event — still OPEN** (rate-limited, not verified). Deepest one;
>   needs server-side encounter state. Unchanged.
>
> The rest of this file is the original attacker's-eye record, kept for context.

---

**Status (original): authenticated, and partly server-authoritative — but the
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

### Already solid — credit where due
This API gets the things the recipe app didn't, and it's worth being explicit so
the migration doesn't accidentally regress them:
- **Authentication** — bearer tokens (`secrets.token_urlsafe(32)`), `werkzeug`
  password hashing, server-side sessions with expiry, and sessions deleted on ban.
- **Authorization** — a real role ladder; **`owner` is not storable** (it comes
  from the `ELUSION_OWNER` env var, so no request can grant it), with a DB `CHECK`
  as defence in depth and `can_act_on()` guarding staff actions.
- **Character level is server-owned** — proven above (claimed 99, stayed 1).
- **Loot acquisition is server-owned** — `POST /api/loot/take` writes
  `carry_items` itself rather than trusting the client.
- **Kill rewards are server-rolled** and rate-limited (E-3's one weakness is the
  event, not the payout).
- **`.gitignore`** uses prefix patterns (`*.db.*`) precisely because a plain
  `*.db` once let a `.db.before-…` backup slip into a commit — that lesson is
  captured in the file itself.

---

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

## Fix roadmap (the code already names it)

The inventory comment lays out the real sequence — the migration is half-built,
not unplanned:

1. **`debug=False`** for anything reachable by anyone but you → closes **E-4**. One line.
2. **Close the client-side item-granting paths** — shops, crafting, cooking, bank
   withdrawal, and the staff debug keys — so the server *writes* those items the
   way `POST /api/loot/take` already does. Once every legitimate gain flows
   through the server, the disagreements `_report_unexplained_gains()` logs are
   all dishonest.
3. **Flip that reporter from log to reject** → closes **E-1**. `PUT /inventory`
   stops being "store what I claim" and becomes "reconcile against what the
   server granted."
4. **Same shape for skills** — derive skill XP from server-observed events and
   make `PUT /skills` refuse increases the server didn't grant → closes **E-2**.
   *Cheap interim check:* reject any skill level that character level couldn't
   support — skill 99 on a level-1 character is a state legitimate play can't
   produce, so it's both an exploit and a ready-made detection signal.
5. **Throttle `login`** (attempt cap / backoff) → closes **E-5**.
6. **Verify the kill** (server-side encounter state, or at minimum tie kill
   reports to server-known enemy spawns) → closes **E-3**, the deepest one, best
   saved for last.

Re-run the three `curl` claims after each step. The target: rows 1 and 2 come
back `409`/`403` ("the server didn't grant that"), the way row 3 already refuses
today.
