# The shape of the server, and where it is going

16 Oct. The owner, after a review put architecture at 7 of 10: "lets bump
this up Architecture" - and, asked where to start, "choose the best
professional choice". This is that choice, written down so every batch after
it follows the same road.

## Where it stands

`app.py` was about 20,000 lines when this was written (16 Oct), and is about
11,400 with the carry, who-is-around, the switches and the kill out (18 Oct): the shared core (config, the database and
its write lock, sessions and tokens, the auth decorators, rate limiting,
request timing) and the areas not yet moved, one after another. It works, and every
area is covered by its own suite, but one file that size has a cost that grows:
a change to one area means reading around all the others, and nothing stops
one area reaching into another's tables.

Beside it, **feature packages**: a folder per area, each in three layers.

| Package | Since | How it got there |
|---|---|---|
| `playerstats/` | game 0.47.0 | built outside app.py |
| `events/` | game 0.48.0 | built outside app.py |
| `insights/` | game 0.50.0 | built outside app.py |
| `killrecord/` | 16 Oct | **moved out** of app.py |
| `friends/` | 16 Oct | **moved out** of app.py |
| `gathering/` | 18 Oct | **moved out** of app.py: fishing and cooking |
| `loot/` | 18 Oct | **moved out** of app.py: the loot bag |
| `guilds/` | 18 Oct | **moved out** of app.py: guilds, staff's two guild routes too |
| `parties/` | 18 Oct | **moved out** of app.py: parties and party voice |
| `chat/` | 18 Oct | **moved out** of app.py: chat, pictures, the ignore list |
| `trade/` | 18 Oct | **moved out** of app.py: player trade and the kingdom tax |
| `kingdom/` | 18 Oct | **moved out** of app.py: the Kingdom Tax board |
| `world/` | 18 Oct | **moved out** of app.py: the stirring lands |
| `moderation/` | 18 Oct | **moved out** of app.py: sanctions, reports, mutes |
| `staffdesk/` | 18 Oct | **moved out** of app.py: the staff list and the record |
| `equipment/` | 18 Oct | **moved out** of app.py: wearing gear |
| `consuming/` | 18 Oct | **moved out** of app.py: using a consumable |
| `death/` | 18 Oct | **moved out** of app.py: reviving and respawning |
| `bank/` | 18 Oct | **moved out** of app.py: the bank and the account |
| `carry/` | 18 Oct | **moved out** of app.py: the backpack, and the item grants |
| `skills/` | 18 Oct | **moved out** of app.py: the skills write, training, the XP grant |
| `saves/` | 18 Oct | **moved out** of app.py: a character's slot, its read, deleting it |
| `status/` | 18 Oct | **moved out** of app.py: the live stats and the healing check |
| `shop/` | 18 Oct | **moved out** of app.py: buying, selling, the shelf |
| `players/` | 18 Oct | **moved out** of app.py: who is online and nearby, the presence ticket |
| `switches/` | 18 Oct | **moved out** of app.py: the server's switches, `/api/status`, the owner's announcement |
| `kill/` | 18 Oct | **moved out** of app.py: a kill reported and paid for, and its two records |

## Where it is going

`app.py` as the **composition root**: the core, and one `register()` call per
area, handing each package what it needs. Every area in a package of its own.

## The package contract (BE105)

- **Three layers.** `routes.py` is the edge (request in, answer out),
  `services.py` the rules (pure: no database, no request), `data.py` the SQL.
- **It never imports app.py.** `register(app, *, ...)` is handed the app, the
  decorators (`require_auth`, `require_owner`, ...), `get_db` and whatever else
  it needs, by keyword. That is what lets `test_packages.py` run every package
  on a bare Flask app with stand-ins - and two such apps side by side.
- **What other areas share stays in app.py and is handed in**: who is online
  (`_presence_for()`), how a person is shown (`friend_entry()`), the limits
  that are this file's tunables (asked through a callable, so a suite can turn
  them down).
- **What other areas ask of a package, register() can hand back.** chat/
  returns post_chat() and can_read_row() (the events post lines, a report
  asks whether its reporter could read the line), and app.py keeps them
  callable under the names it always used.
- **A seam a suite replaces is handed as a lookup.** Where a suite swaps or
  slows one of app.py's names (`_turn_fetch`, `gold_item_value`), the package
  is handed `lambda ...: name(...)`, so the swap is heard on every call. A
  function handed in once is one the suite never touches - which is how
  test_concurrency's hold stopped reaching the loot take, still passing, until
  it was found on 18 Oct.
- **A module handed in whole is a new name for it.** trade/ asks five things of
  the catalogue (what an item is, its worth, the mythic tier, the tax, whether
  it exists) and is handed gamedata itself, as `catalogue`, looked up on every
  call. A suite that holds a rule by searching for `gamedata.` must search
  for `catalogue.` too - test_quality's Q-7 does, since the move.
- **A moved route keeps its endpoint name** (`add_url_rule(..., endpoint=...)`)
  - rate limiting, request timing and `/api/staff/powers` key on it - **and its
  docstring**, which is its page in `/apidocs`.
- **Tables stay in `init_db()` for now.** A package's `data.py` reads and
  writes them; moving each table's `CREATE` into its package comes after the
  areas are out, in one batch, when the order of creation can be checked once.

## The guards

Nothing a player or the game can see changes when an area moves. That is
checked, not hoped:

- **`routes.lock`, held by `test_routes.py`.** One line per route and method:
  path, endpoint name, whether it needs a token, the lowest rank it lets in,
  whether it asks for the password again, whether it takes the write lock,
  whether it has a docstring. A move that drops, renames or loosens anything is
  a difference, line by line - and every route the lock says needs a token is
  asked without one and must answer 401. A change that is meant is written
  with `python test_routes.py --write`, and the lock's diff in the commit says
  what changed for the game.
- **`test_packages.py`**: every package on a bare app, and the list of them is
  the code map's rule (a folder app.py imports, with a `routes.py`) - a package
  added without a bare-app run of its own fails it.
- **`test_deploy.py`** copies every package it finds by the same rule, so a new
  one cannot be forgotten in the deployed copy.
- **The area's own suite**, unchanged: it still drives the real app.py.

## Moving an area out - the checklist

1. Read the area and everything that calls into it (`grep` its functions and
   its tables across app.py and the suites). **Look for suites that read
   app.py's SOURCE** for a rule (every kind logged is a listed kind, nothing
   picks a save by recency): code moved out of app.py is code they stop
   seeing. Each must read the packages too - found the code map's way - and
   a sabotage in the package must fail it. Guilds needed three; two more -
   the per-user write scan (test_ownership) and the item-lookup rule
   (test_quality) - were found a move late, with parties (18 Oct). **And
   look for suites that replace one of app.py's names** (`app_module.x = `,
   `held_open(app_module, ...)`): see the seam rule above.
2. Make the package: `__init__.py` (what it is, its routes), `routes.py`,
   `services.py`, `data.py`.
3. Move the routes **as they are** - docstrings, messages, status codes - and
   the helpers only they use. What other areas also use stays and is handed in.
4. In app.py, at the area's old banner: the import and the `register()` call,
   with a line saying where the code went.
5. Run `test_routes.py` (no difference), the area's suite, `test_packages.py`
   (add the package's bare-app checks), then every suite; re-run `codemap.py`
   (CODEMAP.md is generated, never edited).
6. **`git add` the new folder** - the server updates by `git pull`, and a
   package left out of the commit is an import error on the next restart.

## The order

Least tangled first, so each move is small and the pattern is proven before the
areas that hold money and items:

1. ~~the kill record~~, ~~friends~~ (16 Oct)
2. ~~fishing and cooking (`gathering/`)~~ (18 Oct). The bait, the rod and the
   cast bucket went with them. **The grants they pay with stayed in app.py and
   are handed in** - `_grant_skill_xp()`, `_add_to_backpack()`,
   `_take_from_backpack()` - because the loot bag, the shop, trades, the bank
   and gifts pay with the same three. They went with the carry (10, 18 Oct):
   the item grants are carry/'s, the XP grant skills/'s, still handed in by
   those names.
3. ~~loot bags~~ (18 Oct): both routes, and every line of SQL on the two
   tables but character delete's. A new bag was still made in app.py, as
   the kill's last step, until the kill moved (18 Oct): it is loot/'s now,
   `create_bag()`, handed back from register() and handed to kill/. The
   money and the backpack the take pays into are handed in.
4. ~~guilds~~ (18 Oct): all ten routes - `/api/staff/guild` and
   `/api/guild/list` with them, since they write and read nothing but guilds -
   and every write to the four tables. Other areas still READ the tables in
   app.py (a chat line's guild, the poll's invitations, the players list, the
   kingdom board), and ask through `guild_membership()` and `guild_tag()`,
   which are the package's `membership()` and `guild_tag()` under their old
   names. Guild chat stays with chat (6).
5. ~~parties and party voice~~ (18 Oct): all seven routes and every write to
   the three party tables. The TURN key (`voice_settings()`) and the call to
   Cloudflare (`_turn_fetch()`) stay in app.py and are handed in - a server
   setting and the server's one outside call, which test_voice replaces - and
   so do the credentials handed out, kept per process. Party chat stays with
   chat (6), the poll's view of a party (`_own_party()`) with the poll.
6. ~~chat: world chat, channels, pictures, the ignore list's routes~~ (18
   Oct): ten routes on nine endpoints, the rules (length, retention, the flood
   bucket, the channels, every picture cap) and every write to the chat
   tables. **register() hands back what other areas ask of chat** -
   `post_chat()`, `can_read_row()` for a report - and app.py keeps them under
   their old names; `ignores()` is the package's data. What stays in app.py:
   the broadcasts (the server's voice), `clean_player_text()` (every typed
   name), reports and mutes (moderation, 9), and the picture fetch (the
   server's call out, which test_chatrooms points at a local server).
7. ~~trade and the kingdom tax~~ (18 Oct), as two packages: `trade/` - all
   seven routes, the execution, and every write to the three trade tables -
   and `kingdom/`, the board that shows what the tax and every other burn
   took, with its cache, which is per register() now rather than a module
   global (test_packages runs two apps and asks). **What stays in app.py**:
   the grants the execution pays with (the carry, 10) - handed in as lambdas,
   since they are defined further down the file than the trade's register() -
   `gold_delta()`, the owner's switch, which character an account is playing
   (`PLAYING_SLOT_SQL`, shared with the nearby list and parties), and the
   resync flag both clients re-read their bags by, which an owner's gift rides
   too. A rollback, a character delete and the poll still find an open trade
   by app.py's old names, which are the package's.
8. ~~the stirring lands (`world/`)~~ (18 Oct): the owner's three routes, the
   settings and the stir itself - which land, when the next begins, what a
   kill of a stirring land's monster pays. **register() hands back what four
   areas ask of the world**: the kill route what a kill pays, the poll what
   the game is told, the events their guardrails, the insights the next
   stir - and app.py keeps them under their old names. **And it is handed
   the events and the insights as lookups** (`lambda: world_events`): both
   are registered at the end of app.py, after the world, and are handed what
   it hands back, so each is asked for when it is wanted - test_packages
   hands one in after the world is registered and checks it is heard. What
   stays in app.py: the server_settings store, the broadcasts, the kill
   route and the poll themselves.
9. moderation and the staff list, in two moves.
   ~~The sanctions, the reports and the mutes (`moderation/`)~~ (18 Oct): ten
   routes - ban, kick, unban, a rank, mute, unmute, both reports and the
   Reports tab - and every write to `chat_reports`. **What every staff area
   shares stays in app.py and is handed in**: the rank ladder
   (`can_act_on()`, `role_for()`), `_moderation_target()` and the moderation
   log's writer, `log_staff_action()` - which test_moderation's M-8 finds in
   the package by that name, so it is handed in under it. So do the three
   tunables a suite turns down (`REPORTS_PER_HOUR`, `REPORT_LINES_PER_PLAYER`,
   `MUTE_MAX_MINUTES_MOD`, which the staff powers page quotes too).
   ~~The staff list and the moderation record (`staffdesk/`)~~ (18 Oct):
   `/api/staff/users`, `actions`, `note` and `user/<username>`. It hands back
   the two query builders, which app.py keeps under their old names because
   test_moderation's M-9 asks SQLite for the plan of exactly what the routes
   run. role_for() as SQL (`_rank_level_sql()`) stays with the ladder, and
   test_moderation's M-8 learned to count each logged call once, for the
   function it is written in - a package's routes are functions inside its
   register(), and the note route's computed kind came back twice.
   `/api/staff/powers` stays in app.py: it reads every route on the server and
   quotes limits from every area, so it is the composition root's own page.
10. the carry: saves, character, bank, equipment, consuming, reviving - in
    several moves, the smallest first.
    ~~Equipment and consuming (`equipment/`, `consuming/`)~~ (18 Oct): equip,
    unequip and consume, and every write to `consume_grants`. Both move the
    item through the backpack grants, which stay in app.py for now, handed in
    as lambdas (they are further down the file). `_derived_stats()` - the one
    way to ask a character's maxima - stays and is handed in, and
    test_gearbonus now reads the packages for a bare `max_stats_for(`, by
    either name.
    ~~Reviving and respawning (`death/`)~~ (18 Oct): the death screen's two
    exits. Every coin and lusion they cost goes through app.py's ledgers,
    handed in; the account row is handed in as a lookup, because
    test_concurrency slows `_ensure_account()`.
    ~~The bank (`bank/`)~~ (18 Oct): `/api/account`, the bank's whole-array
    write and the lusion read-back, a transfer of items and of gold, a move
    and a discard - seven routes - and how a deposit is packed. **Registered
    further down app.py than its old banner**, after the carry's sizes and
    helpers it is handed. What it shares with the backpack - taking from cells,
    a move, a discard - stays in app.py, handed in by name, and goes with the
    carry.
    ~~The carry and the skills (`carry/`, `skills/`)~~ (18 Oct): the
    backpack's four routes - the owner's whole-carry write, a move, a
    discard, cashing a pile - and the skills' two, the write and the training
    report; and with them **the grants every other area pays with**: an item
    into the bag and out of it, whether an account holds one, the XP grant,
    and the grid steps the bank and the shop's sale share. **Registered before
    every area that is handed them** (THE CARRY AND THE SKILLS, ahead of the
    bank), and both hand everything back; app.py keeps each under its old
    name, so the shop, the kill, trades, loot and gathering are handed what
    they always were. What they need from further down the file - the
    trade-resync flag, the lusion pile's id, the account view - is handed in
    as a lookup, since the name does not exist yet when register() runs.
    test_gifts' "one function" check reads the cash route wherever it lives,
    and that app.py hands it `money_item_worth` by name.
    ~~The character itself (`saves/`)~~ (18 Oct): GET and PUT `/api/save`,
    the one-call character read and deleting a character - four routes, and
    every write to `character_deletions`. **Registered further down app.py
    than its old banner**, after everything it is handed (the carry, the
    skills, the trade lookup, the save history), so nothing is a lookup.
    What every route shares stays at SAVES: the slot parser, the 400, a cell,
    the JSON-column reader and `_derived_stats()`.
    ~~The status read and write (`status/`)~~ (18 Oct): GET and PUT
    `/api/player/status` and the check that every rise in a pool was earned,
    with what a status write may name, what the server owns and what it
    derives. **The healing tunables stay in app.py, handed in as lookups**
    (`lambda: HEAL_CLAMP_ENFORCED` and the margins): they are switches with
    their history written beside them, and a copy handed in once would be a
    second switch the first no longer moves - checked by flipping app.py's on
    the real app, and app.config's on the bare one. register() hands back
    `status_of()`, kept as `status_payload()` for every area that answers with
    the vitals.
    ~~The shop (`shop/`)~~ (18 Oct): buying, selling and the shelf - three
    routes. It writes no table of its own: the bag through the carry's grant
    and one-cell steps, handed in by the names app.py keeps for them, the gold
    through the ledger, a Perfect's hold through trade/'s (a lookup - it is
    defined further down). **That finishes the carry.**

11. what is left beside the core, in moves, the smallest first (written here
    18 Oct, when the carry finished and the list was counted - 39 routes):
    - ~~who is around: the online list, the nearby list, the presence ticket
      (3)~~ (18 Oct, `players/`): the one table it writes is
      `presence_tickets`. Which character an account is playing stays here
      (the broadcast poll, trades and the guild roster ask it too) and is
      handed in, as the rule and as SQL; so are the presence socket's
      settings, read with every other setting - the address itself is worked
      out in the package.
    - ~~the server's switches: maintenance, the minimum build, PvP, trading,
      co-owners, the owner's announcement, and `/api/status` (8)~~ (18 Oct,
      `switches/`). **What a switch READS as stays in app.py** - the keys,
      the readers and the way each fails when its row is missing or broken -
      because the gates that ask run on every request (the maintenance window
      inside require_auth, the build gate before it, the co-owners inside
      role_for()); the package is handed them, and writes through
      set_server_setting(). The open trades it counts are trade/'s own count
      now (`trade.open_count()`), not a query into trade's table. **The
      broadcast poll stays** (below).
    - ~~the kill: `/api/combat/kill` - the XP, the level, the loot roll and the
      kill's record, in one transaction (1)~~ (18 Oct, `kill/`), with the
      token bucket and the spawn ceiling and **its two records** -
      `record_kill()` writes kill_reports and kill_tally, as `_record_kill()`
      did here, in the kill's transaction. The catalogue is handed whole, so
      the suites that seed the die or hold the roll on gamedata are heard; the
      skills' grant, the stirring lands and the bag's store are lookups,
      defined further down. test_killrecord's "no commit of its own" reads the
      packages now, and test_killwatch holds killwatch.py's copy of the
      window and the respawn floor to the gate's.
    - the owner's tools: gold, level, skill, grant, the gifts ledger, the save
      history and rollback, teleport and its acknowledgement (9)
    - the account's settings: e-mail and its check, the name colour (4)
    What stays in app.py on purpose: `/api/staff/powers` (it reads every route
    and quotes every area's limits - the composition root's own page), the
    broadcast poll `GET /api/server/broadcasts` (the heartbeat: it stamps
    presence and gathers every area's news - the world, events, a teleport,
    the guild, the party, a trade, chat, what waits on an answer, reports -
    so it is the composition root's too; moved, it would be a package that
    owns nothing and is handed seventeen things from a dozen areas),
    `/api/metrics` and `/api/economy/supply` (the request timing and the
    ledger audit, both the core's).
12. then the core itself (sessions, tokens, auth - the other 10 routes) into a
    package of its own, and each table's `CREATE` into its area - and app.py
    is the composition root it was meant to be.

Things noticed along the way that need upgrading, but are not part of a move,
are kept in UPGRADES.md - checked before they are written there.

The game has the same kind of work waiting: the 30,000-line test runner into a
file per area, and the HUD (characterhud.gd) into its parts. Server first:
it is where the money and the items are.
