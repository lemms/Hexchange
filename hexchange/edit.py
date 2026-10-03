"""GM editing of the galaxy: sectors, systems, lanes and political regions.

Every function edits a :class:`~hexchange.model.Campaign` in place and keeps it
valid (no dangling references).  If a :class:`~hexchange.economy.Simulation`
is running on the campaign, call ``sim.rebuild()`` afterwards; it keeps the
market state of everything that still exists.
"""

from __future__ import annotations

import colorsys
import random

from . import hexgrid
from .generate import classify, make_name, roll_attributes
from .lanes import build_lanes, lane_id, make_lane
from .model import Campaign, Lane, Polity, Sector, StarSystem, relation_key

DIRECTIONS = {"E": (1, 0), "W": (-1, 0), "S": (0, 1), "N": (0, -1)}


class EditError(ValueError):
    pass


def _sector(camp: Campaign, sid: str) -> Sector:
    sec = next((s for s in camp.sectors if s.id == sid), None)
    if sec is None:
        raise EditError(f"unknown sector {sid!r}")
    return sec


def _system(camp: Campaign, sid: str) -> StarSystem:
    x = next((s for s in camp.systems if s.id == sid), None)
    if x is None:
        raise EditError(f"unknown system {sid!r}")
    return x


def _polity(camp: Campaign, pid: str) -> Polity:
    p = next((p for p in camp.polities if p.id == pid), None)
    if p is None:
        raise EditError(f"unknown polity {pid!r}")
    return p


def system_id(camp: Campaign, sector: Sector, col: int, row: int) -> str:
    """The first sector keeps plain hex labels (back-compatible); others are prefixed."""
    label = sector.label(col, row)
    return label if sector.id == camp.sectors[0].id else f"{sector.id}-{label}"


def sector_at(camp: Campaign, col: int, row: int) -> Sector | None:
    return next((s for s in camp.sectors if s.contains(col, row)), None)


def _extent(camp: Campaign) -> None:
    camp.width = max((s.col0 + s.width for s in camp.sectors), default=camp.width)
    camp.height = max((s.row0 + s.height for s in camp.sectors), default=camp.height)


def _shift(camp: Campaign, dc: int, dr: int) -> None:
    """Move everything so all coordinates stay non-negative (dc kept even)."""
    for s in camp.sectors:
        s.col0 += dc
        s.row0 += dr
    for x in camp.systems:
        x.col += dc
        x.row += dr


# --------------------------------------------------------------------------- sectors

def add_sector(camp: Campaign, name: str, *, adjacent: tuple[str, str] | None = None,
               at: tuple[int, int] | None = None, width: int = 32, height: int = 40,
               mode: str = "random", density: float = 0.4, polities: int = 0,
               seed: int | None = None) -> Sector:
    """Add a sector next to another (``adjacent=(sector_id, 'E'|'W'|'N'|'S')``) or at a
    global position.  ``mode='random'`` rolls systems and connects them to the existing
    lane network (existing lanes are never changed); ``'blank'`` adds an empty sector."""
    if width % 2:
        width += 1                                  # keep hex column parity aligned
    if adjacent:
        ref = _sector(camp, adjacent[0])
        d = adjacent[1].upper()
        if d not in DIRECTIONS:
            raise EditError("direction must be one of N, S, E, W")
        dc, dr = DIRECTIONS[d]
        col0 = ref.col0 + (ref.width if dc > 0 else -width if dc < 0 else 0)
        row0 = ref.row0 + (ref.height if dr > 0 else -height if dr < 0 else 0)
    elif at:
        col0, row0 = at
    else:
        raise EditError("give adjacent=(sector, direction) or at=(col, row)")
    col0 -= col0 % 2
    new = Sector(id=_next_id("S", [s.id for s in camp.sectors]), name=name, col0=col0, row0=row0,
                 width=width, height=height)
    for s in camp.sectors:
        if (new.col0 < s.col0 + s.width and s.col0 < new.col0 + new.width
                and new.row0 < s.row0 + s.height and s.row0 < new.row0 + new.height):
            raise EditError(f"overlaps sector {s.name}")
    camp.sectors.append(new)
    dc = -min(0, new.col0)
    dc += dc % 2
    dr = -min(0, new.row0)
    if dc or dr:
        _shift(camp, dc, dr)
    _extent(camp)
    if mode == "random":
        rng = random.Random(camp.options.seed * 7919 + len(camp.sectors) if seed is None else seed)
        used = {x.name for x in camp.systems}
        added = []
        for c in range(new.col0, new.col0 + new.width):
            for r in range(new.row0, new.row0 + new.height):
                if rng.random() >= density:
                    continue
                attrs = roll_attributes(camp.setting, rng)
                x = StarSystem(id=system_id(camp, new, c, r), name=make_name(camp.setting, rng, used),
                               sector=new.id, col=c, row=r, attrs=attrs, codes=classify(camp.setting, attrs))
                camp.systems.append(x)
                added.append(x.id)
        camp.lanes.extend(build_lanes(camp, rng, only=set(added)))
        if polities:
            _new_polities(camp, added, polities, rng)
    elif mode != "blank":
        raise EditError("mode must be 'random' or 'blank'")
    return new


def update_sector(camp: Campaign, sid: str, name: str | None = None, notes: str | None = None) -> Sector:
    sec = _sector(camp, sid)
    if name:
        sec.name = name
    if notes is not None:
        sec.notes = notes
    return sec


def remove_sector(camp: Campaign, sid: str) -> None:
    if len(camp.sectors) == 1:
        raise EditError("cannot remove the only sector")
    _sector(camp, sid)
    for x in [x for x in camp.systems if x.sector == sid]:
        remove_system(camp, x.id)
    camp.sectors = [s for s in camp.sectors if s.id != sid]
    _extent(camp)


# --------------------------------------------------------------------------- systems

def add_system(camp: Campaign, col: int, row: int, *, name: str | None = None,
               attrs: dict | None = None, seed: int | None = None) -> StarSystem:
    """Create a system on an empty hex.  Attributes not given are rolled from the setting."""
    sec = sector_at(camp, col, row)
    if sec is None:
        raise EditError("that hex is not inside any sector")
    if any(x.col == col and x.row == row for x in camp.systems):
        raise EditError("that hex already has a system")
    rng = random.Random(hash((col, row, seed)) if seed is not None else None)
    rolled = roll_attributes(camp.setting, rng)
    merged = _clean_attrs(camp, {**rolled, **(attrs or {})})
    used = {x.name for x in camp.systems}
    x = StarSystem(id=system_id(camp, sec, col, row), name=name or make_name(camp.setting, rng, used),
                   sector=sec.id, col=col, row=row, attrs=merged, codes=classify(camp.setting, merged))
    camp.systems.append(x)
    return x


def _clean_attrs(camp: Campaign, attrs: dict) -> dict:
    out = {}
    for a in camp.setting.attributes:
        if a.key not in attrs:
            continue
        v = attrs[a.key]
        if a.kind == "category":
            if str(v) not in a.values:
                raise EditError(f"{a.name} must be one of {a.values}")
            out[a.key] = str(v)
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise EditError(f"{a.name} must be a whole number")
            out[a.key] = max(a.min, min(a.max, v))
    return out


def update_system(camp: Campaign, sid: str, *, name: str | None = None, attrs: dict | None = None,
                  notes: str | None = None) -> StarSystem:
    x = _system(camp, sid)
    if name:
        x.name = name
    if notes is not None:
        x.notes = notes
    if attrs:
        x.attrs = {**x.attrs, **_clean_attrs(camp, attrs)}
        x.codes = classify(camp.setting, x.attrs)
        port = camp.setting.ports.get(str(x.attrs.get(camp.setting.roles.port)))
        if port is not None and not port.lanes:      # e.g. starport X: no charted lanes
            camp.lanes = [ln for ln in camp.lanes if sid not in (ln.a, ln.b)]
        else:                                          # port size changes lane capacity
            for i, ln in enumerate(camp.lanes):
                if sid in (ln.a, ln.b):
                    fresh = make_lane(camp, ln.a, ln.b, ln.length)
                    camp.lanes[i] = ln.model_copy(update={"capacity": fresh.capacity})
    return x


def reroll_system(camp: Campaign, sid: str, seed: int | None = None) -> StarSystem:
    rng = random.Random(seed)
    return update_system(camp, sid, attrs=roll_attributes(camp.setting, rng))


def remove_system(camp: Campaign, sid: str) -> None:
    _system(camp, sid)
    camp.systems = [x for x in camp.systems if x.id != sid]
    camp.lanes = [ln for ln in camp.lanes if sid not in (ln.a, ln.b)]
    for ev in camp.events:
        ev.targets.systems = [s for s in ev.targets.systems if s != sid]
    pv = camp.player_view
    pv.visible_systems = [s for s in pv.visible_systems if s != sid]
    pv.knowledge.pop(sid, None)
    if pv.location == sid:
        pv.location = None
    for p in camp.polities:
        if p.capital == sid:
            p.capital = None
    camp.state.pending_trades = [t for t in camp.state.pending_trades if t.system != sid]


# --------------------------------------------------------------------------- lanes

def add_lane(camp: Campaign, a: str, b: str, *, capacity: float | None = None,
             risk: float | None = None, toll: float | None = None) -> Lane:
    if a == b:
        raise EditError("a lane needs two different systems")
    _system(camp, a)
    _system(camp, b)
    lid = lane_id(a, b)
    if any(ln.id == lid for ln in camp.lanes):
        raise EditError("those systems are already connected")
    ln = make_lane(camp, a, b)
    upd = {k: v for k, v in (("capacity", capacity), ("risk", risk), ("toll", toll)) if v is not None}
    ln = ln.model_copy(update=upd)
    camp.lanes.append(ln)
    return ln


def update_lane(camp: Campaign, lid: str, *, capacity: float | None = None, risk: float | None = None,
                toll: float | None = None) -> Lane:
    for i, ln in enumerate(camp.lanes):
        if ln.id == lid:
            upd = {k: float(v) for k, v in (("capacity", capacity), ("risk", risk), ("toll", toll)) if v is not None}
            if upd.get("capacity", 0) < 0 or not 0 <= upd.get("risk", 0) <= 1:
                raise EditError("capacity must be >= 0 and risk between 0 and 1")
            camp.lanes[i] = ln.model_copy(update=upd)
            return camp.lanes[i]
    raise EditError(f"unknown lane {lid!r}")


def remove_lane(camp: Campaign, lid: str) -> None:
    if not any(ln.id == lid for ln in camp.lanes):
        raise EditError(f"unknown lane {lid!r}")
    camp.lanes = [ln for ln in camp.lanes if ln.id != lid]
    for ev in camp.events:
        ev.targets.lanes = [x for x in ev.targets.lanes if x != lid]


def auto_lanes(camp: Campaign, systems: list[str], seed: int | None = None) -> list[Lane]:
    """Generate lanes for these systems, keeping all existing lanes."""
    new = build_lanes(camp, random.Random(seed), only=set(systems))
    camp.lanes.extend(new)
    return new


# --------------------------------------------------------------------------- polities

def _next_id(prefix: str, existing: list[str]) -> str:
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"


def _new_color(camp: Campaign) -> str:
    rng = random.Random(len(camp.polities))
    used = len(camp.polities)
    h = (used * 0.61803398875 + rng.random() * 0.05) % 1.0      # golden-ratio hues stay distinct
    r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.6)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def add_polity(camp: Campaign, name: str, *, color: str | None = None, capital: str | None = None,
               roll_laws: bool = True, seed: int | None = None) -> Polity:
    if capital is not None:
        _system(camp, capital)
    p = Polity(id=_next_id("P", [p.id for p in camp.polities]), name=name,
               color=color or _new_color(camp), capital=capital)
    if roll_laws:
        rng = random.Random(seed)
        for preset in camp.setting.polity_legality:
            if preset.options:
                opt = rng.choices(preset.options, weights=[o.weight for o in preset.options])[0]
                for gid in preset.goods:
                    p.legality[gid] = opt.value
                for tag in preset.tags:
                    p.legality[f"tag:{tag}"] = opt.value
    for other in camp.polities:
        camp.relations[relation_key(p.id, other.id)] = 0.0
    camp.polities.append(p)
    if capital is not None:
        _system(camp, capital).polity = p.id
    return p


def update_polity(camp: Campaign, pid: str, *, name: str | None = None, color: str | None = None,
                  capital: str | None = None) -> Polity:
    p = _polity(camp, pid)
    if name:
        p.name = name
    if color:
        p.color = color
    if capital is not None:
        if capital == "":
            p.capital = None
        else:
            _system(camp, capital).polity = pid
            p.capital = capital
    return p


def remove_polity(camp: Campaign, pid: str) -> None:
    _polity(camp, pid)
    camp.polities = [p for p in camp.polities if p.id != pid]
    for x in camp.systems:
        if x.polity == pid:
            x.polity = None
    camp.relations = {k: v for k, v in camp.relations.items() if pid not in k.split("|")}
    for ev in camp.events:
        ev.targets.polities = [p for p in ev.targets.polities if p != pid]
        if "against" in ev.params:
            ev.params["against"] = [p for p in ev.params["against"] if p != pid]


def assign(camp: Campaign, systems: list[str], pid: str | None) -> None:
    """Put systems into a polity (or make them unaligned with ``pid=None``)."""
    if pid is not None:
        _polity(camp, pid)
    for sid in systems:
        x = _system(camp, sid)
        old = x.polity
        x.polity = pid
        for p in camp.polities:                    # a capital that changes hands stops being one
            if p.capital == sid and p.id != pid and old == p.id:
                p.capital = None


def grow_polity(camp: Campaign, pid: str, reach: int, *, start: str | None = None,
                overwrite: bool = False) -> list[str]:
    """Claim systems within ``reach`` lane-distance of ``start`` (default: the capital).
    Systems of other polities are only taken with ``overwrite``.  Returns claimed ids."""
    p = _polity(camp, pid)
    start = start or p.capital
    if start is None:
        raise EditError("polity has no capital; give a start system")
    _system(camp, start)
    adj: dict[str, list[tuple[str, int]]] = {x.id: [] for x in camp.systems}
    for ln in camp.lanes:
        adj[ln.a].append((ln.b, ln.length))
        adj[ln.b].append((ln.a, ln.length))
    by = {x.id: x for x in camp.systems}
    best = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for sid in frontier:
            for nb, d in adj[sid]:
                nd = best[sid] + d
                if nd <= reach and nd < best.get(nb, 1 << 30):
                    best[nb] = nd
                    nxt.append(nb)
        frontier = nxt
    claimed = [sid for sid in best
               if overwrite or by[sid].polity in (None, pid)]
    assign(camp, claimed, pid)
    return claimed


def _new_polities(camp: Campaign, system_ids: list[str], n: int, rng: random.Random, reach: int = 10) -> None:
    """Found ``n`` polities among the given (new) systems, grown over unaligned space."""
    pop = camp.setting.roles.population
    cands = sorted((x for x in camp.systems if x.id in set(system_ids) and x.polity is None),
                   key=lambda x: -float(x.attrs.get(pop, 0)) - rng.random())
    capitals: list[StarSystem] = []
    for x in cands:
        if all(hexgrid.distance((x.col, x.row), (c.col, c.row)) > reach for c in capitals):
            capitals.append(x)
        if len(capitals) == n:
            break
    names = [nm for nm in camp.setting.polity_names if nm not in {p.name for p in camp.polities}]
    rng.shuffle(names)
    for i, cap in enumerate(capitals):
        name = names[i] if i < len(names) else f"{cap.name} Compact"
        p = add_polity(camp, name, capital=cap.id, seed=rng.randrange(1 << 30))
        grow_polity(camp, p.id, reach)
