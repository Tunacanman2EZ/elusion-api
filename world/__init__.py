"""world - the stirring lands (game 0.46.0; a package since 18 Oct, the routes
as they were in app.py): a world that changes on its own.

    GET  /api/server/world        the settings, the stir now, the last few, how
                                  hunted each land has been, the next stir
                                  and why (owner)
    POST /api/server/world        change the settings (owner, password again)
    POST /api/server/world/stir   stir a land now, for a whole stir (owner,
                                  password again)

The owner: "lets build it in my game if you think it will elavate it". Every
few hours one of the seven lands (an element: dark, light, ice, wind, earth,
fire, water - Element.Type 1 to 7) STIRS. While it does, its monsters can hit
harder, have more health, move faster, drop loot more often and give more XP,
and armour's resistance to it can count for more - each of the six an owner's
switch with an amount (the GM panel's World tab), and the whole thing one
switch more. Players' own damage never changes: "elements defend; they do not
change what anybody hits for" (GameConstants, 0.11.0).

THE OWNER, after the first try: "i did not notice dps change on enemies or
anything". Harder hits alone were too quiet to feel, so a stirring land's
monsters also glow in its colour, and have more health and speed (game 0.46.0,
the same release).

WHO DECIDES WHAT. The server picks the land and pays the loot and XP (the kill
route asks what register() hands back as stir_for()), so a modded game cannot
stir a land or fake its drops. The game is TOLD which land and the numbers it
applies itself (the broadcast poll's "world", public()): how hard its monsters
hit and how much resistance counts, in Player.damage_taken(), and their health
and speed, in BaseEnemy. The combat books (combatbook.py, through presence.py's
stirred_lands()) count a stirring land's monster at the health its leader says
it was born with - more than the catalogue's, never less.

TICKS, NOT CLOCKS. world_ticks holds one row per stir, numbered 1, 2, 3... with
when it began and ends. Nothing runs on a timer: whoever asks first after one
ends - the broadcast poll every game in the world sends, or a kill - writes the
next (current_tick()). The next begins where the last ended, so the rhythm
holds while people play; after a quiet spell longer than a stir it begins when
somebody next asks. Two asking at once both try to write the same tick number;
the PRIMARY KEY lets exactly one in, and only that one announces it.

WHICH LAND. The one hunted least in the last WORLD_HUNT_WINDOW seconds
(kill_reports - every paid kill), never the one that just stirred, so the
world sends people to where they have not been - and (game 0.50.0) never one a
new player is learning in while another will do, which insights/ decides. A
tie, which with a quiet server is most of the time, is broken by a Random
seeded with the tick number: the same tick always picks the same land, whoever
asks. A stir event (events/, game 0.48.0) wins while it is on.

AND IF ANY OF IT FAILS, NOTHING STIRS. A setting that cannot be read is off
(services.settings_from(), like pvp_enabled()); a table that cannot be read or
written is "no land" (public(), stir_for()), which is simply the game as it
was. The poll that carries it is also the heartbeat, so the world must never
be the reason it fails.

Built like trade/ (ARCHITECTURE.md): routes.py (the edge, and the stir the
other areas ask for), services.py (the rules, pure), data.py (the SQL). It
never imports app.py; register() is handed what it needs - the settings row,
the world's voice, the moderation log, what land a monster is of, and the
events and insights, asked for on every use since they are registered after
it - and hands back what the kill route, the poll, the events and the insights
ask of it. WHAT STAYS IN app.py: the server_settings store, the broadcasts, the
kill route and the poll themselves, and the events' and insights' own
registration.
"""
from .routes import register  # noqa: F401
from .services import (LANDS, WORLD_DEFAULTS, WORLD_HUNT_WINDOW, WORLD_KEY, WORLD_LIMITS,  # noqa: F401
                       WORLD_RECENT_TICKS, WORLD_SWITCHES, stir_text)
