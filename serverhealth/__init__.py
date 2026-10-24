"""serverhealth - the live server as the owner sees it, without SSH (game
0.78.0).

    GET /api/server/health    owner and co-owner: the last minute, today and
                              the week - players and the biggest crowds, the
                              tick, data out and the month it adds up to, the
                              machine, the slowest routes, and whether the
                              kill receipts are ready to switch on

The owner, after the 128-player swarm: "work on this or if you have a even
better idea lets hear it" - of four next steps: send each game only the
players near it, watch the live server, test on the real machine, let the
server referee every kill. Two of those were numbers only SSH could read
(GET /api/metrics with a token, killwatch.py's receipts line), and the first
needed one nobody had: what the server really sends, against the 1,000 GiB a
month the droplet comes with. This puts all of them on the GM panel's Health
tab.

WHERE THE NUMBERS COME FROM. presence.py writes a presence_health row a
minute (its header, "HOW ITS MINUTES WENT"): connections, crowds, ticks, bytes
it sent, the machine's network out, load and memory. The receipts are
killwatch.py's own receipts_backing(), handed in, so the panel and the command
line are one count. The routes' timings are app.py's /api/metrics, handed in -
this worker's, like that route's. The disk is read here.

Built like economywatch/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL - reads only). It never imports app.py,
nor killwatch.py: register() is handed what it needs.
"""
from .routes import register  # noqa: F401
from .services import (CROWD_NOW, CROWD_WATCH, RECEIPTS_MIN_KILLS, RECEIPTS_READY_SHARE,  # noqa: F401
                       TRANSFER_ALLOWANCE_GIB)
