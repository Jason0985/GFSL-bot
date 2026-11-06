"""Einteilung der Ersatzfahrer. Start: python -m tests.test_assign"""
import random

from gfsl.cogs.events import assign


def event(signups: dict, assignments: dict | None = None) -> dict:
    order = iter(range(1, 100))
    return {"signups": {u: {"status": "yes", "class_id": c, "late": late, "order": next(order)} for u, (c, late) in signups.items()},
            "assignments": {u: {"class_id": c, "at": 0} for u, c in (assignments or {}).items()}}


d = {"settings": {"classes": [{"id": 1, "kind": "team", "seats": 2}, {"id": 2, "kind": "team", "seats": 2},
                              {"id": 3, "kind": "reserve"}, {"id": 4, "kind": "streamer"}]}}

# Stammfahrer a2 kommt zu spät, Ersatzfahrer sitzt schon in seinem Auto, insgesamt ist noch ein Platz frei:
# a2 fährt sein eigenes Auto (Q-Sperre), der Ersatzfahrer wird umverteilt
ev = event({"a1": (1, False), "b1": (2, False), "ef": (3, False), "a2": (1, True)}, {"ef": 1})
assert assign.update(d, ev, random.Random(0)) == [("a2", None, 1), ("ef", 1, 2)]

# Kein Platz mehr frei: a2 verdrängt den Ersatzfahrer nicht
ev = event({"a1": (1, False), "b1": (2, False), "b2": (2, False), "ef": (3, False), "a2": (1, True)}, {"ef": 1})
assert assign.update(d, ev, random.Random(0)) == []
assert assign.waiting(d, ev) == ["a2"]

# Streamer werden nie eingeteilt, auch nicht mit "late"
ev = event({"a1": (1, False), "st": (4, True)})
assign.update(d, ev, random.Random(0))
assert ev["assignments"] == {} and assign.waiting(d, ev) == []

print("ok")
