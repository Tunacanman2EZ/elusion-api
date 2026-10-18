# The shape of the server, and where it is going

16 Oct. The owner, after a review put architecture at 7 of 10: "lets bump
this up Architecture" - and, asked where to start, "choose the best
professional choice". This is that choice, written down so every batch after
it follows the same road.

## Where it stands

`app.py` is about 20,000 lines: the shared core (config, the database and its
write lock, sessions and tokens, the auth decorators, rate limiting, request
timing) and most of the game's areas, one after another. It works, and every
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
   and gifts pay with the same three. They go with the carry (10).
3. ~~loot bags~~ (18 Oct): both routes, and every line of SQL on the two
   tables but character delete's. **A new bag is still made in app.py**
   (`_create_loot_bag()`, with the package's `stamped()` and `store_bag()`),
   because it is the kill's commit, as `_record_kill()` is. The money and the
   backpack the take pays into are handed in.
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
    carry. Next: the carry itself, the grants with it; then saves and the
    status write.
11. then the core itself (sessions, tokens, auth) into a package of its own,
    and each table's `CREATE` into its area - and app.py is the composition
    root it was meant to be.

The game has the same kind of work waiting: the 30,000-line test runner into a
file per area, and the HUD (characterhud.gd) into its parts. Server first:
it is where the money and the items are.
