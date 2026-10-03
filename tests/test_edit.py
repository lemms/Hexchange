"""GM editing: sectors, systems, lanes, polities; state preserved across rebuilds."""

import numpy as np
import pytest

import hexchange as hx
from hexchange import edit, hexgrid
from hexchange.model import Campaign


@pytest.fixture()
def sim(setting):
    camp = hx.generate(setting, name="Core", width=16, height=16, density=0.4, polities=2, seed=5)
    s = hx.Simulation(camp)
    s.warmup(6)
    s.step(2)
    return s


def _valid(camp):
    Campaign.model_validate(camp.model_dump())


def test_add_sector_connects_without_touching_existing_lanes(sim):
    camp = sim.camp
    before = {ln.id: ln.model_dump() for ln in camp.lanes}
    prices = {x.id: sim.prices[i].copy() for i, x in enumerate(camp.systems)}
    sec = edit.add_sector(camp, "East", adjacent=("S1", "E"), width=16, height=16, polities=1, seed=9)
    _valid(camp)
    assert sec.col0 == 16 and camp.width == 32
    for lid, ln in before.items():                                # existing lanes unchanged
        assert next(x for x in camp.lanes if x.id == lid).model_dump() == ln
    sector = {x.id: x.sector for x in camp.systems}
    assert any({sector[ln.a], sector[ln.b]} == {"S1", sec.id} for ln in camp.lanes)
    assert all("-" in x.id for x in camp.systems if x.sector == sec.id)
    sim.rebuild()
    for sid, p in prices.items():                                  # old markets unchanged
        assert np.allclose(sim.prices[sim.ix.sys[sid]], p)
    sim.step(2)
    # the party can travel and routes cross the border
    a = next(x.id for x in camp.systems if x.sector == "S1")
    b = next(x.id for x in camp.systems if x.sector == sec.id)
    sim.move_party(a)
    sim.move_party(b)
    assert camp.player_view.location == b and a in camp.player_view.knowledge


def test_add_sector_is_deterministic(setting):
    def make():
        camp = hx.generate(setting, width=12, height=12, seed=2)
        edit.add_sector(camp, "South", adjacent=("S1", "S"), width=12, height=12, seed=4)
        return camp.model_dump()
    assert make() == make()


def test_sector_to_the_west_shifts_coordinates_but_not_ids(setting):
    camp = hx.generate(setting, width=12, height=12, seed=2)
    ids = [x.id for x in camp.systems]
    labels = {x.id: camp.sectors[0].label(x.col, x.row) for x in camp.systems}
    edit.add_sector(camp, "West", adjacent=("S1", "W"), width=12, height=12, seed=1)
    _valid(camp)
    assert min(s.col0 for s in camp.sectors) == 0 and min(s.row0 for s in camp.sectors) == 0
    s1 = camp.sectors[0]
    for x in camp.systems:
        if x.id in labels:
            assert s1.label(x.col, x.row) == labels[x.id]
    assert [x.id for x in camp.systems][:len(ids)] == ids
    with pytest.raises(edit.EditError):
        edit.add_sector(camp, "Overlap", adjacent=("S1", "W"), width=12, height=12)


def test_blank_sector_hand_built(sim):
    camp = sim.camp
    sec = edit.add_sector(camp, "Frontier", adjacent=("S1", "S"), width=12, height=12, mode="blank")
    assert not any(x.sector == sec.id for x in camp.systems)
    a = edit.add_system(camp, sec.col0 + 1, sec.row0 + 1, name="Alpha", attrs={"port": "A", "pop": 7, "tech": 12})
    b = edit.add_system(camp, sec.col0 + 3, sec.row0 + 2, name="Beta", attrs={"port": "B"})
    assert a.attrs["port"] == "A" and a.attrs["pop"] == 7 and a.codes == hx.generate.__globals__["classify"](camp.setting, a.attrs)
    with pytest.raises(edit.EditError):
        edit.add_system(camp, sec.col0 + 1, sec.row0 + 1)          # occupied
    with pytest.raises(edit.EditError):
        edit.add_system(camp, -5, -5)                              # outside every sector
    ln = edit.add_lane(camp, a.id, b.id)
    assert ln.length == hexgrid.distance((a.col, a.row), (b.col, b.row))
    with pytest.raises(edit.EditError):
        edit.add_lane(camp, a.id, b.id)                            # duplicate
    # connect the frontier to the core automatically
    new = edit.auto_lanes(camp, [a.id, b.id])
    _valid(camp)
    sim.rebuild()
    sim.step(1)
    assert sim.quote(a.id, camp.setting.goods[0].id).price > 0
    edit.update_lane(camp, ln.id, capacity=123, risk=0.2)
    assert next(x for x in camp.lanes if x.id == ln.id).capacity == 123
    edit.remove_lane(camp, ln.id)
    assert not any(x.id == ln.id for x in camp.lanes)
    assert isinstance(new, list)


def test_remove_system_leaves_no_dangling_references(sim):
    camp = sim.camp
    victim = max(camp.systems, key=lambda x: sum(x.id in (l.a, l.b) for l in camp.lanes))
    sim.add_event(hx.Event(id="p", type="piracy", name="p", start=camp.state.tick,
                           targets=hx.Targets(systems=[victim.id, camp.systems[0].id])))
    sim.move_party(victim.id)
    camp.player_view.visible_systems = [victim.id]
    for p in camp.polities:
        p.capital = victim.id if p.id == victim.polity else p.capital
    edit.remove_system(camp, victim.id)
    _valid(camp)
    assert not any(victim.id in (l.a, l.b) for l in camp.lanes)
    assert victim.id not in camp.events[0].targets.systems
    assert camp.player_view.location is None and victim.id not in camp.player_view.knowledge
    assert all(p.capital != victim.id for p in camp.polities)
    sim.rebuild()
    sim.step(1)


def test_update_system_reclassifies_and_updates_lanes(sim):
    camp = sim.camp
    x = next(s for s in camp.systems if any(s.id in (l.a, l.b) for l in camp.lanes))
    edit.update_system(camp, x.id, attrs={"pop": 9, "tech": 13})
    assert "Hi" in x.codes and "Ht" in x.codes
    edit.update_system(camp, x.id, attrs={"port": "X"})          # no port -> no charted lanes
    assert not any(x.id in (l.a, l.b) for l in camp.lanes)
    with pytest.raises(edit.EditError):
        edit.update_system(camp, x.id, attrs={"port": "Q"})
    sim.rebuild()
    sim.step(1)


def test_polity_editing_mid_game(sim):
    camp = sim.camp
    unaligned = [x.id for x in camp.systems if x.polity is None][:3]
    p = edit.add_polity(camp, "Free Traders", color="#123456", capital=unaligned[0])
    assert all(f"{p.id}|" in k or f"|{p.id}" in k for k in camp.relations if p.id in k)
    claimed = edit.grow_polity(camp, p.id, reach=3)
    assert unaligned[0] in claimed
    assert all(next(x for x in camp.systems if x.id == s).polity == p.id for s in claimed)
    edit.assign(camp, unaligned[1:], p.id)
    sim.rebuild()
    members = np.array([x.polity == p.id for x in camp.systems])
    assert (sim.ix.polity_of == p.id).sum() == members.sum()
    # laws of the new polity apply immediately after rebuild
    camp.polities[-1].legality = {"arms": "illegal"}
    sim.refresh_legality()
    assert sim.illegal[members, sim.ix.good["arms"]].all()
    edit.update_polity(camp, p.id, name="Free Traders League", color="#654321")
    assert camp.polities[-1].name == "Free Traders League"
    sim.add_event(hx.Event(id="emb", type="embargo", name="e", start=camp.state.tick,
                           targets=hx.Targets(polities=[p.id]), params={"against": [camp.polities[0].id]}))
    edit.remove_polity(camp, p.id)
    _valid(camp)
    assert not any(x.polity == p.id for x in camp.systems)
    assert not any(p.id in k for k in camp.relations)
    assert p.id not in camp.events[-1].targets.polities
    sim.rebuild()
    sim.step(1)


def test_remove_sector(sim):
    camp = sim.camp
    sec = edit.add_sector(camp, "Gone", adjacent=("S1", "E"), width=12, height=12)
    edit.remove_sector(camp, sec.id)
    _valid(camp)
    assert not any(x.sector == sec.id for x in camp.systems) and camp.width == 16
    with pytest.raises(edit.EditError):
        edit.remove_sector(camp, "S1")


@pytest.mark.slow
def test_four_sector_galaxy_runs(setting):
    import time
    camp = hx.generate(setting, width=32, height=40, seed=1)
    edit.add_sector(camp, "B", adjacent=("S1", "E"))
    edit.add_sector(camp, "C", adjacent=("S1", "S"))
    edit.add_sector(camp, "D", adjacent=("S2", "S"))
    sim = hx.Simulation(camp)
    sim.warmup(4)
    t = time.time()
    sim.step(2)
    per_week = (time.time() - t) / 2
    print(f"\n4 sectors: {len(camp.systems)} systems, {per_week:.2f} s/week")
    assert np.isfinite(sim.prices).all()
