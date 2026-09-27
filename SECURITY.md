# Security model — Elusion RPG

One page. Every claim below is either a structural truth or names the test that
holds it, and the test is the authority:

> **If this page and the tests disagree, the page is wrong. Trust the tests.**

`test_security_doc.py` enforces that literally — it reads this file, and fails if
a suite named here does not exist or a check quoted here is not in it.

---

## The premise

**The player's machine is hostile. The client is a claim, not a source of truth.**

The attacker is not a stranger at the port; that door is shut. It is a
**logged-in player running a modified client** — a real account, a valid token,
both obtained legitimately. The question is never "can somebody get in", it is
**"how much can a real player lie to the server about what happened in the
game."** Anything the client computes, the player can forge. The only facts that
are true are the ones the server establishes or checks, and security is the list
of which claims it verifies.

---

## Actors and trust

Role definitions, the ladder and how the owner is named: [`CLAUDE.md` → Ranks](CLAUDE.md#ranks).

| Actor | Can, by design | Is **not** trusted with |
|---|---|---|
| **Anonymous** | `GET /api/status`, register, log in | Any state at all. Three routes, and `/api/status` exists because a login screen has to ask "are you there?" before anyone has logged in |
| **Logged-in player** | Move, chat, save, and *request* loot, trades, purchases, revives, cooking, fishing — each as a claim the server re-derives | Their own level, XP, stat maxima, loot rolls, gold totals, lusion totals, any of the six skills, or any row that is not theirs |
| **Mod** | Kick, ban up to 30 days, take a chat line down, read the staff user list and linked accounts | Banning at or above their own rank, banning permanently, granting a rank at or above their own, touching the economy |
| **Dev** | Everything a mod can, plus permanent bans, teleporting a player, reading economy supply | Granting dev or owner, moving the whole server, minting gold, the metrics endpoint |
| **Owner** | Everything, incl. broadcast, maintenance, metrics, moving everyone | Being stored anywhere. `ELUSION_OWNER` is an environment variable, so no request writes it and no database backup carries it |
| **The server** | Owns level, XP, derived maxima, loot rolls and loot bag contents; sole author of the gold and lusion ledgers | Knowing whether the client is honest, or whether the IP it sees is the player's. Both are assumed false |

Two cells are deliberately weaker than they look, and both are covered under
[Honest limits](#honest-limits): the server does **not** own kill *events*, and
the backpack ledger is still whatever the client pushes.

---

## Invariants

Ten promises, each anchored. The first three are structural — they cannot rot
because there is nothing to change.

**Structural**

1. **The client IP is untrusted.** It is used for throttling and ban-evasion
   hints only, never for identity or authorisation, and `ProxyFix` means it is
   whatever a proxy said it was.
2. **`Api.role` on the client hides buttons; it never refuses anything.** It is
   client memory set from a login response, so a patched build sets it to
   `owner` and gains nothing. `require_role()` and `require_owner()` are what
   refuse.
3. **The owner is not a row.** There is no write that produces the top rank, and
   `role_for()` consults the environment before the column, so a `users.role`
   column edited by hand to `'owner'` grants nothing.
   → Held by: `test_revocation.py` — "owner cannot be granted through the API"

**Held by a test**

4. **Gold and lusions are server-owned.** Balances change only from events the
   server itself recorded; a client-sent total is dropped, and the ledger is held
   to its invariant so nothing is minted by a purchase or a trade.
   → Held by: `test_economy.py` — "injected gold is reported as drift", "nothing was minted by a purchase"
5. **A fabricated backpack or bank is trimmed to nothing.** The claim is accepted
   as a *request* — a 400 would break every honest save — and reconciled against
   what the server actually granted.
   → Held by: `test_security.py` — "but the fabricated items are trimmed to nothing"
6. **All six skills are server-owned.** `PUT /api/character/skills` drops every
   skill name it accepts, so a client claim earns nothing.
   → Held by: `test_gathering.py` — "every skill the route accepts is a skill it drops"
7. **A ban ends every session, on every device, at once.** The sessions are
   deleted in the same transaction that sets the ban, and the ban is checked
   again on every request in case one survives.
   → Held by: `test_revocation.py` — "every device is refused on its next request"
8. **A demotion needs no revocation.** The rank is a join, not a claim in the
   token, so one UPDATE is effective on the demoted account's very next request
   with no session destroyed.
   → Held by: `test_revocation.py` — "the SAME token now reports mod, on the very next request"
9. **The rank ladder is hidden: a staff route answers 404, never 403.** A 403
   confirms the route exists and that you are not allowed to use it. Every
   `/api/staff/` route must carry a rank decorator, so the one added next month
   fails the suite rather than quietly working for everybody.
   → Held by: `test_refusals.py` — "every /api/staff/ route carries a rank decorator"
10. **A 401 is not a verdict on the session.** Changing a password answers 401
    for a mistyped *current* password while the token stays live, so a client
    that signs people out on any 401 signs them out for typos.
    → Held by: `test_refusals.py` — "...and the token is still served afterwards"
11. **The login 401 enumerates nothing.** An unknown username and a wrong
    password return byte-identical bodies, and both pay the same scrypt cost so a
    stopwatch cannot tell them apart either.
    → Held by: `test_refusals.py` — "and the two answers are identical, body and all"
12. **No route lets you name somebody else's row.** Every id a client may send is
    scoped inside the query, so "not yours" and "does not exist" are the same
    404; and for every table carrying a `user_id`, every `UPDATE` and `DELETE`
    must scope on it or sit on a short allowlist with its reason written down.
    → Held by: `test_ownership.py` — "bob reading alice's bag by id is refused", "every write to a per-user table names the owner, or is on the allowlist"
13. **The highest-value rows accept no id at all.** Not one trade route takes a
    `trade_id`; every one resolves the trade from the caller. An ownership bug
    needs an id to tamper with.
    → Held by: `test_ownership.py` — "no trade route accepts a trade_id from the client"
14. **A picture id is a capability, not a row number.** `GET /api/chat/image/<id>`
    has no ownership check, and that is sound only because the id is the SHA-256
    of the bytes — 256 unguessable bits *are* the permission. If the ids ever
    become sequential or the hash is truncated, this route becomes a textbook
    IDOR without a line of it changing, so the properties of the id are the test.
    → Held by: `test_ownership.py` — "THE ID IS THE SHA-256 OF THE BYTES SERVED"
15. **Delete means revoke, in chat.** Taking a line down removes it from the
    screens that already have it within one poll (~3s), and drops the picture
    from the store entirely once no remaining line shows it.
    → Held by: `test_ownership.py` — "a delete is now a revocation, not a hide", "bob hears about it even though his cursor is past it"
    → and in the game: `src/tools/testrunner.gd` — `_test_chat_deletions_reach_the_client()`

---

## Honest limits

No comforting lies. These are known, named and open.

- **The backpack ledger is still client-declared.** `POST /api/loot/take` closed
  where items *come from*; the bag they land in is whatever the client pushes on
  save, reconciled against server grants rather than derived from them.
- **Kill events are asserted, not proven** (E-3, the deepest one still open). The
  server rolls its own rewards, refuses reward-less enemies, rate-limits with a
  token bucket and caps kills at what the world's respawners can physically
  produce — that is a rate and content bound, not a proof. Closing it needs
  server-side encounter state.
- **hp, mana and stamina are clamped, not verified** (E-9). Every rise is
  reconciled against what regeneration plus authorised potions could produce and
  trimmed past a 3× margin. A bound, not a proof.
- **A VPN defeats ban evasion** (E-11). Registration is refused from an address
  holding a live ban, and staff see linked accounts rated strong or weak. A new
  address is a new person as far as this server can tell.
- **A patched client can ignore a 401 and keep drawing the world.** What it
  cannot do is **save, trade, loot, or report a kill the server will accept** —
  all four are authenticated server-side. The heartbeat fixes the honest client
  that simply never found out it had been kicked.
- **Per-account lockout reveals that a locked username exists** (E-5). Only a
  real row can be locked. That is the standard, accepted trade for per-account
  lockout and it is written down rather than pretended away.
- **`/register` answers "does this name exist" outright**, with 409 against 201,
  because a signup form has to say when a name is taken. `REGISTER_MAX_CONFLICTS`
  stops it being an unlimited oracle; it does not stop it being an oracle.
- **A whispered picture is only as private as its bytes are rare.** The image
  store deduplicates on the content hash, so the same image posted in world chat
  is the same row and the same id. Inherent to content addressing, and the right
  trade for a game chat.
- **Anyone holding a picture id may fetch it, for as long as the row exists.**
  That is what a capability means; see invariant 14 for why the id cannot be
  guessed, and invariant 15 for the one way it is taken back.

Anything not on this list that later turns out to be true belongs on it.

---

## Where the detail lives

| For | Read |
|---|---|
| Threat model, and every finding E-1…E-15 with what happened to it | [`SECURITY_NOTES.md`](SECURITY_NOTES.md) |
| Who owns which field, and the exact request/response of every route | [`docs/apicontract.md`](../../Elusion_RPG/docs/apicontract.md) *(game repo)* |
| TLS, the proxy setting, secrets, backups, monitoring, "before the first stranger connects" | [`DEPLOY.md`](DEPLOY.md) |
| Ranks, conventions, and the traps that cost a day each | [`CLAUDE.md`](CLAUDE.md) |

Secrets, for the avoidance of doubt: `.env` and `elusion.db` are never committed,
`ELUSION_OWNER` lives only in the environment, and no password or token is ever
written to a log.

---

## Reporting a problem

**Something exploitable — please do not open a public issue.** Use GitHub's
private vulnerability reporting: the **Security** tab on this repository →
*Report a vulnerability*. That reaches the maintainer privately and nothing is
visible to anyone else until it is fixed.

Ordinary bugs, questions and anything already public: a normal issue is perfect.

No inbox is published here on purpose. A documented security address is a
documented spam target, and a private report should land somewhere with a record
attached to it rather than in a mailbox.
