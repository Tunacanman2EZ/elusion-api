# How Elusion is built

13 Nov 2026, game 0.156.0. The owner: "security and architecture need a
full update". This replaces the 16 Oct page, which was the plan for taking
app.py apart - that plan is done (19 Oct), and since then the server grew a
second process, rooms per party, its own books on every monster, a guard on
the month's data, Google sign-in and a gate for old games. This is the
whole system as it stands, measured where it can be.

Where the rest lives: **DEPLOY.md** runs it (the machine, the settings, the
updates, the backups), **SECURITY.md** says what it trusts and why,
**CODEMAP.md** is app.py's generated map (`python codemap.py`, never
edited), and the game's `docs/apicontract.md` is the wire, route by route.

## The pieces

| Piece | What it is | Where |
|---|---|---|
| The game | Godot 4.7, GDScript. A Windows build (the Mobile renderer) and a browser build (WebGL 2, the Compatibility renderer, no threads), the same code | the player's PC or phone |
| The website | elusionrpg.com and www: static pages, the update log, the guide, the simulator, the visit counter | Netlify |
| The browser build | play.elusionrpg.com, the exported game as files; the itch.io page embeds it | the droplet, `/srv/elusion-web` |
| The API | Flask under gunicorn: `app.py`, the shared core (about 9,200 lines), and 39 feature packages - about 175 routes | the droplet, 127.0.0.1:5000 |
| The presence server | `presence.py`, one asyncio process holding every WebSocket: who stands where, the shared monsters, attacks, levers, party runs, voice setup | the droplet, 127.0.0.1:5001 |
| The database | SQLite, one file, WAL | the droplet, `/var/lib/elusion` |
| The proxy | Caddy: TLS (it fetches and renews the certificates), compression, and which process a request goes to | the droplet, 80 and 443 |
| Mail | SMTP, for recovery codes and confirming an address (`mailer.py`) | a mail provider |
| Voice relay | Cloudflare's TURN relay, so no player sees another's address; the API hands out short-lived credentials | Cloudflare |
| Google sign-in | Google's identity service; the API checks the token itself | Google |
| The watchers | scripts beside it: what players claim to kill (`killwatch.py`), deaths, gifts, a canary, the red-team bot (`security_bot.py`), the nightly backup (`backup_db.py`, SQLite's own online copy) and a restore drill | the droplet, or a copy of it |

**One machine.** DigitalOcean `elusion-1`, SFO2: 1 vCPU, 1 GB of memory
and a 1 GB swap file, 25 GB of disk, 1,000 GiB of outbound data a month.
ufw lets in SSH, 80 and 443 and nothing else; SSH takes keys only. The
names api.elusionrpg.com and play.elusionrpg.com point at it; the bare
domain stays on Netlify.

## How they talk

```
  the game (PC / phone / browser)
     |                         |
     | HTTPS, JSON             | WSS, JSON text frames
     v                         v
  Caddy :443  -- /api/* -->  gunicorn: 2 workers x 4 threads --> app.py + packages
     |                                                              |    ^
     +------- /ws/* ------>  presence.py (one asyncio loop)         |    |
                                |   reads tickets, writes books,    v    |
                                +-- health, samples -------------> SQLite (WAL)

  play.elusionrpg.com: Caddy serves the build's files, and /api and /ws on
  the same origin - a browser game then needs no cross-site rules.
  elusionrpg.com: Netlify, static; it never talks to the droplet but to say
  "Play".
```

**Signing in.** The game posts a password (or finishes a Google sign-in)
to `/api/auth/*` and gets a session token, which every later request
carries. The token is a row in the database: logging out, a ban, a kick, a
new password or a login elsewhere deletes it, and every request after that
is refused.

**Playing.** The game asks the API for what lasts - the character, the
backpack one cell at a time, a kill's pay, a trade, a shop sale, the bank -
and polls `/api/server/broadcasts` every ten seconds, which is also its
heartbeat (notices, the lands, events, a teleport, guild, party and trade
news in one answer, each part answered on its own - one part failing gives
up that part, not the answer).

**Seeing each other.** The game asks the API for a presence ticket
(`POST /api/presence/ticket`), opens `wss://.../ws/presence` and sends the
ticket in its hello. presence.py never sees a password: it reads the ticket
row the API wrote, and who the player is on screen - name, rank, colour,
guild, class, level, pets - came from the API's own rows. A ticket lives two
minutes and the game renews it every minute; every five seconds the server
drops any connection whose ticket ran out or whose login no longer exists,
so a ban or a logout reaches the socket within seconds without the API
knowing presence.py exists.

**The two processes share only the database.** gunicorn's workers share no
memory with each other or with presence.py. What the API needs to know of
the live world it reads from rows presence.py writes: a sample of where
each character stands every 15 seconds (`position_samples`), one health row
a minute (`presence_health`), and the books (`combat_kills`,
`combat_flags`).

## Who owns what

The rule the whole server is built on: **assume the game is hostile.**
Whatever a player could gain by lying about is the server's.

| The server's | How |
|---|---|
| Level, XP | granted by the kill route and nothing else |
| Gold | double-entry ledger (`gold_ledger`); every change is a pair of rows, and `/api/economy/supply` adds them up |
| Health, mana and stamina maxima | derived from class, level and gear on every status write, never stored from a request |
| Loot | rolled with `random.SystemRandom` (a Mersenne Twister can be rebuilt from 624 outputs), held in bag rows the game only draws |
| The backpack, the bank, worn gear | moved one cell at a time by the server (carry/, bank/, equipment/); the game asks, the server answers with the bag it holds |
| A kill's pay | ceilings on rate and kind per area (gamedata.json, written by the game's exporter), and - with the owner's switch on - the books' receipt (below) |
| The Endless's chest, the minigames' day | rolled and kept by minigames/, held to the run's own schedule (gamedata.json's `"endless"`) |
| Trades, the shop, the smith, the larder | each a transaction in its package, the kingdom tax with it |

| The game's | Held how |
|---|---|
| Health, mana, stamina as they are | written by the game, clamped; a heal past what regeneration allows is trimmed back and logged (status/'s healing check) |
| Where a player stands, what they swing | relayed by presence.py, never stored but for the 15-second sample |
| The monsters of a shared area | run by one game, the area's leader (below); the server keeps books but does not simulate them |
| The Endless's swarm of thousands | each game's own arrays, made the same from a seed; worth nothing to the server |

## Inside the API

**app.py is the composition root.** What is in it is the shared core:
configuration and the one startup check of every setting (`envfile.py`,
0.86.1), the database and its write lock, the core's tables and every boot
migration, sessions and tokens, the auth decorators and gates
(`require_auth`, `require_owner`, ranks), rate limiting, request timing,
the ledgers, the four pages that read every area (the staff powers page,
the broadcast poll, the metrics, the supply audit), and one `register()`
call per area. `wsgi.py` is what gunicorn imports; `--preload` is not
decoration (without it every worker runs the migrations at once).

**The feature packages**, a folder per area (CODEMAP.md lists every route):

| Area | Packages |
|---|---|
| Accounts | `auth/`, `googlesignin/`, `accountsettings/`, `accountdelete/` |
| A character | `saves/`, `status/`, `skills/`, `carry/`, `equipment/`, `consuming/`, `death/`, `killrecord/` |
| Combat and loot | `kill/`, `loot/`, `minigames/` |
| Making and selling | `shop/`, `smith/`, `gathering/` (fishing and cooking), `larder/`, `bank/` |
| The economy | `trade/`, `kingdom/`, `economyrules/`, `economywatch/` |
| The world | `world/` (the stirring lands), `events/`, `switches/` (maintenance, PvP, `/api/status`) |
| Together | `players/`, `friends/`, `parties/` (and party voice), `guilds/`, `chat/` |
| Staff | `moderation/`, `staffdesk/`, `ownertools/` |
| The server itself | `serverhealth/`, `insights/`, `playerstats/`, `clienterrors/` |

### The package contract (BE105)

- **Three layers.** `routes.py` is the edge (request in, answer out),
  `services.py` the rules (pure: no database, no request), `data.py` the
  SQL; `schema.py` its tables, where it has any.
- **It never imports app.py.** `register(app, *, ...)` is handed the app,
  the decorators, `get_db` and whatever else it needs, by keyword. That is
  what lets `test_packages.py` run every package on a bare Flask app with
  stand-ins - and two such apps side by side.
- **What other areas share stays in app.py and is handed in**: who is
  online, how a person is shown, the limits that are app.py's tunables
  (asked through a callable, so a suite can turn them down).
- **What other areas ask of a package, register() can hand back** (chat/
  hands back `post_chat()` and `can_read_row()`).
- **A seam a suite replaces is handed as a lookup** (`lambda ...:
  name(...)`), so the swap is heard on every call. A function handed in
  once is one the suite never touches - which is how test_concurrency's
  hold stopped reaching the loot take, still passing, until it was found
  on 18 Oct.
- **A module handed in whole is a new name for it** (trade/ is handed
  gamedata as `catalogue`); a suite that holds a rule by searching for
  `gamedata.` must search for `catalogue.` too.
- **A route keeps its endpoint name** (`add_url_rule(..., endpoint=...)`)
  - rate limiting, request timing and `/api/staff/powers` key on it - **and
  its docstring**, which is its page in `/apidocs`.
- **A table lives with the one area that adds its rows**: its `CREATE` and
  indexes in that package's `schema.py`, with a `create(db)` (and an
  `upgrade(db, add_column)` for columns added since). What the core writes,
  or what several areas add rows to (`consume_grants`, `carry_items`,
  `skills`), stays in `init_db()`, which calls every `create()` once, in
  one commented order.

### The guards

Nothing a player or the game can see changes by accident. Checked, not
hoped:

- **`routes.lock`, held by `test_routes.py`**: one line per route and
  method - path, endpoint, whether it needs a token, the lowest rank, the
  password asked again, the write lock, the docstring. A change that drops,
  renames or loosens anything is a difference, line by line, and every
  route the lock says needs a token is asked without one and must answer
  401. A change meant is written with `python test_routes.py --write`.
- **`test_packages.py`**: every package on a bare app; a package added
  without a bare-app run of its own fails it.
- **`test_deploy.py`** copies every package by the same rule, so a new one
  cannot be left out of the server's copy.
- **`test_schema.py`**: the database a server builds, held to a fixture -
  every table, column, index, constraint, foreign key and trigger, and
  which file creates it (`--write-fixture` for a change meant).
- **The rules a source scan holds**: test_ownership (every per-user write
  is scoped to its owner), test_quality, test_security_doc (SECURITY.md's
  claims point at code that exists), and 77 suites in all, run side by
  side by `run_tests.ps1`.

### Adding an area

1. A folder: `__init__.py` (what it is, its routes), `routes.py`,
   `services.py`, `data.py`, and `schema.py` if it has tables.
2. In app.py: the import and the `register()` call, at the area's banner.
3. Its suite, its bare-app checks in `test_packages.py`, the route lock
   written, the schema fixture written; re-run `codemap.py`.
4. **`git add` the new folder** - the server updates by `git pull`, and a
   package left out of the commit is an import error on the next restart.

### Old games, and the gate

Every request carries the game's build (`X-Elusion-Build`). An owner's
switch (`POST /api/server/minbuild`) sets the lowest build the server
takes, and an older game is told to update rather than half-working
(app.py, `min_client_build()`). **It ships disarmed** (0 until an owner sets
it), and it refuses a minimum above the build the server itself knows
(`CURRENT_CLIENT_BUILD`). **It is a protocol control, not a security one**:
the header is the game's to send, so it stops an honest old build and
nothing else - app.py says so beside it.

## Inside presence.py

One process, one loop, every connection in memory - a position is worth
something for a tenth of a second, so none of it touches the database but
the samples, the health rows and the books, each on its own clock.

- **Rooms.** An area is a room, except the ones played as a party
  (`INSTANCED_PLACES`: the Endless, the Healer's Run, the Descent): a room
  per party, or per player alone, chosen by the server and never named to
  the game, so nobody can walk into somebody else's.
- **The tick** (`TICK_SECONDS`, a tenth of a second): who moved, every step
  each sent, and the attacks made since, to everyone near.
- **Only what is near** (0.89.0): each game is sent the players within a
  screen and a margin of it (`NEAR_X` 480, `NEAR_Y` 300; out past `FAR_X` /
  `FAR_Y`). Staff see everybody.
- **Shared monsters: one game runs them.** The server has no map, no
  collision and no AI. Each room has a leader - the game in it longest -
  whose game runs the monsters as a lone player's would and says what they
  do; the others draw them where it says and send their hits to it. The
  leader leaving hands the room to the next game in line. The server
  decides only who leads and who hears what: a world message from anybody
  else is dropped.
- **The books** (`combatbook.py`): every world message and every hit is
  also read into the server's own count of each monster's health - every
  hit held to what that character could do, every spawn to the area's map,
  every death judged (AGREED, SHORT, NOT DUE). Since 0.87.0 a game in a
  shared room draws the books' health, not the leader's. With the owner's
  switch on, a kill is paid only against an AGREED receipt for that
  account and that monster (kill/, 0.57.0).
- **Limits on every connection**: a hello within five seconds, a message
  at most 2 KB (a leader's world 64 KB), 40 a second with a burst of 80,
  at most 1,000 connections, and per-message compression at level 1.
- **The data guard** (0.79.0): every minute it adds what the machine sent
  to the month's, and works out what it can still afford a second until
  the month ends. Over it, it tightens a level - full, half (others' steps
  at half rate), crowd (rooms of 16), alone - and loosens again when the
  month allows. The bill comes before the core.
- **Voice**: presence carries a call's setup between party members; the
  sound goes through Cloudflare's TURN relay, never this server.
- **How its minutes went**: one `presence_health` row a minute -
  connections, the biggest crowds, late ticks, bytes sent, the machine's
  network out, its share of the core, memory - for the GM panel's Health
  tab.

## Inside the game

- **Twenty autoloads** hold what outlives a scene: `Api` (every request,
  the session, the build header), `Presence` (the socket), `CharacterData`
  (the character and its saves), `Combat` (kill reports - an autoload
  because a monster's own coroutine dies with it), `AreaRegistry`,
  `Settings`, `PerfOverlay` (the readout), and the rest.
- **An area is a scene**, and `AreaRegistry` gives each its systems as it
  arrives: the floor and props drawn in chunks near the view, the sleeper
  (far monsters stop thinking), the shared monsters (`monstersync.gd`), the
  spikes' effects drawn together, and once a session every look the game
  draws with, drawn out of sight (`shaderwarm.gd`, 0.156.0).
- **A minigame** (the Endless, the Healer's Run, the Descent, the Forge,
  the Cloth, the Mage's Stand) is a run in an area: its own clock and its
  monsters loaded before they are wanted (`scenewarmer.gd`). The Endless,
  the Healer's Run and the Descent play as a party, in a room of their
  own, one game leading the run as it leads the monsters.
- **The Endless's horde** is not monsters: thousands of slimes in packed
  arrays, stepped 20 times a second (10 on a phone), drawn as one
  MultiMesh, hit through 256 stand-ins.
- **A phone** plays at 30 frames, moves the world 40 times a second, lights
  simply and draws the bar, the readout and the Testing Ground's panel as
  pictures redrawn only when they change.
- **Its suite**: 12 parts on one node (`src/tools/testrunner*.gd`), run
  headless in a real Godot; CI runs it on every push.

## Limits, measured

| What | Measured | Where |
|---|---|---|
| A read (`GET /api/player/status`) | about 1,000 a second, under 2 ms | `loadtest.py`, 2-core box |
| A kill, the heaviest write | about 560 committed a second | the same |
| The ceiling of writes | SQLite's one writer, whatever the workers | DEPLOY.md, "Sizing" |
| 128 players in one area, every one fighting | presence 13% of a core, the API 12%; a step's p99 104 ms | `swarm.py`, a container core |
| 256 in one area | presence 25%, the API 24%; p99 118 ms | the same |
| What goes out | grows with the square of a crowd in one place: 13 MB/s at 128, 48 at 256; 4 MB/s for 128 spread over the Big Field, only what is near | the same |
| The month | 0.4 MB/s held all month is the whole 1,000 GiB; the data guard keeps under it | DEPLOY.md, "The data guard" |
| A phone's boss fight with the horde | physics about 1.5 ms a tick in the container; the owner's phone, 9.1 before 0.156.0 | zz/fightbench.gd (the game) |

**The core is not the limit; the network and the bill are.** One core
holds a crowd past anything the game has seen; what a crowd costs to send
is what binds, and the guard holds it.

## What comes next, and what it would take

- **More than one writer.** SQLite serialises writes; shortening the kill's
  transaction comes first, Postgres only when that is not enough. Nothing
  before launch needs it.
- **A second machine.** presence.py and the API already share only the
  database, so presence can move to a box of its own the day the network
  of one is the limit; the tickets and the books would need the database
  reachable from it.
- **The server running combat** (E3_SCOPE.md, option C): the books watch
  and judge today; deciding a fight on the server is a season of work, and
  the receipts are the step before it.
- **Updates reaching players** (LAUNCH_ONE_WAY_DOORS.md, D2): the gate can
  turn an old game away; how a player gets the new one is a decision, not
  code - the browser build is always current, a downloaded one is not.
- UPGRADES.md keeps the small things noticed along the way.

## How app.py got this way

`app.py` was about 20,000 lines on 16 Oct, every area in one file. Over
three days it was taken apart into packages, least tangled first - the kill
record and friends, then fishing and cooking, the loot bag, guilds,
parties, chat, trade, the kingdom, the world, moderation, the staff desk,
gear, using things, death, the bank, the backpack, skills, saves, the live
stats, the shop, who is online, the switches, the kill, the owner's tools,
the account settings, and the login last (19 Oct) - each move held to the
route lock, the schema fixture and the area's own suite. Every area since
was built as a package from the start: smith/, larder/, economyrules/,
economywatch/, clienterrors/, serverhealth/, minigames/, accountdelete/,
googlesignin/. The steps a move took are in git history (the 16 Oct
version of this file).
