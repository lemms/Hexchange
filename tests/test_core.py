import json

import numpy as np
import pytest

import hexchange as hx
from hexchange import hexgrid
from hexchange.equilibrium import Curves, Network, solve
from hexchange.model import Campaign, Setting


# ---------------------------------------------------------------- hex grid
def test_hex_distance_and_neighbours():
    assert hexgrid.distance((0, 0), (0, 0)) == 0
    assert hexgrid.distance((0, 0), (1, 0)) == 1
    assert hexgrid.distance((1, 0), (2, 1)) == 1          # odd column shifted down
    assert hexgrid.distance((0, 0), (3, 0)) == 3
    assert len(list(hexgrid.within(5, 5, 1, 20, 20))) == 6
    assert len(list(hexgrid.within(5, 5, 2, 20, 20))) == 18
    assert hexgrid.label(0, 0) == "0101" and hexgrid.label(31, 39) == "3240"


# ---------------------------------------------------------------- generation
def test_generation_is_deterministic(setting):
    a = hx.generate(setting, width=12, height=12, seed=5)
    b = hx.generate(setting, width=12, height=12, seed=5)
    c = hx.generate(setting, width=12, height=12, seed=6)
    assert a.model_dump() == b.model_dump()
    assert a.model_dump() != c.model_dump()


def test_lanes_respect_jump_and_connect_reachable_systems(setting):
    camp = hx.generate(setting, width=20, height=20, density=0.5, seed=2)
    mj = setting.lanes.max_jump
    by = {s.id: s for s in camp.systems}
    assert camp.lanes
    for ln in camp.lanes:
        assert 1 <= ln.length <= mj
        assert hexgrid.distance((by[ln.a].col, by[ln.a].row), (by[ln.b].col, by[ln.b].row)) == ln.length
    # union-find over lanes vs over all lane-capable pairs within jump: same components
    lane_ok = [s for s in camp.systems if setting.ports[str(s.attrs["port"])].lanes]

    def components(edges):
        parent = {s.id: s.id for s in lane_ok}
        def f(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a, b in edges:
            parent[f(a)] = f(b)
        return len({f(s.id) for s in lane_ok})

    possible = [(a.id, b.id) for i, a in enumerate(lane_ok) for b in lane_ok[i + 1:]
                if hexgrid.distance((a.col, a.row), (b.col, b.row)) <= mj]
    assert components([(ln.a, ln.b) for ln in camp.lanes]) == components(possible)


def test_codes_follow_setting_conditions(setting):
    camp = hx.generate(setting, width=16, height=16, seed=4)
    for s in camp.systems:
        if "Hi" in s.codes:
            assert s.attrs["pop"] >= 8
        if s.attrs["pop"] >= 8:
            assert "Hi" in s.codes


# ---------------------------------------------------------------- equilibrium solver
def _two_node(cap, cost=10.0):
    c = Curves(supply=[(np.array([100.0, 0.0]), 0.5)], demand=[(np.array([10.0, 100.0]), 1.2)], p_ref=100.0)
    net = Network(2, np.array([0, 1]), np.array([1, 0]), np.array([cap, cap]), np.array([cost, cost]))
    return c, net


def test_equilibrium_price_gap_equals_cost_when_trading():
    c, net = _two_node(1e6)
    sol = solve(c, net, tau=1e-3)
    assert sol.flow[0] > 1 and sol.flow[1] < 1e-6
    assert sol.price[1] - sol.price[0] == pytest.approx(10.0, abs=0.05)
    # conservation: excess supply at 0 leaves, shortage at 1 arrives
    assert sol.supply[0] - sol.demand[0] == pytest.approx(sol.flow[0], rel=1e-4)
    assert sol.demand[1] - sol.supply[1] == pytest.approx(sol.flow[0], rel=1e-4)


def test_equilibrium_capacity_creates_price_cliff():
    c, net = _two_node(20.0)
    sol = solve(c, net, tau=1e-3)
    assert sol.flow[0] == pytest.approx(20.0, rel=1e-3)
    assert sol.price[1] - sol.price[0] > 100           # far above the 10 transport cost


def test_equilibrium_no_trade_when_gap_below_cost():
    c = Curves(supply=[(np.array([50.0, 50.0]), 0.5)], demand=[(np.array([50.0, 55.0]), 1.0)], p_ref=100.0)
    net = Network(2, np.array([0, 1]), np.array([1, 0]), np.array([1e6, 1e6]), np.array([40.0, 40.0]))
    sol = solve(c, net, tau=1e-3)
    assert sol.flow.max() < 1e-3
    assert abs(sol.price[1] - sol.price[0]) < 40


# ---------------------------------------------------------------- simulation
def test_prices_bounded_and_settle(small):
    hist = []
    for _ in range(20):
        small.step(1)
        hist.append(small.prices.copy())
    rel = hist[-1] / small.pref
    assert np.all(np.isfinite(rel)) and rel.min() > 0.02 and rel.max() < 20
    drift = np.median(np.abs(hist[-1] - hist[-2]) / hist[-2])
    assert drift < 0.01


def test_events_apply_and_fully_revert(small):
    camp = small.camp
    small.step(2)
    base = hx.economy.ev_mod.build_modifiers(camp, camp.state.tick)
    ev = hx.Event(id="w", type="war", name="war", start=camp.state.tick, duration=3,
                  targets=hx.Targets(polities=[p.id for p in camp.polities[:2]]))
    small.add_event(ev)
    during = hx.economy.ev_mod.build_modifiers(camp, camp.state.tick)
    assert (during.lane_capacity < 1).any()
    after = hx.economy.ev_mod.build_modifiers(camp, camp.state.tick + 3)
    for f in ("production", "demand", "lane_capacity", "lane_risk", "lane_tariff", "offlane_risk"):
        assert np.array_equal(getattr(after, f), getattr(base, f)), f
    small.remove_event("w")
    assert not small.active_events()


def _border(sim):
    """A pair of polities sharing at least one lane."""
    camp = sim.camp
    pol = {s.id: s.polity for s in camp.systems}
    for ln in camp.lanes:
        a, b = pol[ln.a], pol[ln.b]
        if a and b and a != b:
            return a, b
    pytest.skip("no shared border in this sector")


def test_embargo_closes_lanes_but_smugglers_continue(small):
    a, b = _border(small)
    camp = small.camp
    small.step(3)
    smug_before = len(camp.state.smuggling)
    small.add_event(hx.Event(id="emb", type="embargo", name="embargo", start=camp.state.tick,
                             targets=hx.Targets(polities=[a]), params={"against": [b]}))
    small.step(4)
    pol = {s.id: s.polity for s in camp.systems}
    crossing = {ln.id for ln in camp.lanes if {pol[ln.a], pol[ln.b]} == {a, b}}
    assert crossing
    assert not [f for f in camp.state.flows if f.lane in crossing], "charted border lanes should be closed"
    smug_cross = [f for f in camp.state.smuggling
                  if {pol[x] for x in f.lane.split("~")} == {a, b}]
    assert smug_cross, "smugglers should still carry goods across the embargoed border"
    assert len(camp.state.smuggling) >= smug_before


def test_war_raises_military_prices(small):
    a, b = _border(small)
    camp = small.camp
    k = small.ix.good["arms"]
    small.step(3)
    terr = np.array([s.polity in (a, b) for s in camp.systems])
    before = small.prices[terr, k].mean()
    small.add_event(hx.Event(id="war", type="war", name="war", start=camp.state.tick,
                             targets=hx.Targets(polities=[a, b])))
    small.step(4)
    assert small.prices[terr, k].mean() > before * 1.1


def test_player_trading_narrows_the_gap(setting):
    """Players buying at the origin and selling at the destination push the two
    prices together compared with the same galaxy left alone."""
    def run(trade):
        camp = hx.generate(setting, width=16, height=16, density=0.45, polities=3, seed=3)
        sim = hx.Simulation(camp)
        sim.warmup(10)
        r = hx.find_routes(sim, camp.systems[0].id, cargo_tons=500, jump=4, max_jumps=4)[0]
        k, o, d = sim.ix.good[r.good], sim.ix.sys[r.origin], sim.ix.sys[r.destination]
        for _ in range(4):
            if trade:
                sim.trade(r.origin, r.good, r.quantity)
                sim.trade(r.destination, r.good, -r.quantity)
            sim.step(1)
        return sim.prices[d, k] - sim.prices[o, k], len(camp.trades)

    gap_alone, _ = run(False)
    gap_traded, n = run(True)
    assert n == 8
    assert gap_traded < gap_alone


def test_contraband_never_enters_banning_system_by_lane(small):
    camp = small.camp
    small.step(2)
    k = small.ix.good["narc"]
    for f in camp.state.flows:
        if f.good != "narc":
            continue
        ln = camp.lanes[small.ix.lane[f.lane]]
        dest = ln.b if f.amount > 0 else ln.a
        assert not small.illegal[small.ix.sys[dest], k]


# ---------------------------------------------------------------- file format
def test_campaign_roundtrip(small, tmp_path):
    small.step(2)
    p = hx.save_campaign(small.camp, tmp_path / "x.hexchange.json")
    back = hx.load_campaign(p)
    assert back.model_dump() == small.camp.model_dump()
    gz = hx.save_campaign(small.camp, tmp_path / "x.hexchange.json.gz")
    assert hx.load_campaign(gz).state.tick == small.camp.state.tick
    # a reloaded campaign continues identically
    s2 = hx.Simulation(back)
    s2.step(1)
    small.step(1)
    assert np.allclose(s2.prices, small.prices, rtol=1e-3)


def test_validation_rejects_bad_files(setting, tmp_path):
    data = json.loads(setting.model_dump_json())
    data["goods"][3]["inputs"] = {"unobtainium": 1}
    with pytest.raises(Exception):
        Setting.model_validate(data)
    camp = hx.generate(setting, width=8, height=8, seed=1)
    bad = json.loads(camp.model_dump_json())
    bad["lanes"].append({"id": "x", "a": "9999", "b": bad["systems"][0]["id"], "length": 1, "capacity": 1})
    with pytest.raises(Exception):
        Campaign.model_validate(bad)
    newer = json.loads(camp.model_dump_json())
    newer["schema_version"] = 999
    (tmp_path / "n.json").write_text(json.dumps(newer))
    with pytest.raises(ValueError):
        hx.load_campaign(tmp_path / "n.json")


def test_settings_found_via_env(setting, tmp_path, monkeypatch):
    (tmp_path / "mine.setting.json").write_text(setting.model_copy(update={"name": "Mine"}).model_dump_json())
    monkeypatch.setenv("HEXCHANGE_SETTINGS", str(tmp_path))
    assert "mine" in hx.list_settings()
    assert hx.load_setting("mine").name == "Mine"


def test_schema_export(tmp_path):
    paths = hx.export_schemas(tmp_path)
    assert {p.name for p in paths} == {"setting.schema.json", "campaign.schema.json"}
    assert json.loads(paths[0].read_text())["title"] == "Setting"
