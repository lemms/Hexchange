"""Charted hyperlane network.

1. Candidate links: pairs of lane-capable systems within ``max_jump`` hexes.
2. Backbone: minimum spanning forest of the candidates, weighted by length
   divided by importance, so every system that *can* be reached is.
3. Extra lanes: edges of the relative neighbourhood graph (a sparse, planar
   subgraph of the Delaunay triangulation, so lanes don't cross needlessly)
   added with a probability that grows with the "gravity" of the pair --
   importance product over length squared.  Degree is capped.

Uncharted (off-lane) jumps are not stored: any pair within ``max_jump`` can be
used by smugglers, see :mod:`hexchange.economy`.
"""

from __future__ import annotations

import random

import numpy as np
from scipy.spatial import Delaunay

from . import hexgrid
from .model import Campaign, Lane


def importance(camp: Campaign) -> dict[str, float]:
    s = camp.setting
    out = {}
    for sys_ in camp.systems:
        port = s.ports.get(str(sys_.attrs.get(s.roles.port)))
        pop = float(sys_.attrs.get(s.roles.population, 0) or 0)
        out[sys_.id] = (port.weight if port else 0.0) * (1.0 + pop) ** 0.5
    return out


def lane_id(a: str, b: str) -> str:
    return "-".join(sorted((a, b)))


def candidate_pairs(camp: Campaign, eligible: list[int], max_jump: int) -> list[tuple[int, int, int]]:
    pos = {(camp.systems[i].col, camp.systems[i].row): i for i in eligible}
    out = []
    for i in eligible:
        s = camp.systems[i]
        for c, r in hexgrid.within(s.col, s.row, max_jump, camp.width, camp.height):
            j = pos.get((c, r))
            if j is not None and j > i:
                out.append((i, j, hexgrid.distance((s.col, s.row), (c, r))))
    return out


def _rng_edges(points: np.ndarray, idx: list[int]) -> set[tuple[int, int]]:
    """Relative neighbourhood graph edges (as original indices) via Delaunay."""
    if len(idx) < 3:
        return {(idx[0], idx[1])} if len(idx) == 2 else set()
    try:
        tri = Delaunay(points)
    except Exception:                    # collinear etc.
        return set()
    edges = set()
    for simplex in tri.simplices:
        for a in range(3):
            u, v = sorted((simplex[a], simplex[(a + 1) % 3]))
            edges.add((u, v))
    nbrs: dict[int, set[int]] = {}
    for u, v in edges:
        nbrs.setdefault(u, set()).add(v)
        nbrs.setdefault(v, set()).add(u)
    out = set()
    for u, v in edges:
        d = np.linalg.norm(points[u] - points[v])
        blocked = False
        for w in nbrs[u] | nbrs[v]:     # RNG witnesses are always Delaunay neighbours
            if w in (u, v):
                continue
            if max(np.linalg.norm(points[u] - points[w]), np.linalg.norm(points[v] - points[w])) < d - 1e-9:
                blocked = True
                break
        if not blocked:
            out.add(tuple(sorted((idx[u], idx[v]))))
    return out


def make_lane(camp: Campaign, a_id: str, b_id: str, length: int | None = None) -> Lane:
    """A lane with capacity and risk from the setting's ports and lane parameters."""
    s = camp.setting
    p = s.lanes
    by = {x.id: x for x in camp.systems}
    a, b = by[a_id], by[b_id]
    if length is None:
        length = hexgrid.distance((a.col, a.row), (b.col, b.row))
    pa = s.ports.get(str(a.attrs.get(s.roles.port)))
    pb = s.ports.get(str(b.attrs.get(s.roles.port)))
    cap = min(pa.capacity if pa else 0.0, pb.capacity if pb else 0.0)
    return Lane(id=lane_id(a.id, b.id), a=a.id, b=b.id, length=int(length),
                capacity=round(p.base_capacity * s.volume_scale * max(cap, 0.1), 1),
                risk=round(p.lane_risk * length, 4))


def build_lanes(camp: Campaign, rng: random.Random, only: set[str] | None = None) -> list[Lane]:
    """New lanes for the campaign.

    With ``only`` (system ids), existing lanes are kept fixed: systems they already
    connect count as joined, and every new lane touches at least one system in
    ``only`` -- used when adding a sector or systems to a running galaxy.
    Returns the new lanes only.
    """
    s = camp.setting
    p = s.lanes
    eligible = [i for i, sy in enumerate(camp.systems)
                if (pc := s.ports.get(str(sy.attrs.get(s.roles.port)))) is not None and pc.lanes]
    if len(eligible) < 2:
        return []
    ids = [x.id for x in camp.systems]
    pos = {sid: i for i, sid in enumerate(ids)}
    focus = None if only is None else {pos[x] for x in only if x in pos}
    imp = importance(camp)
    cands = [c for c in candidate_pairs(camp, eligible, p.max_jump)
             if focus is None or c[0] in focus or c[1] in focus]
    if not cands:
        return []
    n = len(camp.systems)
    length = {(i, j): d for i, j, d in cands}
    existing = {tuple(sorted((pos[ln.a], pos[ln.b]))) for ln in camp.lanes if ln.a in pos and ln.b in pos}

    # backbone: Kruskal minimum spanning forest, cheaper between important systems,
    # starting from the components the existing lanes already form
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in existing:
        parent[find(i)] = find(j)
    weight = {(i, j): d / (0.5 + (imp[ids[i]] * imp[ids[j]]) ** 0.25) for i, j, d in cands}
    chosen: set[tuple[int, int]] = set()
    for (i, j) in sorted(weight, key=lambda e: (weight[e], e)):
        ri, rj = find(i), find(j)
        if ri != rj and (i, j) not in existing:
            parent[ri] = rj
            chosen.add((i, j))
    degree = {i: 0 for i in range(n)}
    for i, j in chosen | existing:
        degree[i] += 1
        degree[j] += 1

    # extra lanes from the relative neighbourhood graph, by gravity
    pts = np.array([hexgrid.center(camp.systems[i].col, camp.systems[i].row) for i in eligible])
    extra = [e for e in _rng_edges(pts, eligible)
             if e in length and e not in chosen and e not in existing]
    grav = {e: imp[ids[e[0]]] * imp[ids[e[1]]] / length[e] ** 2 for e in extra}
    med = float(np.median(list(grav.values()))) if grav else 1.0
    for e in sorted(extra, key=lambda e: (-grav[e], e)):
        i, j = e
        if degree[i] >= p.max_degree or degree[j] >= p.max_degree:
            continue
        prob = min(0.95, p.extra_edges * (grav[e] / med) ** 0.5)
        if rng.random() < prob:
            chosen.add(e)
            degree[i] += 1
            degree[j] += 1

    return [make_lane(camp, ids[i], ids[j], length[(i, j)]) for i, j in sorted(chosen)]
