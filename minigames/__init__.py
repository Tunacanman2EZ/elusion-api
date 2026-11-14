"""minigames - the games beside the world, and how often each is played
(game 0.93.3).

    GET  /api/minigames         every minigame, and how long until you may
                                go in again
    POST /api/minigames/enter   going in: refused while your wait runs - and
                                your chest from the last day emptied
    POST /api/minigames/kill    an Endless elite or boss killed (0.93.4): its
                                loot into your chest in the corridor, no XP
    POST /api/minigames/finish  a Healer's Run's end (0.99.0): its gold, by
                                how far it got, into the character's purse
    POST /api/minigames/best    a Forge run's time (0.120.0), kept when it is
                                your best; the list carries it

THE FORGE (0.120.0): the owner, "Rewards + daily wait" - once a day as the
Endless, every fire sprite slain paying its own loot into the character's
chest by the island's arrival (loot only), held to what the run's schedule
could have sent since the Go (services.forge_ceiling()), and the best time a
run held kept per account.

THE HEALER'S RUN (0.99.0): the owner, "this minigame can be played 3 times a
day", and "gold should be rewarded on the screen then moved to char when
hitting leave". Its Go starts a run of its own (minigame_runs) while the
account has had fewer than three in the last day, and hands the game the gold
table to count on screen (services.healer_pays()); Leave (or Again) finishes
it, and the server pays from how far it got, held to what the time since the
Go allows (services.healer_believed()), once.

The owner: "we need to get rid of ladder and make this a button in hud that
says minigames this dungeon will have a 1 day wait timer for players", and
asked when the day starts: "Pressing Go". So going in IS the day: the first
Go writes when, and every Go inside GAMES' wait after it is refused (409,
with how long is left). Per account, so another character does not start a
fresh day; the owner and a co-owner skip the wait (testing), and their going
in is written all the same.

THE HONEST LIMIT. The server keeps the day; the game asks before it goes,
and a modified game could walk into the area without asking. But the
Endless's chest (0.93.4) pays only inside a day this table holds - that is
where the wait stops being a courtesy - and only as many elites and bosses as
the run's schedule could have sent since the Go (services.py). A modified game
could still report all of those: one Go a day caps it at one long run.

AND ONE ALLOWANCE A DAY (14 Nov, services.py, A DAY'S MINIGAME GOLD): every
run's gold at Leave and every chest's coins, counted together, at most
ELUSION_MINIGAME_GOLD_DAY (100,000 unless set) an account in any 24 hours.

THE CHEST is loot/'s (a table of cells, taken from like a bag by
/api/loot/take); this package fills it, a kill at a time, and empties it at
the account's next Go. Owner, 28 Oct: the Endless pays loot only, no XP.

Built like killrecord/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL), schema.py (its table). It never imports
app.py; register() is handed what it needs.
"""
from . import services  # noqa: F401
from .routes import register  # noqa: F401
