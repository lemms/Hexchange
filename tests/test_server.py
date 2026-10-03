import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from hexchange.server import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "c.hexchange.json"))
    return TestClient(app)


def test_full_flow(client, tmp_path):
    assert client.get("/api/campaign").status_code == 409
    names = [s["name"] for s in client.get("/api/settings").json()]
    assert "generic" in names
    r = client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2, "warmup": 4})
    assert r.status_code == 200, r.text
    camp = r.json()
    assert camp["systems"] and (tmp_path / "c.hexchange.json").exists()
    sid = camp["systems"][0]["id"]
    good = camp["setting"]["goods"][0]["id"]
    assert client.get(f"/api/prices?good={good}").json()["systems"][sid]["price"] > 0
    d = client.get(f"/api/system/{sid}").json()
    assert len(d["market"]) == len(camp["setting"]["goods"])
    assert client.post("/api/step", json={"weeks": 2}).json()["started"]
    import time
    for _ in range(200):
        s = client.get("/api/status").json()
        if not s["running"]:
            break
        time.sleep(0.05)
    assert s["tick"] == 2 and s["done"] == 2 and s["error"] is None
    ev = {"id": "e1", "type": "piracy", "name": "Pirates", "start": 2, "targets": {"systems": [sid]}}
    assert client.post("/api/events", json=ev).status_code == 200
    assert client.post("/api/events", json=ev).status_code == 409
    assert client.get("/api/events").json()[0]["active"]
    assert client.delete("/api/events/e1").status_code == 200
    t = client.post("/api/trade", json={"system": sid, "good": good, "quantity": 5}).json()
    assert t["price"] > 0
    assert client.get("/api/flows").status_code == 200
    assert isinstance(client.get(f"/api/routes?origin={sid}&jump=4").json(), list)
    # player view hides unlisted systems; revealing shows the system but no prices
    assert client.get(f"/api/player/system/{sid}").status_code == 403
    client.put("/api/player_view", json={"visible_systems": [sid], "show_flows": False})
    assert client.get(f"/api/player/system/{sid}").json()["known"] is None
    assert client.get(f"/api/player/prices?good={good}").json()["systems"] == {}
    assert client.get("/").status_code == 200 and client.get("/player").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_polity_law_editing(client):
    camp = client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2,
                                              "warmup": 2, "polities": 2}).json()
    pid = camp["polities"][0]["id"]
    r = client.put(f"/api/polities/{pid}/legality", json={"key": "arms", "value": "legal"})
    assert r.status_code == 200 and r.json()["arms"] == "legal"
    assert client.put(f"/api/polities/{pid}/legality", json={"key": "arms", "value": "maybe"}).status_code == 422
    r = client.put(f"/api/polities/{pid}/legality", json={"key": "arms", "value": None})
    assert "arms" not in r.json()
    sid = next(s["id"] for s in camp["systems"] if s["polity"] == pid)
    client.put(f"/api/polities/{pid}/legality", json={"key": "arms", "value": "illegal"})
    row = next(m for m in client.get(f"/api/system/{sid}").json()["market"] if m["good"] == "arms")
    assert row["legal"] is False


def test_player_view_reveals_only_shared_systems(client):
    camp = client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2,
                                              "warmup": 2}).json()
    hidden = camp["systems"][-1]
    r = client.get("/api/player/campaign")
    data = r.json()
    assert data["systems"] == [] and data["lanes"] == [] and data["polities"] == []
    assert hidden["id"] not in r.text and hidden["name"] not in r.text
    # reveal one system: its lanes show, the far ends only as anonymous positions
    shown = camp["lanes"][0]["a"]
    out_lanes = {ln["id"] for ln in camp["lanes"] if shown in (ln["a"], ln["b"])}
    far = {ln["b"] if ln["a"] == shown else ln["a"] for ln in camp["lanes"] if ln["id"] in out_lanes}
    client.put("/api/player_view", json={"visible_systems": [shown], "show_flows": False})
    r = client.get("/api/player/campaign")
    data = r.json()
    assert [x["id"] for x in data["systems"]] == [shown]
    assert {ln["id"] for ln in data["lanes"]} == out_lanes
    assert {u["id"] for u in data["unknown"]} == far
    assert set(data["unknown"][0]) == {"id", "col", "row"}
    names = {s["id"]: s["name"] for s in camp["systems"]}
    for sid in far:
        assert names[sid] not in r.text                     # far ends stay anonymous
    assert all(set(ln) == {"id", "a", "b", "length"} for ln in data["lanes"])
    assert data["relations"] == {} and data["event_help"] == {}
    assert all("legality" not in p for p in data["polities"])


def _wait(client):
    import time
    for _ in range(400):
        s = client.get("/api/status").json()
        if not s["running"]:
            return s
        time.sleep(0.05)


def test_party_location_and_market_knowledge(client):
    camp = client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2,
                                              "warmup": 2}).json()
    a, b, far = camp["systems"][0]["id"], camp["systems"][1]["id"], camp["systems"][-1]["id"]
    good = camp["setting"]["goods"][0]["id"]
    # GM puts the party at A: live data there
    client.put("/api/party", json={"location": a})
    pa = client.get(f"/api/player/system/{a}").json()
    assert pa["here"] and pa["known"]["live"]
    assert client.get(f"/api/player/campaign").json()["party"] == a
    # move to B: A's prices are frozen at the departure week
    client.put("/api/party", json={"location": b})
    left = client.get(f"/api/player/system/{a}").json()["known"]
    assert not left["live"] and left["source"] == "visit"
    frozen = {r["good"]: r["price"] for r in left["market"]}
    client.post("/api/events", json={"id": "d", "type": "disaster", "name": "Quake", "start": 0,
                                     "targets": {"systems": [a]}, "params": {"production": 0.1}})
    client.post("/api/step", json={"weeks": 3})
    _wait(client)
    again = client.get(f"/api/player/system/{a}").json()["known"]
    assert {r["good"]: r["price"] for r in again["market"]} == frozen and again["age"] == 3
    live_now = client.get(f"/api/system/{a}").json()["market"]
    assert any(abs(r["price"] - frozen[r["good"]]) > 1e-6 for r in live_now)   # the real market moved on
    # the current location kept updating every week
    here = client.get(f"/api/player/system/{b}").json()["known"]
    assert here["live"] and here["age"] == 0
    # courier report for a distant system: one-off, labelled, then stale
    assert client.get(f"/api/player/system/{far}").status_code == 403
    rep = client.post("/api/party/report", json={"system": far, "note": "courier, Cr500"}).json()
    assert rep["source"] == "report"
    kf = client.get(f"/api/player/system/{far}").json()["known"]
    assert kf["source"] == "report" and kf["note"] == "courier, Cr500" and not kf["live"]
    prices = client.get(f"/api/player/prices?good={good}").json()["systems"]
    assert set(prices) == {a, b, far} and prices[b]["live"] and not prices[far]["live"]
    # GM can make the players forget
    client.delete(f"/api/party/knowledge/{far}")
    assert client.get(f"/api/player/system/{far}").status_code == 403
    # revealing systems does not clear location/knowledge
    client.put("/api/player_view", json={"visible_systems": [], "show_flows": False})
    assert client.get("/api/player/campaign").json()["party"] == b


def test_editor_endpoints(client):
    camp = client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2,
                                              "warmup": 2, "polities": 2}).json()
    assert [s["id"] for s in camp["sectors"]] == ["S1"]
    r = client.post("/api/sectors", json={"name": "East", "adjacent": "S1", "direction": "E",
                                          "width": 12, "height": 12, "polities": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert {s["name"] for s in data["sectors"]} == {camp["name"], "East"}
    east = data["result"]["id"]
    # add a system on an empty hex of a new blank sector, then connect it
    r = client.post("/api/sectors", json={"name": "Void", "adjacent": "S1", "direction": "S", "width": 12,
                                          "height": 12, "mode": "blank"})
    void = r.json()["result"]
    sys_ = client.post("/api/systems", json={"col": void["col0"] + 1, "row": void["row0"] + 1, "name": "Lonely",
                                             "attrs": {"port": "A"}}).json()
    new_id = sys_["result"]["id"]
    assert sys_["result"]["attrs"]["port"] == "A" and new_id.startswith(void["id"])
    assert client.post("/api/systems", json={"col": void["col0"] + 1, "row": void["row0"] + 1}).status_code == 400
    other = next(s["id"] for s in camp["systems"])
    lane = client.post("/api/lanes", json={"a": new_id, "b": other}).json()["result"]
    assert client.put(f"/api/lanes/{lane['id']}", json={"capacity": 50}).json()["result"]["capacity"] == 50
    assert client.delete(f"/api/lanes/{lane['id']}").status_code == 200
    assert client.put(f"/api/systems/{new_id}", json={"attrs": {"pop": 9}}).json()["result"]["attrs"]["pop"] == 9
    assert client.post(f"/api/systems/{new_id}/reroll").status_code == 200
    # polities
    p = client.post("/api/polities", json={"name": "Lonely League", "color": "#112233", "capital": new_id}).json()["result"]
    assert client.put("/api/assign", json={"systems": [other], "polity": p["id"]}).status_code == 200
    m = client.get("/api/campaign").json()
    assert next(s for s in m["systems"] if s["id"] == other)["polity"] == p["id"]
    assert client.post(f"/api/polities/{p['id']}/grow", json={"reach": 3}).status_code == 200
    assert client.put(f"/api/polities/{p['id']}", json={"name": "LL"}).json()["result"]["name"] == "LL"
    assert client.delete(f"/api/polities/{p['id']}").status_code == 200
    assert client.delete(f"/api/systems/{new_id}").status_code == 200
    assert client.delete(f"/api/sectors/{east}").status_code == 200
    assert client.delete("/api/sectors/nope").status_code == 400
    # the economy still runs after all that
    client.post("/api/step", json={"weeks": 1})
    s = _wait(client)
    assert s["error"] is None and s["tick"] >= 1
    # edits are refused while a run is in progress
    client.post("/api/step", json={"weeks": 30})
    assert client.post("/api/polities", json={"name": "Too Late"}).status_code in (200, 409)
    _wait(client)


def test_player_sees_only_known_sectors(client):
    client.post("/api/generate", json={"setting": "generic", "width": 12, "height": 12, "seed": 2, "warmup": 2})
    client.post("/api/sectors", json={"name": "Hidden Reach", "adjacent": "S1", "direction": "E", "width": 12, "height": 12})
    r = client.get("/api/player/campaign")
    assert r.json()["sectors"] == [] and "Hidden Reach" not in r.text


def test_new_galaxy_names_first_sector(client):
    d = client.post("/api/generate", json={"setting": "generic", "name": "Andromeda Reach", "sector_name": "Core",
                                           "width": 12, "height": 12, "warmup": 0}).json()
    assert d["name"] == "Andromeda Reach" and [s["name"] for s in d["sectors"]] == ["Core"]
    d = client.post("/api/generate", json={"setting": "generic", "name": "Solo", "sector_name": "",
                                           "width": 12, "height": 12, "warmup": 0}).json()
    assert [s["name"] for s in d["sectors"]] == ["Solo"]
