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
    assert client.post("/api/step", json={"weeks": 2}).json()["tick"] == 2
    ev = {"id": "e1", "type": "piracy", "name": "Pirates", "start": 2, "targets": {"systems": [sid]}}
    assert client.post("/api/events", json=ev).status_code == 200
    assert client.post("/api/events", json=ev).status_code == 409
    assert client.get("/api/events").json()[0]["active"]
    assert client.delete("/api/events/e1").status_code == 200
    t = client.post("/api/trade", json={"system": sid, "good": good, "quantity": 5}).json()
    assert t["price"] > 0
    assert client.get("/api/flows").status_code == 200
    assert isinstance(client.get(f"/api/routes?origin={sid}&jump=4").json(), list)
    # player view hides unlisted systems
    assert client.get(f"/api/player/system/{sid}").status_code == 403
    client.put("/api/player_view", json={"visible_systems": [sid], "show_flows": False})
    assert client.get(f"/api/player/system/{sid}").status_code == 200
    pp = client.get(f"/api/player/prices?good={good}").json()
    assert list(pp["systems"]) == [sid]
    assert client.get("/").status_code == 200 and client.get("/player").status_code == 200
    assert client.get("/static/app.js").status_code == 200
