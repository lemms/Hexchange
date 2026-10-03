"""Events as non-destructive modifiers.

Every tick the active events are folded into a :class:`Modifiers` object that
the economy applies on top of the unchanged base data, so removing or expiring
an event restores the galaxy exactly.

Charted-lane effects (wars, embargoes, tariffs, disruptions) never touch the
off-lane jump network: smugglers route around them, at their usual high cost
and risk.  Only piracy and explicit ``offlane_risk`` modifiers affect smugglers.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from .model import Campaign, Event, Targets, relation_key

EVENT_HELP = {
    "war": "targets.polities: the belligerents (2+). Border lanes lose capacity (params.capacity, default 0.1) "
           "and gain risk (params.risk 0.15); goods tagged 'military' see demand x params.demand (2.0) in "
           "their territory; border-system production x params.production (0.85). Smugglers are unaffected.",
    "embargo": "targets.polities/systems embargo params.against (polity ids; empty = everyone else). "
               "Charted lanes between them close for targets.goods/tags (empty = all). Smugglers are unaffected.",
    "tariff": "Like embargo, but adds params.rate (0.25) x price as a duty on crossing lanes.",
    "lane_disruption": "targets.lanes (or all lanes touching targets.systems) capacity x params.factor (0).",
    "piracy": "Lanes and off-lane jumps touching targets.systems/polities gain params.risk (0.1).",
    "disaster": "targets.systems/polities: production x params.production (0.5), demand x params.demand (1.0).",
    "boom": "targets.systems/polities: production x params.production (1.3), demand x params.demand (1.3).",
    "relations": "Set relation of targets.polities[0] and [1] to params.value (-1..1).",
    "legality": "In targets.systems/polities, targets.goods/tags become params.status: 'legal', 'illegal', "
                "or a number N (illegal where law level > N). E.g. martial law, legalisation. "
                "Banned goods can then only arrive by smuggling.",
    "modifier": "Generic: params.field in production|demand|lane_capacity|lane_risk|tariff|offlane_risk, "
                "params.op mul|add, params.value; applied to targets.",
    "player_action": "Same as modifier, recorded as caused by the players.",
}


@dataclass
class Modifiers:
    production: np.ndarray          # (N, G) multiplier
    demand: np.ndarray              # (N, G) multiplier
    lane_capacity: np.ndarray       # (E, G) multiplier
    lane_risk: np.ndarray           # (E,) additive
    lane_tariff: np.ndarray         # (E, G) additive fraction of price
    offlane_risk: np.ndarray        # (N,) additive, applies to jumps touching the system
    relations: dict[str, float] = field(default_factory=dict)
    legality: list[tuple[np.ndarray, np.ndarray, object]] = field(default_factory=list)  # (systems, goods, value)
    active: list[str] = field(default_factory=list)


class Index:
    """Lookups shared by events and the economy."""

    def __init__(self, camp: Campaign):
        self.sys = {s.id: i for i, s in enumerate(camp.systems)}
        self.good = {g.id: k for k, g in enumerate(camp.setting.goods)}
        self.lane = {ln.id: e for e, ln in enumerate(camp.lanes)}
        self.polity_of = np.array([s.polity or "" for s in camp.systems], dtype=object)
        self.lane_a = np.array([self.sys[ln.a] for ln in camp.lanes], dtype=int)
        self.lane_b = np.array([self.sys[ln.b] for ln in camp.lanes], dtype=int)
        self.tags = [set(g.tags) for g in camp.setting.goods]


def goods_mask(ix: Index, t: Targets, n_goods: int) -> np.ndarray:
    if not t.goods and not t.tags:
        return np.ones(n_goods, bool)
    m = np.zeros(n_goods, bool)
    for gid in t.goods:
        if gid in ix.good:
            m[ix.good[gid]] = True
    if t.tags:
        for k, tags in enumerate(ix.tags):
            if tags & set(t.tags):
                m[k] = True
    return m


def systems_mask(ix: Index, t: Targets, n: int, polities: list[str] | None = None) -> np.ndarray:
    m = np.zeros(n, bool)
    for sid in t.systems:
        if sid in ix.sys:
            m[ix.sys[sid]] = True
    for pid in (t.polities if polities is None else polities):
        m |= ix.polity_of == pid
    return m


def _crossing(ix: Index, side_a: np.ndarray, side_b: np.ndarray) -> np.ndarray:
    a, b = ix.lane_a, ix.lane_b
    return (side_a[a] & side_b[b]) | (side_b[a] & side_a[b])


def build_modifiers(camp: Campaign, tick: int, ix: Index | None = None) -> Modifiers:
    ix = ix or Index(camp)
    n, g, e = len(camp.systems), len(camp.setting.goods), len(camp.lanes)
    mod = Modifiers(np.ones((n, g)), np.ones((n, g)), np.ones((e, g)), np.zeros(e), np.zeros((e, g)),
                    np.zeros(n))
    for ev in camp.events:
        if not ev.active(tick):
            continue
        mod.active.append(ev.id)
        p, t = ev.params, ev.targets
        gm = goods_mask(ix, t, g)
        if ev.type == "war":
            pols = t.polities
            for i, pa in enumerate(pols):
                for pb in pols[i + 1:]:
                    sa = ix.polity_of == pa
                    sb = ix.polity_of == pb
                    cross = _crossing(ix, sa, sb)
                    mod.lane_capacity[cross] *= float(p.get("capacity", 0.1))
                    mod.lane_risk[cross] += float(p.get("risk", 0.15))
                    mod.relations[relation_key(pa, pb)] = -1.0
                    border = np.zeros(n, bool)
                    border[ix.lane_a[cross]] = True
                    border[ix.lane_b[cross]] = True
                    mod.production[border] *= float(p.get("production", 0.85))
            mil = np.array(["military" in tg for tg in ix.tags])
            terr = systems_mask(ix, Targets(), n, pols)
            mod.demand[np.ix_(terr, mil)] *= float(p.get("demand", 2.0))
        elif ev.type in ("embargo", "tariff"):
            side_a = systems_mask(ix, t, n)
            against = p.get("against") or []
            side_b = systems_mask(ix, Targets(), n, against) if against else ~side_a
            cross = _crossing(ix, side_a, side_b)
            if ev.type == "embargo":
                mod.lane_capacity[np.ix_(cross, gm)] = 0.0
                for pa in t.polities:
                    for pb in against:
                        k = relation_key(pa, pb)
                        mod.relations[k] = min(mod.relations.get(k, 1.0), -0.5)
            else:
                mod.lane_tariff[np.ix_(cross, gm)] += float(p.get("rate", 0.25))
        elif ev.type == "lane_disruption":
            lm = np.zeros(e, bool)
            for lid in t.lanes:
                if lid in ix.lane:
                    lm[ix.lane[lid]] = True
            sm = systems_mask(ix, t, n)
            lm |= sm[ix.lane_a] | sm[ix.lane_b]
            mod.lane_capacity[np.ix_(lm, gm)] *= float(p.get("factor", 0.0))
        elif ev.type == "piracy":
            sm = systems_mask(ix, t, n)
            r = float(p.get("risk", 0.1))
            mod.lane_risk[sm[ix.lane_a] | sm[ix.lane_b]] += r
            mod.offlane_risk[sm] += r
        elif ev.type in ("disaster", "boom"):
            sm = systems_mask(ix, t, n)
            dp, dd = (0.5, 1.0) if ev.type == "disaster" else (1.3, 1.3)
            mod.production[np.ix_(sm, gm)] *= float(p.get("production", dp))
            mod.demand[np.ix_(sm, gm)] *= float(p.get("demand", dd))
        elif ev.type == "legality":
            mod.legality.append((systems_mask(ix, t, n), gm, p.get("status", "illegal")))
        elif ev.type == "relations":
            if len(t.polities) >= 2:
                mod.relations[relation_key(t.polities[0], t.polities[1])] = float(p.get("value", 0.0))
        elif ev.type in ("modifier", "player_action"):
            _generic(mod, ix, ev, gm, n, e)
    return mod


def _generic(mod: Modifiers, ix: Index, ev: Event, gm: np.ndarray, n: int, e: int) -> None:
    p, t = ev.params, ev.targets
    fld, op, val = p.get("field", "production"), p.get("op", "mul"), float(p.get("value", 1.0))

    def apply(arr, idx):
        if op == "mul":
            arr[idx] *= val
        else:
            arr[idx] += val

    sm = systems_mask(ix, t, n)
    lm = np.zeros(e, bool)
    for lid in t.lanes:
        if lid in ix.lane:
            lm[ix.lane[lid]] = True
    if fld in ("lane_capacity", "lane_risk", "tariff") and not t.lanes:
        lm |= sm[ix.lane_a] | sm[ix.lane_b]
    if fld == "production":
        apply(mod.production, np.ix_(sm, gm))
    elif fld == "demand":
        apply(mod.demand, np.ix_(sm, gm))
    elif fld == "lane_capacity":
        apply(mod.lane_capacity, np.ix_(lm, gm))
    elif fld == "lane_risk":
        apply(mod.lane_risk, lm)
    elif fld == "tariff":
        apply(mod.lane_tariff, np.ix_(lm, gm))
    elif fld == "offlane_risk":
        apply(mod.offlane_risk, sm)


def random_events(camp: Campaign, tick: int, rng: random.Random) -> list[Event]:
    """Roll the setting's random-event presets for one tick (1% chance per unit weight)."""
    out = []
    pols = [p.id for p in camp.polities]
    for k, pre in enumerate(camp.setting.random_events):
        if rng.random() >= 0.01 * pre.weight:
            continue
        t = Targets()
        if pre.type in ("war", "embargo", "tariff", "relations"):
            if len(pols) < 2:
                continue
            a, b = rng.sample(pols, 2)
            t.polities = [a] if pre.type in ("embargo", "tariff") else [a, b]
            params = dict(pre.params)
            if pre.type in ("embargo", "tariff"):
                params["against"] = [b]
        else:
            s = rng.choice(camp.systems)
            if pre.type == "lane_disruption":
                lanes = [ln.id for ln in camp.lanes if s.id in (ln.a, ln.b)]
                if not lanes:
                    continue
                t.lanes = [rng.choice(lanes)]
            else:
                t.systems = [s.id]
            params = dict(pre.params)
        # presets may aim at particular goods, e.g. martial law on weapons
        t.goods = list(params.pop("goods", []))
        t.tags = list(params.pop("tags", []))
        dur = rng.randint(*pre.duration)
        out.append(Event(id=f"R{tick}-{k}", type=pre.type, name=f"{pre.type.replace('_', ' ').title()} (random)",
                         start=tick, duration=dur, targets=t, params=params, source="random"))
    return out
