"""killrecord/services.py - the answers, from the rows. Pure: no database,
no request."""


def mine_answer(slot, rows, since):
    """{slot, since, total, kinds, kills: [{enemy_id, kills, first_at, last_at}]}."""
    kills = [{"enemy_id": r["enemy_id"], "kills": int(r["kills"]),
              "first_at": int(r["first_at"]), "last_at": int(r["last_at"])} for r in rows]
    return {"slot": slot, "since": since,
            "total": sum(k["kills"] for k in kills), "kinds": len(kills),
            "kills": kills}


def everyone_answer(rows, players, since):
    """{since, total, players, kills: [{enemy_id, kills, players, top, top_kills, yours}]}."""
    kills = [{"enemy_id": r["enemy_id"], "kills": int(r["total"]), "players": int(r["players"]),
              "top": r["top"], "top_kills": int(r["top_kills"]), "yours": int(r["yours"])}
             for r in rows]
    return {"since": since, "total": sum(k["kills"] for k in kills),
            "players": int(players or 0), "kills": kills}
