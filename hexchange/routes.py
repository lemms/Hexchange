"""Trade-route finder for a player ship.

For every system reachable within ``max_jumps`` jumps of the ship's jump
range, and every good, estimate the profit of buying at the origin and
selling at the destination.  Price impact is included: buying ``q`` units
lifts the origin price and selling them depresses the destination price, so
the optimal load is ``q* = margin / (1/depth_origin + 1/depth_dest)``, capped
by the hold and the origin's stock.
"""

from __future__ import annotations

import heapq
from dataclasses import asdict, dataclass


from . import hexgrid
from .economy import Simulation


@dataclass
class Route:
    origin: str
    destination: str
    good: str
    path: list[str]
    jumps: int
    offlane_jumps: int
    quantity: float
    tons: float
    buy: float
    sell: float
    margin: float
    profit: float
    risk: float
    legal: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _graph(sim: Simulation, jump: int, lanes_only: bool):
    c = sim.camp
    adj: dict[int, list[tuple[int, int, bool, float]]] = {i: [] for i in range(sim.N)}
    for e, ln in enumerate(c.lanes):
        if ln.length <= jump:
            i, j = sim.ix.sys[ln.a], sim.ix.sys[ln.b]
            adj[i].append((j, ln.length, False, ln.risk))
            adj[j].append((i, ln.length, False, ln.risk))
    if not lanes_only:
        r = c.setting.lanes.offlane_risk
        for a, b, d in zip(sim.off_a, sim.off_b, sim.off_len):
            if d <= jump:
                adj[int(a)].append((int(b), int(d), True, r))
                adj[int(b)].append((int(a), int(d), True, r))
    return adj


def reachable(sim: Simulation, origin: str, jump: int, max_jumps: int, lanes_only: bool):
    """Dijkstra by (jumps, distance): origin index -> {dest: (jumps, dist, off, risk, path)}."""
    adj = _graph(sim, jump, lanes_only)
    o = sim.ix.sys[origin]
    best: dict[int, tuple] = {}
    heap = [(0, 0, 0, 0.0, o, [o])]
    while heap:
        jumps, dist, off, risk, i, path = heapq.heappop(heap)
        if i in best:
            continue
        best[i] = (jumps, dist, off, risk, path)
        if jumps >= max_jumps:
            continue
        for j, d, is_off, r in adj[i]:
            if j not in best:
                heapq.heappush(heap, (jumps + 1, dist + d, off + is_off, 1 - (1 - risk) * (1 - r), j, path + [j]))
    best.pop(o, None)
    return best


def find_routes(sim: Simulation, origin: str, *, cargo_tons: float = 100.0, jump: int = 2,
                max_jumps: int = 3, lanes_only: bool = True, cost_per_ton_hex: float | None = None,
                include_illegal: bool = False, top: int = 25) -> list[Route]:
    c, s = sim.camp, sim.camp.setting
    cost_per_ton_hex = 0.5 * s.lanes.freight_rate if cost_per_ton_hex is None else cost_per_ton_hex
    o = sim.ix.sys[origin]
    prices = sim.prices
    stock = sim._arr("stock")
    out: list[Route] = []
    for dest, (jumps, dist, off, risk, path) in reachable(sim, origin, jump, max_jumps, lanes_only).items():
        for k, g in enumerate(s.goods):
            legal = not sim.illegal[dest, k] and not sim.illegal[o, k]
            if not legal and not include_illegal:
                continue
            buy = prices[o, k] * (1 + sim.spread(o))
            sell = prices[dest, k] * (1 - sim.spread(dest))
            unit_cost = g.tons_per_unit * cost_per_ton_hex * dist
            margin = sell * (1 - risk) - buy - unit_cost
            if margin <= 0:
                continue
            impact = 1 / sim.depth(o, k) + 1 / sim.depth(dest, k)
            q = margin / impact
            q = min(q, cargo_tons / g.tons_per_unit, max(0.0, float(stock[o, k])) + 1e-9)
            if q <= 0:
                continue
            profit = q * margin - 0.5 * q * q * impact
            out.append(Route(origin, c.systems[dest].id, g.id, [c.systems[i].id for i in path], jumps, off,
                             round(q, 2), round(q * g.tons_per_unit, 2), round(buy, 2), round(sell, 2),
                             round(margin, 2), round(profit, 2), round(risk, 4), legal))
    out.sort(key=lambda r: -r.profit)
    return out[:top]


def hex_distance(sim: Simulation, a: str, b: str) -> int:
    sa, sb = sim.camp.systems[sim.ix.sys[a]], sim.camp.systems[sim.ix.sys[b]]
    return hexgrid.distance((sa.col, sa.row), (sb.col, sb.row))
