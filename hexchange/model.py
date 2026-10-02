"""Data model and JSON file format.

Two documents:

* **Setting** (``*.setting.json``): everything genre-specific -- world
  attributes and how they are rolled, classification codes, port classes,
  goods, name syllables, generation and event presets.  The engine itself
  knows nothing about any particular game.
* **Campaign** (``*.hexchange.json``): one galaxy -- an embedded copy of its
  setting, the generated systems, polities and lanes, the DM's events, and the
  simulation state.

Both are pydantic models, so loading validates the file and
``hexchange schema`` exports JSON Schema documents for editors.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- setting

class Condition(_Model):
    """Test on one world attribute: ``min <= value <= max`` and/or ``value in values``."""

    attr: str
    min: float | None = None
    max: float | None = None
    values: list[int | str] | None = None


class AttrMod(_Model):
    """Modifier added to an attribute roll, from another attribute.

    Numeric: ``factor * (other + offset)``.  Table: ``table[str(other)]`` (key
    ``"*"`` is the default)."""

    attr: str
    factor: float = 1.0
    offset: float = 0.0
    table: dict[str, float] | None = None


class AttrRule(_Model):
    """Override applied after rolling: when all conditions hold, set (or add)."""

    when: list[Condition]
    set: int | str | None = None
    add: float | None = None


class AttributeDef(_Model):
    key: str
    name: str
    kind: Literal["int", "category"] = "int"
    min: int = 0
    max: int = 15
    values: list[str] = Field(default_factory=list, description="category values, in order")
    roll: str = Field("2d6", description="dice expression, e.g. '2d6', '1d6', '0'")
    add: float = 0
    mods: list[AttrMod] = Field(default_factory=list)
    roll_table: dict[str, str] | None = Field(
        None, description="category attributes: roll result -> value ('*' default, ranges like '3-4')")
    rules: list[AttrRule] = Field(default_factory=list)
    encoding: Literal["int", "ehex", "raw"] = "int"
    descriptions: dict[str, str] = Field(default_factory=dict)


class CodeDef(_Model):
    code: str
    name: str
    all: list[Condition] = Field(default_factory=list, description="all must hold")


class PortClass(_Model):
    capacity: float = Field(1.0, description="multiplier on lane capacity at this port")
    lanes: bool = Field(True, description="whether charted lanes may end here")
    weight: float = Field(1.0, description="importance in lane generation")
    fees: float = Field(0.0, description="port fee per ton handled, currency units")


class Roles(_Model):
    """Which attributes carry economic meaning."""

    population: str
    population_weight_base: float = Field(
        2.0, description="economic weight = base ** population attribute (keeps huge worlds from swamping)")
    tech: str
    port: str
    law: str | None = None


class Production(_Model):
    base: float = Field(0.0, description="units/week at economic weight 1, before code multipliers")
    codes: dict[str, float] = Field(default_factory=dict, description="code -> multiplier; missing codes count 1")
    requires_any: list[str] = Field(default_factory=list, description="produced only on worlds with one of these codes")
    min_tech: int = 0


class Demand(_Model):
    base: float = Field(0.0, description="units/week at economic weight 1")
    codes: dict[str, float] = Field(default_factory=dict)
    min_tech: int = 0
    elasticity: float = 1.2


class Good(_Model):
    id: str
    name: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    base_price: float
    tons_per_unit: float = 1.0
    inputs: dict[str, float] = Field(default_factory=dict, description="recipe: input good -> units per unit")
    production: Production = Field(default_factory=Production)
    demand: Demand = Field(default_factory=Demand)
    illegal_above_law: int | None = Field(None, description="contraband where law attribute exceeds this")


class LaneParams(_Model):
    max_jump: int = 4
    extra_edges: float = Field(0.35, description="probability scale for non-tree lanes")
    max_degree: int = 6
    base_capacity: float = Field(4000.0, description="tons/week for a lane between average ports")
    freight_rate: float = Field(150.0, description="currency per ton per hex")
    offlane_factor: float = Field(3.0, description="freight multiplier for uncharted jumps")
    offlane_capacity: float = Field(0.08, description="fraction of base capacity available off-lane")
    offlane_risk: float = 0.25
    lane_risk: float = 0.01


class RandomEvent(_Model):
    type: str
    weight: float = 1.0
    duration: tuple[int, int] = (2, 8)
    params: dict[str, Any] = Field(default_factory=dict)


class Setting(_Model):
    schema_version: int = SCHEMA_VERSION
    name: str
    description: str = ""
    currency: str = "Cr"
    distance_unit: str = "hex"
    time_unit: str = "week"
    roles: Roles
    attributes: list[AttributeDef]
    profile: str = Field(..., description="display template, e.g. '{port}{size}{atm}-{tech}'")
    codes: list[CodeDef] = Field(default_factory=list)
    ports: dict[str, PortClass]
    goods: list[Good]
    lanes: LaneParams = Field(default_factory=LaneParams)
    name_syllables: list[str] = Field(default_factory=lambda: ["ka", "ra", "to", "mi", "su", "ne", "lo", "va"])
    polity_names: list[str] = Field(default_factory=list)
    random_events: list[RandomEvent] = Field(default_factory=list)
    disclaimer: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Setting":
        keys = {a.key for a in self.attributes}
        for role in ("population", "tech", "port"):
            if getattr(self.roles, role) not in keys:
                raise ValueError(f"roles.{role} refers to unknown attribute {getattr(self.roles, role)!r}")
        ids = {g.id for g in self.goods}
        if len(ids) != len(self.goods):
            raise ValueError("duplicate good ids")
        for g in self.goods:
            for i in g.inputs:
                if i not in ids:
                    raise ValueError(f"good {g.id!r} uses unknown input {i!r}")
        port_attr = next(a for a in self.attributes if a.key == self.roles.port)
        missing = [v for v in (port_attr.values or []) if v not in self.ports]
        if missing:
            raise ValueError(f"ports missing classes {missing}")
        return self

    def attr(self, key: str) -> AttributeDef:
        return next(a for a in self.attributes if a.key == key)


# --------------------------------------------------------------------------- campaign

class StarSystem(_Model):
    id: str = Field(..., description="hex label, e.g. '0304' (column, row)")
    name: str
    col: int
    row: int
    attrs: dict[str, int | str]
    codes: list[str] = Field(default_factory=list)
    polity: str | None = None
    notes: str = ""


class Lane(_Model):
    id: str
    a: str
    b: str
    length: int
    capacity: float
    risk: float = 0.01
    toll: float = 0.0


class Polity(_Model):
    id: str
    name: str
    color: str
    capital: str | None = None


EventType = Literal["war", "embargo", "tariff", "lane_disruption", "piracy",
                    "disaster", "boom", "relations", "modifier", "player_action"]


class Targets(_Model):
    systems: list[str] = Field(default_factory=list)
    lanes: list[str] = Field(default_factory=list)
    polities: list[str] = Field(default_factory=list)
    goods: list[str] = Field(default_factory=list, description="good ids; empty = all")
    tags: list[str] = Field(default_factory=list, description="goods with any of these tags")


class Event(_Model):
    id: str
    type: EventType
    name: str
    start: int
    duration: int | None = Field(None, description="weeks; None = until removed")
    targets: Targets = Field(default_factory=Targets)
    params: dict[str, Any] = Field(default_factory=dict)
    source: Literal["dm", "player", "random"] = "dm"
    notes: str = ""

    def active(self, tick: int) -> bool:
        return self.start <= tick and (self.duration is None or tick < self.start + self.duration)


class Trade(_Model):
    tick: int
    system: str
    good: str
    quantity: float = Field(..., description="> 0 bought by players, < 0 sold")
    price: float
    note: str = ""


class MarketState(_Model):
    """Arrays indexed [system][good] in the order of ``systems`` and ``setting.goods``."""

    price: list[list[float]]
    stock: list[list[float]]
    input_stock: list[list[float]]
    supply: list[list[float]] = Field(default_factory=list)
    demand: list[list[float]] = Field(default_factory=list)


class LaneFlow(_Model):
    lane: str
    good: str
    amount: float = Field(..., description="units/week; positive a->b, negative b->a")


class State(_Model):
    tick: int = 0
    market: MarketState | None = None
    flows: list[LaneFlow] = Field(default_factory=list)
    smuggling: list[LaneFlow] = Field(default_factory=list, description="off-lane flows, lane id 'a~b'")
    history: dict[str, list[list[float]]] = Field(
        default_factory=dict, description="good id -> list over ticks of per-system prices")
    history_ticks: list[int] = Field(default_factory=list)
    pending_trades: list[Trade] = Field(default_factory=list)
    lane_shares: list[list[float]] = Field(
        default_factory=list, description="[lane][good] share of each lane's tonnage capacity")


class PlayerView(_Model):
    visible_systems: list[str] = Field(default_factory=list)
    show_flows: bool = False


class Options(_Model):
    random_events: bool = False
    calibrate: bool = Field(True, description="scale each good's production so galaxy-wide supply "
                                              "matches demand (incl. industrial inputs) at base price")
    supply_margin: float = Field(1.05, description="calibrated supply / demand ratio")
    history_length: int = 52
    seed: int = 0


class Campaign(_Model):
    schema_version: int = SCHEMA_VERSION
    format: Literal["hexchange-campaign"] = "hexchange-campaign"
    name: str
    width: int
    height: int
    setting: Setting
    systems: list[StarSystem]
    polities: list[Polity] = Field(default_factory=list)
    relations: dict[str, float] = Field(default_factory=dict, description="'p1|p2' (sorted) -> -1..1")
    lanes: list[Lane] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    trades: list[Trade] = Field(default_factory=list)
    state: State = Field(default_factory=State)
    player_view: PlayerView = Field(default_factory=PlayerView)
    options: Options = Field(default_factory=Options)

    @model_validator(mode="after")
    def _check(self) -> "Campaign":
        ids = {s.id for s in self.systems}
        if len(ids) != len(self.systems):
            raise ValueError("duplicate system ids")
        for ln in self.lanes:
            if ln.a not in ids or ln.b not in ids:
                raise ValueError(f"lane {ln.id} references unknown system")
        pids = {p.id for p in self.polities}
        for s in self.systems:
            if s.polity is not None and s.polity not in pids:
                raise ValueError(f"system {s.id} references unknown polity {s.polity}")
        return self


def relation_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))
