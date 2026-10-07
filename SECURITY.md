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
| **Mod** | Kick, ban up to 30 days, mute up to a day, take a chat line down, read and close chat reports, read the staff user list, the moderation log and linked accounts, write staff-only notes and warnings | Banning at or above their own rank, banning permanently, granting a rank at or above their own, touching the economy — including creating any item, which since 6 Oct 2026 no rank below the owner can do (`test_security.py` E-1, `test_api.py` STAFF GRANTS) |
| **Dev** | Everything a mod can, plus permanent bans, longer mutes, teleporting a player, reading economy supply | Granting dev or owner, moving the whole server, minting gold, the metrics endpoint |
| **Owner** | Everything, incl. broadcast, maintenance, metrics, moving everyone, test fixtures on their own characters (gold, level, skill levels, any item), giving any player an item, and putting a player's character back to a snapshot — the only account that can create an item or undo a character | Being stored anywhere. `ELUSION_OWNER` is an environment variable, so no request writes it and no database backup carries it |
| **The server** | Owns level, XP, derived maxima, loot rolls and loot bag contents, and every backpack and bank cell; sole author of the gold and lusion ledgers | Knowing whether the client is honest, or whether the IP it sees is the player's. Both are assumed false |

One cell is deliberately weaker than it looks, and it is covered under
[Honest limits](#honest-limits): the server does **not** own kill *events*.

---

## Invariants

Every promise here is anchored. The first three are structural — they cannot
rot because there is nothing to change.

(This line used to give a count, and the count was wrong twice: "Ten" after
there were thirteen, then "Twenty-one" after there were twenty-three. That is
the small version of exactly the failure this page exists to avoid, so the
number is gone, by the same rule CLAUDE.md gives for check counts.)

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
5. **A backpack or a bank changes only when the server changes it.** Every
   change a player makes is its own request - a drag is a move, the bin is a
   discard, a pile of coins is cashed - carried out on the server's cells and
   answered with the grid, and refused (409, with the grid) when the game's
   picture was out of date. A player's whole-bag write is answered and ignored,
   so a fabricated bag writes nothing, and neither does a fabricated loss.
   → Held by: `test_security.py` — "but nothing in it is written", "a player's empty bag write destroys nothing"
   → and: `test_bagmoves.py` — "a move naming the wrong item is a 409", "onto the same stackable item it merges up to the stack limit", "THE LEDGER STILL BALANCES: every gold minted is gold somebody holds"
   → and in the game: `src/tools/testrunner.gd` — `_test_the_bag_is_the_servers()`
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
14. **A picture is served only to someone who could see it.**
    `GET /api/chat/image/<id>` asks the chat read's own question — could the
    caller read a line showing this picture? — and otherwise answers only an
    account that uploaded those bytes, or staff judging a report of it. A
    whisper, friends or guild line gets its own copy under a random id, so a
    private picture never shares an id with a world one, and a player who
    leaves the guild or the friendship stops being served what was posted
    there. Refused is the same 404 as missing. The id is still 256 bits (the
    SHA-256 of the bytes, for a world picture), so it cannot be guessed either.
    → Held by: `test_ownership.py` — "bob, holding the id, is NOT served it - no line he can read shows it", "THE WHISPER CARRIES ITS OWN COPY, under a new id", "AND STOPS BEING SERVED HER FRIENDS' PICTURE - the id he kept is not a key", "THE ID IS THE SHA-256 OF THE BYTES SERVED"
15. **Delete means revoke, in chat.** Taking a line down removes it from the
    screens that already have it within one poll (~3s), and drops the picture
    from the store entirely once no remaining line shows it.
    → Held by: `test_ownership.py` — "a delete is now a revocation, not a hide", "bob hears about it even though his cursor is past it"
    → and in the game: `src/tools/testrunner.gd` — `_test_chat_deletions_reach_the_client()`
16. **A client cannot heal itself.** Every rise in hp, mana or stamina is
    measured against what regeneration plus server-issued grants could have
    produced and trimmed to it. **Both** exits from the death screen are server
    routes — `/api/character/revive` and `/api/character/respawn` — so the
    refill after dying is authorised rather than asserted, and the carried gold
    a death destroys goes through the ledger like every other burn.
    → Held by: `test_security.py` — "1 hp to full with no potion is logged"
    → and: `test_economy.py` — "the respawn explains the rise instead of it being clamped", "the loss is in the ledger under its own reason"
    → and in the game: `src/tools/testrunner.gd` — `_test_death_reaches_the_server()`
17. **A staff note is read only by staff who could act on its subject.** Notes
    and warnings are opinions about a person, kept for the next member of staff,
    and they are read under the rule that guards IP addresses — `can_act_on()`,
    strictly above. A mod never reads what was written about another mod, nobody
    reads what was written about themselves, and no route a player can reach
    reads the table at all. The rule is in the query, not applied after it, so a
    page of the log is always a full page.
    The same reach guards an account's trade history on the staff desk: out
    of reach reads exactly like an account that does not exist.
    → Held by: `test_moderation.py` — "a note about a mod is not read by another mod", "the account view does not carry notes to a mod who cannot act", "no route the player can reach carries a note about them"
    → and: `test_trades.py` — "so is one above the mod's reach, indistinguishably"
18. **A name's colour proves nothing; rank is a badge.** Every player, staff
    included, chooses the hue their name is drawn in, so a player can pick the
    owner's gold. What marks staff is the crown and the MOD / DEV badge, drawn
    from the `role` the server sends - never from anything a player sets.
    → Held by: `test_namecolour.py` — "the owner is still the owner - rank did not ride on the colour"
    → and in the game: `src/tools/testrunner.gd` — `_test_staff_panel()`
19. **Accept agrees to the offer that was on the screen, and nothing else.**
    Every change to either side of a trade moves its revision, and
    `POST /api/trade/confirm` must name the revision the client drew. The write
    is conditioned on it, so an offer swapped a moment before the click refuses
    the accept rather than executing it - the other side cannot turn "accept
    the sword" into "accept the stick" by being faster than a poll.
    → Held by: `test_trades.py` — "an accept for an offer that has since changed is refused", "an accept that names no revision is refused"
    → and in the game: `src/tools/testrunner.gd` — `_test_trades_reach_the_right_people()`
20. **A bag the server changed is never overwritten by a copy from before.**
    A trade changes the bags of two players and only one of them asked. Both
    characters are flagged in the trade's own transaction; until the new bag
    has been delivered, a whole-bag save from that client is refused with the
    bag the server holds, so what a trade gave cannot be deleted by the
    receiver's next ordinary save. (A current game sends no bag at all -
    invariant 5 - so this now guards a build from before that.)
    → Held by: `test_trades.py` — "a whole-bag save built before the trade is refused", "the sword he received survives it"
    → and in the game: `src/tools/testrunner.gd` — `_test_trades_reach_the_right_people()`
21. **A staff password alone opens nothing.** Staff names are public - the crown,
    the MOD and DEV badges - so theirs are the passwords worth guessing. For a
    mod, dev or the owner with a confirmed recovery address, a correct password
    answers 202 and emails a six-digit code, and only the code gets a token. A
    wrong code counts toward the account lockout like a wrong password, and the
    streak clears only on a login that gets its token. **Once per computer,
    not once per login:** a login that got in with a code is given a device
    token, kept as a hash, and the same computer sending it back needs no code
    for 30 days - until the account's rank changes, or a password change, a
    recovery reset or "log out everywhere". It is checked only after the
    password and the ban.
    → Held by: `test_staffcode.py` — "a correct owner password answers 202, not 200", "a wrong code is 400 - never 401, which the game would read as a wrong password", "a lockout's worth of wrong codes freezes the account, even for the right code", "the same computer logs in again with no code", "a promotion asks for a code again, on the same computer", "one account's device token opens nothing for another account"
    → and in the game: `src/tools/testrunner.gd` — `_test_staff_logins_take_a_code()`
22. **One login at a time.** A login that gets its token - or a game reopening
    a remembered one, which swaps its token - ends every other session the
    account holds, so two games can never write the same bag, and a stolen
    token dies the next time the owner signs in. Resuming keeps the login's end
    date, so a remembered login cannot renew itself forever. A wrong password,
    a ban or a staff code still owed ends nothing.
    → Held by: `test_accounts.py` — "the earlier session is refused", "game A's bag, from before all that, is refused", "and the remembered one - which another copy of the game would also be holding - is refused, saying why", "the login still ends when it would have: resuming does not renew it", "and that half-login signs nobody out"
    → and in the game: `src/tools/testrunner.gd` — `_test_one_game_per_account()`
23. **A report is only for a line you were shown.** Reporting takes a message
    id, and ids are sequential, so a report route that accepted any id would
    tell you which ones are other people's whispers. The line is checked
    against the same rules the chat read uses - a whisper only for the two in
    it, a friends line only for the author's friends, a guild line only for
    that guild - and anything else is the same 404 as a line that does not
    exist.
    → Held by: `test_chatsafety.py` — "a whisper between two other people is a 404 - you were never shown it", "a friends-channel line is a 404 to somebody who is not the author's friend", "  but the one it was said to can report it"
    → and in the game: `src/tools/testrunner.gd` — `_test_chat_safety_menu()`
24. **Two requests sent together cannot both spend the same thing.** Every
    POST, PUT, PATCH and DELETE takes the database's write lock before its
    first read and keeps it until the request ends (THE WRITE LOCK in app.py).
    Before this, a route read a balance, worked out the new one and wrote it
    back, and two requests arriving together both read the old one. So a loot
    cell taken twice at once paid twice, one potion drunk five times explained
    five heals, one purse deposited twice put the gold in the bank twice, and
    kills sent together lost each other's XP. The only write routes left out are
    nine that hash a password or fetch a picture, and the suite names them.
    **The lock is SQLite's** (`BEGIN IMMEDIATE`). A move to another database
    has to keep this promise with row locks or serialisable transactions,
    because Postgres, like most databases, does not make every write wait its
    turn by default. This suite, run against the new database, is how to know it
    did.
    → Held by: `test_concurrency.py` — "one take paid and the rest found nothing", "one drink went through", "one deposit was paid and the other refused", "every XP point the answers promised is stored", "the only ones left out are the slow ones named here"
25. **Ignoring someone stops them reaching you, and never stops staff.** An
    ignore hides their lines from you, old ones included, and refuses their
    whispers, friend requests and trades to you. Staff and the owner cannot be
    ignored, so a warning always arrives.
    → Held by: `test_chatsafety.py` — "bob's whisper to ann is refused", "bob cannot ask ann to be friends", "bob cannot open a trade with ann", "staff cannot be ignored"
    → and in the game: `src/tools/testrunner.gd` — `_test_chat_safety_menu()`
26. **A mute silences chat, not play, and only within reach.** A muted player
    cannot speak in world chat, whispers or their guild, and still plays. A mod
    can mute for at most a day and never another mod or the owner; a player can
    mute nobody.
    → Held by: `test_chatsafety.py` — "he still plays - a mute is not a ban", "a mod cannot mute for more than a day", "a mod cannot mute another mod", "a player cannot mute anybody"
27. **A copy of the database holds no working login.** Passwords are scrypt
    hashes, and every token that signs someone in - sessions, trusted devices,
    recovery and staff codes - is stored only as a hash, so a leaked
    `elusion.db` or a backup logs nobody in. Sessions stored before this rule
    were hashed in place, and the players holding them stayed signed in.
    → Held by: `test_accounts.py` — "and no cell anywhere in the table is a live token", "what a leak would hand over does not log anyone in (401)"
    → and: `test_api.py` — "and the stored token was replaced by its SHA-256", "so the player signed in before the update is still signed in"
28. **A ban follows the computer, not only the connection.** The game keeps a
    random install id and sends it with every login, registration and resume;
    a new account is refused from a computer a live-banned account has used,
    whatever address it comes from. Like the address check it is registration
    only (the sibling on the family computer keeps playing), it ends with the
    ban, a crowded public computer blocks nothing, and the id is kept only as a
    hash that no route returns.
    → Held by: `test_security.py` — "a banned computer cannot register from a brand-new address", "the sibling on the family computer still logs in", "one banned account cannot stop a library computer making accounts", "the server keeps the SHA-256 of the id, not the id"
    → and in the game: `src/tools/testrunner.gd` — `_test_install_id_is_kept_and_sent()`
29. **Only the owner can put a character back, or put an item in somebody
    else's bag, and both are written down about the player.** The server keeps
    each character's last 20 snapshots - level, XP, purse, gear, pet, bag and
    skills, never the account's bank or lusions. A rollback restores one, moves
    the gold through the ledger so the supply still balances, takes a snapshot
    first so it can itself be undone, refuses a character in an open trade, and
    ends the player's sessions so their game reloads. A gift goes into the
    character they are playing, after a snapshot. Each is a line in the
    moderation log about the player. Everybody else gets the same bare 404 as a
    route that does not exist.
    → Held by: `test_rollback.py` — "nobody below the owner reads, restores or gives - the same bare 404 as no route at all", "the supply balances after a rollback", "the bank is the account's and untouched", "the snapshot taken before a rollback is returned", "a character in an open trade is a 409", "every rollback of carol is a line about carol, and no refusal is", "the gift is a 'give' line about lena, by the owner"
    → and in the game: `src/tools/testrunner.gd` — `_test_give_and_save_history()`
30. **The server keeps its own count of every monster's health, and writes down
    what does not fit** (0.10.0, E3_SCOPE.md option C, step 1). `presence.py`
    reads every leader's world and every player's hit - the leader's own
    included - into `combatbook.py`'s books: each monster's health counted down
    from the catalogue's maximum, each hit held to the biggest hit and the damage
    a second that character could deal with what it holds and its skills, each
    spawn held to the area's map and its respawn time, each death judged AGREED,
    SHORT or NOT DUE, one row per player who hit it (`combat_kills`), and every
    hit, walk or spawn that did not fit counted in `combat_flags`.
    `killwatch.py` matches every paid kill to the books. **Nothing in play
    changes yet**: the relay is untouched and a kill is paid as before - this is
    the week that measures whether honest play ever trips a check.
    → Held by: `test_combatbook.py` — "then it is judged: agreed", "a monster the leader kills with no hits is SHORT, on the leader, with all its health left", "a hit bigger than the character could land is booked at the most it could", "hits faster than the character's rate are refused past the burst it may land", "one back before the area's respawn", "three thousand malformed world messages raise nothing"
    → and: `test_presence.py` P-8, `test_killwatch.py` "books:"
    → and in the game: `src/tools/testrunner.gd` — `_test_shared_monsters_keep_the_books()`, `_test_combat_bounds_match_the_game()`

---

## Honest limits

No comforting lies. These are known, named and open.

- **Kill events are asserted, not proven** (E-3, the deepest one still open). The
  server rolls its own rewards, refuses reward-less enemies, rate-limits with a
  token bucket and caps kills at what the world's respawners can physically
  produce — that is a rate and content bound, not a proof. Since 0.10.0 the
  server **watches** the fight (invariant 30) and writes down every kill its
  own count does not back up, but it still **pays** on the game's word: refusing
  waits for a week of evidence that honest play never trips the books (step 2).
  Until then what a cheat mints is kept from spreading: the owner can switch
  trading off in one request, and a fresh mythic or Perfect find cannot be
  traded for 48 hours (`test_tradegates.py`). A game from before 0.10.0, or one
  whose presence link is down, is not seen by the books at all.
- **hp, mana and stamina are clamped, not verified** (E-9). Every rise is
  reconciled against what regeneration plus authorised potions could produce and
  trimmed past a 3× margin. A bound, not a proof.
- **A new computer defeats ban evasion** (E-11). A new account is refused from
  an address or a computer holding a live ban, and staff see linked accounts
  rated strong or weak. But the install id is whatever the game sends:
  deleting its file, a second browser for the web build, or a modified game
  that sends a fresh id each time gets past it. It stops the VPN, not someone
  who knows where to look - the staff link view is still what catches them.
- **A patched client can ignore a 401 and keep drawing the world.** What it
  cannot do is **save, trade, loot, or report a kill the server will accept** —
  all four are authenticated server-side. The heartbeat fixes the honest client
  that simply never found out it had been kicked.
- **A staff account with no confirmed recovery address logs in on its password
  alone**, and so does every staff account while the server cannot send mail or
  `ELUSION_STAFF_LOGIN_CODES` is off. There is nowhere to send a code, and
  refusing would lock the owner out of their own server. The login answer says
  so (`staff_unprotected`), the game tells that player once, and the boot log
  says it for the server.
- **A staff member's own computer is trusted for 30 days once it has proved
  itself.** Somebody holding both the password and that computer (or the
  game's device file from it) logs in with no code. That is the trade for not
  asking on every login; a password change, a reset or "log out everywhere"
  withdraws the trust from every computer at once.
- **Per-account lockout reveals that a locked username exists** (E-5). Only a
  real row can be locked. That is the standard, accepted trade for per-account
  lockout and it is written down rather than pretended away.
- **`/register` answers "does this name exist" outright**, with 409 against 201,
  because a signup form has to say when a name is taken. `REGISTER_MAX_CONFLICTS`
  stops it being an unlimited oracle; it does not stop it being an oracle.
- **A picture lasts as long as a line showing it**, which is the chat's own 24
  hours, not until the store needs the room. And anyone who was shown it can
  keep a copy: deleting the line takes it off screens and off the server
  (invariant 15), not off somebody's disk.

- **Where somebody stands is whatever their game says.** The presence socket
  (`presence.py`) relays positions as games send them, so a modified game can
  stand inside a wall or hop across the area on other people's screens. Since
  0.10.0 the books write down a walk faster than the character can and a step
  the length of a teleport, and a hit from beyond any screen, but refuse none
  of them. It
  cannot appear as somebody else, or show a rank, colour, guild or pet it does
  not hold - who a player is comes from the server's own rows, through a
  ticket tied to their login - and nothing over the socket deals damage or
  moves an item. Messages are size- and rate-limited, and an ended login ends
  the connection within seconds.
- **The monsters in a shared area are whatever the area's leader says.** Since
  0.7.0 one game - the area's leader, the sharing game that walked in first -
  runs the monsters for everyone there, and `presence.py` passes its messages
  on exactly as sent. A modified game that becomes leader can move, heal or
  kill those monsters on everybody's screen, or set the game's own monsters on
  somebody. It cannot reach anyone's health, bag, gold or kills: every game
  still builds each attack from its own copy of the monster, caps the few
  numbers the leader sends, and reports its own kills under the same E-3
  ceilings as before. Since 0.10.0 the server also reads what the leader says
  into its books (invariant 30), so a monster killed without the hits to kill
  it, or one the map does not hold, is written down against that leader - but
  the books do not check where a leader says a monster stands.

- **A rollback can make a second copy of an item.** It puts back what the
  character held when the snapshot was taken; an item traded, banked or sold
  since then is still wherever it went, too. That is why only the owner can do
  it, why each one is a line on the player's record, and why the Save history
  window says so under the buttons. The gold cannot be copied this way - it moves
  through the ledger like any other mint or burn - but, like every burn, gold a
  rollback takes away is counted on the Kingdom board as given.

Anything not on this list that later turns out to be true belongs on it.

---

## Where the detail lives

| For | Read |
|---|---|
| Threat model, and every finding E-1…E-21 with what happened to it | [`SECURITY_NOTES.md`](SECURITY_NOTES.md) |
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
