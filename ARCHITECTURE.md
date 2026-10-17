# The shape of the server, and where it is going

16 Oct. The owner, after a review put architecture at 7 of 10: "lets bump
this up Architecture" - and, asked where to start, "choose the best
professional choice". This is that choice, written down so every batch after
it follows the same road.

## Where it stands

`app.py` is about 23,700 lines: the shared core (config, the database and its
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
   its tables across app.py and the suites).
2. Make the package: `__init__.py` (what it is, its routes), `routes.py`,
   `services.py`, `data.py`.
3. Move the routes **as they are** - docstrings, messages, status codes - and
   the helpers only they use. What other areas also use stays and is handed in.
4. In app.py, at the area's old banner: the import and the `register()` call,
   with a line saying where the code went.
5. Run `test_routes.py` (no difference), the area's suite, `test_packages.py`
   (add the package's bare-app checks), then every suite; `codemap.py --check`.
6. **`git add` the new folder** - the server updates by `git pull`, and a
   package left out of the commit is an import error on the next restart.

## The order

Least tangled first, so each move is small and the pattern is proven before the
areas that hold money and items:

1. ~~the kill record~~, ~~friends~~ (16 Oct)
2. fishing and cooking (`gathering/`), with the server's grants they pay with
3. loot bags
4. guilds
5. parties and party voice
6. chat: world chat, channels, pictures, the ignore list's routes
7. trade and the kingdom tax
8. the stirring lands (`world/`) - combat asks it what a kill pays
9. moderation and the staff list
10. the carry: saves, character, bank, equipment, consuming, reviving
11. then the core itself (sessions, tokens, auth) into a package of its own,
    and each table's `CREATE` into its area - and app.py is the composition
    root it was meant to be.

The game has the same kind of work waiting: the 30,000-line test runner into a
file per area, and the HUD (characterhud.gd) into its parts. Server first:
it is where the money and the items are.
