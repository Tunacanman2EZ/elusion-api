# What actually gets harder after launch

Written against the premise "once I buy the server everything becomes harder to
upgrade, so perfect the systems first." That premise is right about a short list
and wrong about most of the list, and the difference is worth being exact about,
because perfecting the reversible things is the expensive version of this plan.

Checked against the code on 2026-09-28.

---

## The part that is backwards

**Server code is the *easiest* thing to change after launch, not the hardest.**
It is a Flask app on a box you own. A fix is `git pull` and a restart, and after
deploying you will have a pipeline for that, which is more than exists today.
Routes, rules, thresholds, rewards, refusals, rate limits — all of that stays as
changeable as it is right now.

What actually becomes hard is a much shorter list, and almost none of it is on
the server:

1. **Code that is not on your machine.** The client, once somebody has
   downloaded it.
2. **Data you did not record.** You can add a column tomorrow. You cannot
   backfill the history that column would have held.
3. **Things players already have.** A level 40 character with banked gold is a
   promise. Rebalancing after that is taking something away from somebody.
4. **Identity.** Usernames, the owner model, how an account is proved.

Everything else is a Tuesday.

---

## The doors, in the order they will hurt

### D1 — You cannot tell an old client from a new one · **closed: built, shipped disarmed**

**Built.** Every request carries `X-Elusion-Build` (`api.gd`, matching
`CLIENT_BUILD_HEADER` in `app.py`). The server compares it with
`min_client_build`, one row in `server_settings`, and refuses an older build with
**426 Upgrade Required**, naming the minimum. `/api/status` reports the minimum
too, so the login screen can say so before anyone tries. The owner raises it with
`POST /api/server/minbuild`.

**It ships at 0, which disarms it.** A number typed wrong months before launch
must not be able to lock out the first cohort; the mechanism is what could not be
added later, and switching it on is one owner request. Raise it on the first
breaking change.

What follows is the case as it was made before the header existed, kept because
it is still the reason the gate matters.

**There was no version handshake anywhere.** The client sends nothing that
identifies its build, the server never asks, and no route can refuse one. The
only `User-Agent` in the whole codebase is on an outbound chat image relay. So:

- The server cannot refuse a build with a known bug in it.
- The server cannot tell a player they need to update.
- A breaking wire change does not fail — it *half-works*, silently, on every
  build already out there, which is worse.

**You already have this idea and applied it one layer down.** `/api/status`
returns `gamedata_schema`, so the *data contract* is versioned and a mismatch is
detectable. The client *build* is not.

**Why it is a door rather than a task.** You can add the header later and treat
its absence as "pre-versioning", which works — but every build shipped before
that lands is permanently in the unknown bucket, and it is the bucket you will
most want to reason about, because it is the oldest code.

**Cost now:** roughly an hour. A version constant in the client, a header on
every request, a `MIN_CLIENT_VERSION` on the server, a refusal (426 Upgrade
Required is the code that means exactly this), and the minimum echoed on
`/api/status` so the login screen can say so before anyone tries.

**Cost later:** the same hour, plus a permanent blind spot over the first cohort.

### D2 — How a player gets an update at all · **a decision, not code**

D1 lets the server *say* "update". It does not make one reachable. If the
distribution is a file somebody downloaded, then every breaking change is a
support conversation, forever, with people who may not read it.

This is not something to build; it is something to decide before the first build
leaves, because it determines how brave you can be afterwards. An auto-updating
launcher, a storefront that patches (itch.io's app does), or a hard rule that
the wire never breaks — any of those is fine. Not having picked one is what
costs.

### D3 — Data you are not recording · **partly open, and the cheap half is cheap**

Adding a column later is easy. Reconstructing what it would have held is
impossible.

- **Positions.** `saves` holds no coordinates at all — not stale ones, none.
  This is step 1 of both the world boss and the real E-3 fix. You do not need
  the *feature* before launch. Recording the column and having the heartbeat
  carry it means that when you build the feature you also have a history of how
  people actually move, which is the difference between choosing a range and
  guessing one.
- **`kill_reports` already does this right**, and is the model: it was written
  as groundwork *before* any rule used it, precisely so the rule could be picked
  from data instead of invented. `SECURITY_NOTES.md` says why, in the words that
  matter most here: *"a threshold picked without data is how honest players get
  clamped."*

**Cost now:** the column pair and the heartbeat field, with nothing reading them
yet. Small, and it is honest — an unused column is not a lie, where an unused
*feature* would be.

### D4 — Numbers players have already banked · **inherent, manage it**

Drop rates, XP curve, gold sinks, the revive price. Every one of these is
trivially editable on the server forever, and every one becomes *socially*
expensive the moment somebody has earned against it. Nerfing a rate is taking
something away from the people who played most.

There is no fix, only a posture: expect the first cohort's economy to be wrong,
and say so to them in advance. A wipe you warned about is a fresh start. A wipe
you did not is a betrayal.

### D5 — Identity · **already closed, on purpose**

Worth naming because it is the classic one and you have already shut it:
`username` is `COLLATE NOCASE` so `Tunacan` and `tunacan` cannot both exist;
`ELUSION_OWNER` lives in the environment and is not storable in `role`, so no
database write grants the top rank. Both are the kind of thing that is
unfixable after a hundred accounts exist, and both are done.

---

## What is NOT a door, and should not be "perfected" now

**E-3.** The architecture change is exactly the same size before launch and
after. What is different afterwards is that you will have watched real people
play, which makes the design *better*, not harder — and this project has already
written down what happens when a threshold is chosen without that (the E-2
interim skill bound, which "did not survive contact with the client"). Building
server-observed combat against imagined play is how you ship a bound that
clamps honest players. `E3_SCOPE.md` has the argument in full; nothing in it
changes here.

**Any server-side rule.** Rates, ceilings, refusal codes, rewards, the trade
switch. All of it is a deploy away, forever.

**Anything behind a switch.** Maintenance and PvP already prove the pattern, and
it is the right pattern precisely because it converts a code change into a
request.

**The UI.** It ships in the client, so it is bounded by D1 and D2 — but it is
not a *door*: a worse-looking panel is not an irreversible state, it is just
worse until the next build.

---

## What that means for the last phase

In order, and short on purpose:

1. ~~**The version handshake (D1).**~~ Done — `X-Elusion-Build`, the 426
   refusal and `/api/server/minbuild` are built and ship disarmed at 0.
2. **Decide the update path (D2).** No code. Decide it before the build.
3. **Record positions (D3).** Column and heartbeat only, nothing reading them.
4. **Say the economy may be reset (D4).** One sentence, to the first cohort,
   before they earn anything.
5. **The trade switch,** from `E3_SCOPE.md` — not a door, but it is the thing
   you will want in one request rather than in a deploy.

Then buy the server. Everything after that is still a Tuesday.
