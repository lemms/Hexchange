"""Random galaxy generation driven entirely by a Setting."""

from __future__ import annotations

import colorsys
import random
import re
from collections import deque

from . import hexgrid, lanes as lanegen
from .model import (AttributeDef, Campaign, Condition, Options, Polity, Sector, Setting, StarSystem,
                    relation_key)

_DICE = re.compile(r"^\s*(\d+)\s*d\s*(\d+)\s*([+-]\s*\d+)?\s*$")


def roll(expr: str, rng: random.Random) -> int:
    expr = expr.strip()
    m = _DICE.match(expr)
    if not m:
        return int(expr)
    n, sides, k = int(m.group(1)), int(m.group(2)), m.group(3)
    return sum(rng.randint(1, sides) for _ in range(n)) + (int(k.replace(" ", "")) if k else 0)


def numeric(attr: AttributeDef, value: int | str) -> float:
    """Category values compare by their position in ``values``."""
    if attr.kind == "category":
        return float(attr.values.index(value)) if value in attr.values else -1.0
    return float(value)


def check(cond: Condition, setting: Setting, attrs: dict[str, int | str]) -> bool:
    if cond.attr not in attrs:
        return False
    v = attrs[cond.attr]
    if cond.values is not None and v not in cond.values:
        return False
    x = numeric(setting.attr(cond.attr), v)
    if cond.min is not None and x < cond.min:
        return False
    if cond.max is not None and x > cond.max:
        return False
    return True


_RANGE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$")


def _table_lookup(table: dict[str, str] | dict[str, float], key: int | str):
    """Exact key, then numeric ranges like '3-4' or '-5-2', then '*'."""
    k = str(key)
    if k in table:
        return table[k]
    if isinstance(key, int):
        for tk, tv in table.items():
            m = _RANGE.match(tk)
            if m and int(m.group(1)) <= key <= int(m.group(2)):
                return tv
    return table.get("*")


def roll_attributes(setting: Setting, rng: random.Random) -> dict[str, int | str]:
    attrs: dict[str, int | str] = {}
    for a in setting.attributes:
        total = roll(a.roll, rng) + a.add
        for m in a.mods:
            if m.attr not in attrs:
                continue
            other = attrs[m.attr]
            if m.table is not None:
                total += _table_lookup(m.table, other) or 0
            else:
                total += m.factor * (numeric(setting.attr(m.attr), other) + m.offset)
        total = int(round(total))
        if a.kind == "category":
            value: int | str = _table_lookup(a.roll_table or {}, total) or (a.values[0] if a.values else "")
        else:
            value = max(a.min, min(a.max, total))
        attrs[a.key] = value
        for rule in a.rules:
            if all(check(c, setting, attrs) for c in rule.when):
                if rule.set is not None:
                    attrs[a.key] = rule.set
                elif rule.add is not None and a.kind == "int":
                    attrs[a.key] = max(a.min, min(a.max, int(attrs[a.key]) + int(rule.add)))
    return attrs


def classify(setting: Setting, attrs: dict[str, int | str]) -> list[str]:
    return [c.code for c in setting.codes if c.all and all(check(x, setting, attrs) for x in c.all)]


_EHEX = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def encode(attr: AttributeDef, value: int | str) -> str:
    if attr.kind == "category" or attr.encoding == "raw":
        return str(value)
    if attr.encoding == "ehex":
        v = int(value)
        return _EHEX[v] if 0 <= v < len(_EHEX) else "?"
    return str(value)


def profile(setting: Setting, attrs: dict[str, int | str]) -> str:
    out = setting.profile
    for a in setting.attributes:
        if a.key in attrs:
            out = out.replace("{" + a.key + "}", encode(a, attrs[a.key]))
    return out


def make_name(setting: Setting, rng: random.Random, used: set[str]) -> str:
    syl = setting.name_syllables
    for _ in range(200):
        n = rng.choice((2, 2, 2, 3, 3))
        name = "".join(rng.choice(syl) for _ in range(n)).capitalize()
        if name not in used and 3 <= len(name) <= 12:
            used.add(name)
            return name
    name = f"System-{len(used)}"
    used.add(name)
    return name


def _colors(n: int, rng: random.Random) -> list[str]:
    out = []
    for i in range(n):
        h = (i / max(1, n) + rng.random() * 0.05) % 1.0
        r, g, b = colorsys.hls_to_rgb(h, 0.55, 0.6)
        out.append(f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}")
    return out


def assign_polities(camp: Campaign, n: int, rng: random.Random, reach: int = 10) -> None:
    """Grow ``n`` polities outwards from populous capitals over the lane graph."""
    setting = camp.setting
    pop = setting.roles.population
    if n <= 0 or not camp.systems:
        return
    by_id = {s.id: s for s in camp.systems}
    adj: dict[str, list[tuple[str, int]]] = {s.id: [] for s in camp.systems}
    for ln in camp.lanes:
        adj[ln.a].append((ln.b, ln.length))
        adj[ln.b].append((ln.a, ln.length))
    cands = sorted(camp.systems, key=lambda s: -float(s.attrs.get(pop, 0)) - rng.random())
    capitals: list[StarSystem] = []
    for s in cands:
        if all(hexgrid.distance((s.col, s.row), (c.col, c.row)) > reach for c in capitals):
            capitals.append(s)
        if len(capitals) == n:
            break
    names = list(setting.polity_names)
    rng.shuffle(names)
    colors = _colors(len(capitals), rng)
    owner: dict[str, tuple[int, str]] = {}
    queue: deque[tuple[str, int, str]] = deque()
    for i, cap in enumerate(capitals):
        pid = f"P{i + 1}"
        name = names[i] if i < len(names) else f"{cap.name} {rng.choice(['League', 'Union', 'Concord', 'Hegemony', 'Compact'])}"
        camp.polities.append(Polity(id=pid, name=name, color=colors[i], capital=cap.id))
        owner[cap.id] = (0, pid)
        queue.append((cap.id, 0, pid))
    while queue:                       # breadth-first, nearest capital wins
        sid, d, pid = queue.popleft()
        if owner.get(sid, (1 << 30, ""))[1] != pid:
            continue
        for nb, length in adj[sid]:
            nd = d + length
            if nd <= reach and nd < owner.get(nb, (1 << 30, ""))[0]:
                owner[nb] = (nd, pid)
                queue.append((nb, nd, pid))
    for sid, (_, pid) in owner.items():
        by_id[sid].polity = pid
    for pol in camp.polities:
        for preset in setting.polity_legality:
            if not preset.options:
                continue
            opt = rng.choices(preset.options, weights=[o.weight for o in preset.options])[0]
            for gid in preset.goods:
                pol.legality[gid] = opt.value
            for tag in preset.tags:
                pol.legality[f"tag:{tag}"] = opt.value
    for i, a in enumerate(camp.polities):
        for b in camp.polities[i + 1:]:
            camp.relations[relation_key(a.id, b.id)] = round(rng.uniform(-0.3, 0.7), 2)


def generate(setting: Setting, *, name: str = "New Sector", width: int = 32, height: int = 40,
             density: float = 0.4, polities: int = 4, seed: int = 0) -> Campaign:
    rng = random.Random(seed)
    systems: list[StarSystem] = []
    used: set[str] = set()
    for col in range(width):
        for row in range(height):
            if rng.random() >= density:
                continue
            attrs = roll_attributes(setting, rng)
            systems.append(StarSystem(id=hexgrid.label(col, row), name=make_name(setting, rng, used),
                                      col=col, row=row, attrs=attrs, codes=classify(setting, attrs)))
    camp = Campaign(name=name, width=width, height=height, setting=setting, systems=systems,
                    sectors=[Sector(id="S1", name=name, width=width, height=height)],
                    options=Options(seed=seed))
    camp.lanes = lanegen.build_lanes(camp, rng)
    assign_polities(camp, polities, rng)
    return camp
