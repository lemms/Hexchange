"""Local web server: GM interface at ``/``, read-only player view at ``/player``.

Requires the ``server`` extra (fastapi, uvicorn).  One campaign is held in
memory; every change is written back to its file when one is set.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import edit as ed
from .. import io as hio
from ..economy import Simulation
from ..events import EVENT_HELP
from ..generate import generate, profile
from ..model import Campaign, Event, PlayerView, relation_key
from ..routes import find_routes

WEB = Path(__file__).parent / "web"


class GenerateRequest(BaseModel):
    setting: str
    name: str = "New Galaxy"
    sector_name: str | None = None
    width: int = Field(32, ge=4, le=128)
    height: int = Field(40, ge=4, le=128)
    density: float = Field(0.4, gt=0, le=1)
    polities: int = Field(4, ge=0, le=24)
    seed: int = 0
    warmup: int = Field(16, ge=0, le=200)
    path: str | None = None


class TradeRequest(BaseModel):
    system: str
    good: str
    quantity: float
    note: str = ""


class State:
    def __init__(self, settings_dirs: list[str], path: str | None):
        self.lock = threading.RLock()
        self.settings_dirs = settings_dirs
        self.path: Path | None = Path(path).expanduser() if path else None
        self.sim: Simulation | None = None
        self.job = {"running": False, "done": 0, "total": 0, "error": None}
        if self.path and self.path.exists():
            self.sim = Simulation(hio.load_campaign(self.path))

    def need(self) -> Simulation:
        if self.sim is None:
            raise HTTPException(409, "no campaign loaded - generate or load one first")
        return self.sim

    def save(self) -> None:
        if self.sim is not None and self.path is not None:
            hio.save_campaign(self.sim.camp, self.path)


def _label(camp: Campaign, x) -> str:
    sec = next((s for s in camp.sectors if s.id == x.sector), None)
    return sec.label(x.col, x.row) if sec else x.id


def player_known(camp: Campaign) -> set[str]:
    """Systems that exist for the players: revealed by the GM, the party's location,
    and anywhere they hold market data for."""
    pv = camp.player_view
    return set(pv.visible_systems) | set(pv.knowledge) | ({pv.location} if pv.location else set())


def _known_market(sim: Simulation, sid: str) -> dict[str, Any] | None:
    """Player-facing market for one system: live if the party is there, else their snapshot."""
    pv = sim.camp.player_view
    goods = sim.camp.setting.goods
    if sid == pv.location:
        snap, live = sim.snapshot(sid), True
    elif sid in pv.knowledge:
        snap, live = pv.knowledge[sid], False
    else:
        return None
    return {"live": live, "tick": snap.tick, "source": snap.source, "note": snap.note,
            "age": sim.camp.state.tick - snap.tick,
            "market": [{"good": g.id, "price": snap.price[k], "buy": snap.buy[k], "sell": snap.sell[k],
                        "legal": snap.legal[k], "base_price": g.base_price}
                       for k, g in enumerate(goods) if k < len(snap.price)]}


def _map_payload(sim: Simulation, player: bool = False) -> dict[str, Any]:
    """Map data.  For players, only systems the GM has revealed are included, plus
    the lanes leading out of them.  The far end of such a lane is sent as an
    anonymous position only (``unknown``), so nothing hidden reaches the browser."""
    c, s = sim.camp, sim.camp.setting
    pv = c.player_view
    visible = player_known(c)
    systems = [x for x in c.systems if x.id in visible] if player else c.systems
    lanes = [ln for ln in c.lanes if ln.a in visible or ln.b in visible] if player else c.lanes
    by_id = {x.id: x for x in c.systems}
    unknown = sorted({sid for ln in lanes for sid in (ln.a, ln.b) if sid not in visible}) if player else []
    owners = {x.polity for x in systems}
    polities = [p for p in c.polities if p.id in owners] if player else c.polities
    return {
        "name": c.name, "width": c.width, "height": c.height, "tick": c.state.tick,
        "setting": {
            "name": s.name, "currency": s.currency, "distance_unit": s.distance_unit, "time_unit": s.time_unit,
            "profile": s.profile, "disclaimer": s.disclaimer,
            "attributes": [{"key": a.key, "name": a.name, "kind": a.kind, "values": a.values, "min": a.min,
                            "max": a.max, "descriptions": a.descriptions} for a in s.attributes],
            "codes": [{"code": x.code, "name": x.name} for x in s.codes],
            "goods": [{"id": g.id, "name": g.name, "category": g.category, "tags": g.tags,
                       "base_price": g.base_price, "tons_per_unit": g.tons_per_unit} for g in s.goods],
            "max_jump": s.lanes.max_jump,
            "roles": s.roles.model_dump(),
        },
        "sectors": [sec.model_dump() for sec in c.sectors
                    if not player or any(x.sector == sec.id for x in systems)],
        "systems": [{"id": x.id, "name": x.name, "col": x.col, "row": x.row, "profile": profile(s, x.attrs),
                     "sector": x.sector, "label": _label(c, x),
                     "attrs": x.attrs, "codes": x.codes, "polity": x.polity, "visible": True,
                     "here": x.id == pv.location,
                     "known_tick": pv.knowledge[x.id].tick if x.id in pv.knowledge else None,
                     "known_source": pv.knowledge[x.id].source if x.id in pv.knowledge else None}
                    for x in systems],
        "party": pv.location,
        "lanes": [ln.model_dump(include={"id", "a", "b", "length"}) if player else ln.model_dump() for ln in lanes],
        "unknown": [{"id": sid, "col": by_id[sid].col, "row": by_id[sid].row} for sid in unknown],
        "polities": [p.model_dump(exclude={"legality"}) if player else p.model_dump() for p in polities],
        "relations": {} if player else c.relations,
        "options": {} if player else c.options.model_dump(),
        "player_view": {} if player else c.player_view.model_dump(),
        "event_help": {} if player else EVENT_HELP,
        "active_events": [] if player else [e.id for e in sim.active_events()],
    }


def _prices(sim: Simulation, good: str, only: set[str] | None = None) -> dict[str, Any]:
    if good not in sim.ix.good:
        raise HTTPException(404, f"unknown good {good}")
    k = sim.ix.good[good]
    p = sim.prices[:, k]
    stock = sim._arr("stock")[:, k]
    out = {}
    for i, x in enumerate(sim.camp.systems):
        if only is not None and x.id not in only:
            continue
        out[x.id] = {"price": round(float(p[i]), 2), "stock": round(float(stock[i]), 2),
                     "legal": not bool(sim.illegal[i, k])}
    return {"good": good, "base_price": float(sim.pref[k]), "tick": sim.camp.state.tick, "systems": out}


def _system(sim: Simulation, sid: str, history: bool = True) -> dict[str, Any]:
    if sid not in sim.ix.sys:
        raise HTTPException(404, f"unknown system {sid}")
    c = sim.camp
    i = sim.ix.sys[sid]
    x = c.systems[i]
    lanes = [ln.model_dump() for ln in c.lanes if sid in (ln.a, ln.b)]
    market = []
    for q in sim.market(sid):
        k = sim.ix.good[q.good]
        row = {"good": q.good, "price": round(q.price, 2), "buy": round(q.buy, 2), "sell": round(q.sell, 2),
               "stock": round(q.stock, 1), "supply": round(q.supply, 2), "demand": round(q.demand, 2),
               "legal": q.legal, "base_price": float(sim.pref[k]),
               "production": round(float(sim.K[i, k]), 3)}
        if history:
            row["history"] = [h[i] for h in c.state.history.get(q.good, [])]
        market.append(row)
    return {"id": x.id, "name": x.name, "profile": profile(c.setting, x.attrs), "attrs": x.attrs,
            "codes": x.codes, "polity": x.polity, "notes": x.notes, "lanes": lanes, "market": market,
            "history_ticks": c.state.history_ticks,
            "events": [e.id for e in sim.active_events()
                       if sid in e.targets.systems or (x.polity and x.polity in e.targets.polities)]}


def create_app(campaign_path: str | None = None, settings_dirs: list[str] | None = None) -> FastAPI:
    st = State(settings_dirs or [], campaign_path)
    app = FastAPI(title="Hexchange", version="0.1.0")
    app.state.hx = st

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # the UI is plain ES modules: make browsers revalidate so an updated
        # map.js/app.js is never served from cache
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------ campaign
    @app.get("/api/settings")
    def settings():
        out = []
        for name, path in hio.list_settings(st.settings_dirs).items():
            try:
                s = hio.load_setting(path)
                out.append({"name": name, "title": s.name, "description": s.description, "path": str(path)})
            except Exception as e:          # show broken files instead of hiding them
                out.append({"name": name, "title": name, "description": f"invalid: {e}", "path": str(path)})
        return out

    @app.post("/api/generate")
    def gen(req: GenerateRequest):
        if st.job["running"]:
            raise HTTPException(409, "the simulation is running; wait for it to finish")
        with st.lock:
            setting = hio.load_setting(req.setting, st.settings_dirs)
            camp = generate(setting, name=req.name, width=req.width, height=req.height,
                            density=req.density, polities=req.polities, seed=req.seed,
                            sector_name=req.sector_name or None)
            sim = Simulation(camp)
            if req.warmup:
                sim.warmup(req.warmup)
            st.sim = sim
            if req.path:
                st.path = Path(req.path).expanduser()
            st.save()
            return _map_payload(sim)

    @app.get("/api/campaign")
    def campaign():
        with st.lock:
            return _map_payload(st.need()) | {"path": str(st.path) if st.path else None}

    @app.post("/api/campaign/save")
    def save(path: str | None = Body(None, embed=True)):
        with st.lock:
            st.need()
            if path:
                st.path = Path(path).expanduser()
            if st.path is None:
                raise HTTPException(400, "no path given")
            st.save()
            return {"path": str(st.path)}

    @app.post("/api/campaign/load")
    def load(path: str = Body(..., embed=True)):
        with st.lock:
            p = Path(path).expanduser()
            if not p.exists():
                raise HTTPException(404, f"{p} not found")
            st.sim = Simulation(hio.load_campaign(p))
            st.path = p
            return _map_payload(st.sim)

    @app.get("/api/campaign/download")
    def download():
        with st.lock:
            sim = st.need()
            return JSONResponse(sim.camp.model_dump(mode="json", exclude_none=True),
                                headers={"Content-Disposition":
                                         f'attachment; filename="{re.sub(r"[^\w.-]+", "_", sim.camp.name)}.hexchange.json"'})

    @app.post("/api/campaign/upload")
    def upload(data: dict = Body(...)):
        with st.lock:
            try:
                camp = Campaign.model_validate(data)
            except Exception as e:
                raise HTTPException(422, f"invalid campaign: {e}")
            st.sim = Simulation(camp)
            st.save()
            return _map_payload(st.sim)

    @app.put("/api/options")
    def options(random_events: bool | None = Body(None, embed=True), name: str | None = Body(None, embed=True)):
        with st.lock:
            sim = st.need()
            if random_events is not None:
                sim.camp.options.random_events = random_events
            if name:
                sim.camp.name = name
            st.save()
            return sim.camp.options.model_dump()

    @app.put("/api/polities/{pid}/legality")
    def set_legality(pid: str, key: str = Body(...), value: Any = Body(None)):
        """Set (or with value null, remove) one law: key is a good id or 'tag:<tag>';
        value is 'legal', 'illegal' or an int N (illegal where law > N)."""
        with st.lock:
            sim = st.need()
            pol = next((p for p in sim.camp.polities if p.id == pid), None)
            if pol is None:
                raise HTTPException(404, pid)
            if value is None:
                pol.legality.pop(key, None)
            else:
                if not (value in ("legal", "illegal") or isinstance(value, int)):
                    raise HTTPException(422, "value must be 'legal', 'illegal' or an integer law level")
                pol.legality[key] = value
            sim.refresh_legality()
            st.save()
            return pol.legality

    @app.put("/api/relations")
    def relations(a: str = Body(...), b: str = Body(...), value: float = Body(..., ge=-1, le=1)):
        with st.lock:
            sim = st.need()
            sim.camp.relations[relation_key(a, b)] = value
            st.save()
            return sim.camp.relations

    # ------------------------------------------------------------ simulation
    def run_job(weeks: int) -> None:
        try:
            for i in range(weeks):
                with st.lock:                # released between weeks so the UI stays responsive
                    st.sim.step(1)
                st.job["done"] = i + 1
            with st.lock:
                st.save()
        except Exception as e:               # surface failures to the UI instead of dying silently
            st.job["error"] = f"{type(e).__name__}: {e}"
        finally:
            st.job["running"] = False

    @app.post("/api/step")
    def step(weeks: int = Body(1, embed=True, ge=1, le=520)):
        with st.lock:
            sim = st.need()
            if st.job["running"]:
                raise HTTPException(409, "the simulation is already running")
            st.job.update(running=True, done=0, total=weeks, error=None)
            threading.Thread(target=run_job, args=(weeks,), daemon=True).start()
            return {"started": True, "weeks": weeks, "tick": sim.camp.state.tick}

    @app.get("/api/status")
    def status():
        sim = st.sim
        return {**st.job, "tick": sim.camp.state.tick if sim else None, "loaded": sim is not None}

    @app.get("/api/prices")
    def prices(good: str):
        with st.lock:
            return _prices(st.need(), good)

    @app.get("/api/flows")
    def flows(good: str | None = None):
        with st.lock:
            sim = st.need()
            stt = sim.camp.state
            sel = (lambda f: f.good == good) if good else (lambda f: True)
            value = {g.id: g.base_price for g in sim.camp.setting.goods}
            lanes: dict[str, float] = {}
            for f in stt.flows:
                if sel(f):
                    lanes[f.lane] = lanes.get(f.lane, 0.0) + (f.amount if good else abs(f.amount) * value[f.good])
            smug: dict[str, float] = {}
            for f in stt.smuggling:
                if sel(f):
                    smug[f.lane] = smug.get(f.lane, 0.0) + (f.amount if good else abs(f.amount) * value[f.good])
            return {"good": good, "lanes": lanes, "smuggling": smug}

    @app.get("/api/system/{sid}")
    def system(sid: str):
        with st.lock:
            return _system(st.need(), sid)

    @app.put("/api/system/{sid}/notes")
    def system_notes(sid: str, notes: str = Body(..., embed=True)):
        with st.lock:
            sim = st.need()
            if sid not in sim.ix.sys:
                raise HTTPException(404, sid)
            sim.camp.systems[sim.ix.sys[sid]].notes = notes
            st.save()
            return {"ok": True}

    @app.post("/api/trade")
    def trade(req: TradeRequest):
        with st.lock:
            sim = st.need()
            if req.system not in sim.ix.sys or req.good not in sim.ix.good:
                raise HTTPException(404, "unknown system or good")
            tr = sim.trade(req.system, req.good, req.quantity, req.note)
            st.save()
            return tr.model_dump()

    @app.get("/api/trades")
    def trades():
        with st.lock:
            return [t.model_dump() for t in st.need().camp.trades[-200:]]

    @app.get("/api/routes")
    def routes(origin: str, cargo: float = 100, jump: int = 2, max_jumps: int = 3, lanes_only: bool = True,
               illegal: bool = False):
        with st.lock:
            sim = st.need()
            if origin not in sim.ix.sys:
                raise HTTPException(404, origin)
            return [r.as_dict() for r in find_routes(sim, origin, cargo_tons=cargo, jump=jump, max_jumps=max_jumps,
                                                     lanes_only=lanes_only, include_illegal=illegal)]

    # ------------------------------------------------------------ events
    @app.get("/api/events")
    def events():
        with st.lock:
            sim = st.need()
            tick = sim.camp.state.tick
            return [e.model_dump() | {"active": e.active(tick)} for e in sim.camp.events]

    @app.post("/api/events")
    def add_event(event: Event):
        with st.lock:
            sim = st.need()
            try:
                sim.add_event(event)
            except ValueError as e:
                raise HTTPException(409, str(e))
            st.save()
            return event.model_dump()

    @app.put("/api/events/{eid}")
    def update_event(eid: str, event: Event):
        with st.lock:
            sim = st.need()
            sim.remove_event(eid)
            sim.add_event(event)
            st.save()
            return event.model_dump()

    @app.delete("/api/events/{eid}")
    def delete_event(eid: str):
        with st.lock:
            sim = st.need()
            sim.remove_event(eid)
            st.save()
            return {"ok": True}

    # ------------------------------------------------------------ player view
    @app.put("/api/player_view")
    def set_player_view(visible_systems: list[str] = Body(...), show_flows: bool = Body(False)):
        with st.lock:
            sim = st.need()
            pv = sim.camp.player_view
            pv.visible_systems = [s for s in visible_systems if s in sim.ix.sys]
            pv.show_flows = show_flows
            st.save()
            return pv.model_dump()

    @app.get("/api/player/campaign")
    def player_campaign():
        with st.lock:
            return _map_payload(st.need(), player=True)

    @app.get("/api/player/prices")
    def player_prices(good: str):
        """Prices as the players know them: live where the party is, remembered elsewhere."""
        with st.lock:
            sim = st.need()
            if good not in sim.ix.good:
                raise HTTPException(404, good)
            k = sim.ix.good[good]
            out = {}
            for sid in player_known(sim.camp):
                km = _known_market(sim, sid)
                if km:
                    row = km["market"][k]
                    out[sid] = {"price": row["price"], "legal": row["legal"], "live": km["live"], "age": km["age"]}
            return {"good": good, "base_price": float(sim.pref[k]), "tick": sim.camp.state.tick, "systems": out}

    @app.get("/api/player/system/{sid}")
    def player_system(sid: str):
        with st.lock:
            sim = st.need()
            if sid not in player_known(sim.camp):
                raise HTTPException(403, "unknown system")
            x = sim.camp.systems[sim.ix.sys[sid]]
            return {"id": x.id, "name": x.name, "profile": profile(sim.camp.setting, x.attrs), "codes": x.codes,
                    "polity": x.polity, "here": sid == sim.camp.player_view.location,
                    "known": _known_market(sim, sid), "tick": sim.camp.state.tick}

    # ------------------------------------------------------------ GM map & politics editor
    def editing(fn):
        """Run an edit under the lock, rebuild the economy, autosave, return the new map."""
        with st.lock:
            sim = st.need()
            if st.job["running"]:
                raise HTTPException(409, "the simulation is running; edit when it has finished")
            try:
                result = fn(sim.camp)
            except ed.EditError as e:
                raise HTTPException(400, str(e))
            Campaign.model_validate(sim.camp.model_dump())      # never save an invalid galaxy
            sim.rebuild()
            st.save()
            out = _map_payload(sim) | {"path": str(st.path) if st.path else None}
            if result is not None and hasattr(result, "model_dump"):
                out["result"] = result.model_dump()
            elif isinstance(result, list):
                out["result"] = [r.model_dump() if hasattr(r, "model_dump") else r for r in result]
            return out

    @app.post("/api/sectors")
    def add_sector(name: str = Body(...), adjacent: str | None = Body(None), direction: str = Body("E"),
                   col0: int | None = Body(None), row0: int | None = Body(None), width: int = Body(32, ge=4, le=128),
                   height: int = Body(40, ge=4, le=128), mode: str = Body("random"), density: float = Body(0.4, gt=0, le=1),
                   polities: int = Body(0, ge=0, le=12), seed: int | None = Body(None)):
        at = (col0, row0) if col0 is not None and row0 is not None else None
        return editing(lambda c: ed.add_sector(c, name, adjacent=(adjacent, direction) if adjacent else None, at=at,
                                               width=width, height=height, mode=mode, density=density,
                                               polities=polities, seed=seed))

    @app.put("/api/sectors/{sid}")
    def update_sector(sid: str, name: str | None = Body(None), notes: str | None = Body(None)):
        return editing(lambda c: ed.update_sector(c, sid, name, notes))

    @app.delete("/api/sectors/{sid}")
    def remove_sector(sid: str):
        return editing(lambda c: ed.remove_sector(c, sid))

    @app.post("/api/systems")
    def add_system(col: int = Body(...), row: int = Body(...), name: str | None = Body(None),
                   attrs: dict | None = Body(None)):
        return editing(lambda c: ed.add_system(c, col, row, name=name, attrs=attrs))

    @app.put("/api/systems/{sid}")
    def update_system(sid: str, name: str | None = Body(None), attrs: dict | None = Body(None),
                      notes: str | None = Body(None)):
        return editing(lambda c: ed.update_system(c, sid, name=name, attrs=attrs, notes=notes))

    @app.post("/api/systems/{sid}/reroll")
    def reroll_system(sid: str):
        return editing(lambda c: ed.reroll_system(c, sid))

    @app.delete("/api/systems/{sid}")
    def remove_system(sid: str):
        return editing(lambda c: ed.remove_system(c, sid))

    @app.post("/api/lanes")
    def add_lane(a: str = Body(...), b: str = Body(...), capacity: float | None = Body(None),
                 risk: float | None = Body(None), toll: float | None = Body(None)):
        return editing(lambda c: ed.add_lane(c, a, b, capacity=capacity, risk=risk, toll=toll))

    @app.put("/api/lanes/{lid}")
    def update_lane(lid: str, capacity: float | None = Body(None), risk: float | None = Body(None),
                    toll: float | None = Body(None)):
        return editing(lambda c: ed.update_lane(c, lid, capacity=capacity, risk=risk, toll=toll))

    @app.delete("/api/lanes/{lid}")
    def remove_lane(lid: str):
        return editing(lambda c: ed.remove_lane(c, lid))

    @app.post("/api/lanes/auto")
    def auto_lanes(systems: list[str] = Body(..., embed=True)):
        return editing(lambda c: ed.auto_lanes(c, systems))

    @app.post("/api/polities")
    def add_polity(name: str = Body(...), color: str | None = Body(None), capital: str | None = Body(None)):
        return editing(lambda c: ed.add_polity(c, name, color=color, capital=capital))

    @app.put("/api/polities/{pid}")
    def update_polity(pid: str, name: str | None = Body(None), color: str | None = Body(None),
                      capital: str | None = Body(None)):
        return editing(lambda c: ed.update_polity(c, pid, name=name, color=color, capital=capital))

    @app.delete("/api/polities/{pid}")
    def remove_polity(pid: str):
        return editing(lambda c: ed.remove_polity(c, pid))

    @app.put("/api/assign")
    def assign_systems(systems: list[str] = Body(...), polity: str | None = Body(None)):
        return editing(lambda c: ed.assign(c, systems, polity))

    @app.post("/api/polities/{pid}/grow")
    def grow(pid: str, reach: int = Body(4, ge=1, le=40), start: str | None = Body(None),
             overwrite: bool = Body(False)):
        return editing(lambda c: ed.grow_polity(c, pid, reach, start=start, overwrite=overwrite))

    # ------------------------------------------------------------ party (GM controls)
    @app.put("/api/party")
    def move_party(location: str | None = Body(None, embed=True)):
        with st.lock:
            sim = st.need()
            if location is not None and location not in sim.ix.sys:
                raise HTTPException(404, location)
            sim.move_party(location)
            st.save()
            return sim.camp.player_view.model_dump()

    @app.post("/api/party/report")
    def market_report(system: str = Body(...), note: str = Body("")):
        with st.lock:
            sim = st.need()
            if system not in sim.ix.sys:
                raise HTTPException(404, system)
            snap = sim.market_report(system, note)
            st.save()
            return snap.model_dump()

    @app.delete("/api/party/knowledge/{sid}")
    def forget(sid: str):
        with st.lock:
            sim = st.need()
            sim.camp.player_view.knowledge.pop(sid, None)
            st.save()
            return {"ok": True}

    @app.post("/api/client-log")
    def client_log(entry: dict = Body(...)):
        """Browser-side errors and diagnostics, so problems in the user's browser show up here."""
        import sys
        print(f"[client] {entry}", file=sys.stderr, flush=True)
        return {"ok": True}

    # ------------------------------------------------------------ static UI
    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    @app.get("/player")
    def player():
        return FileResponse(WEB / "player.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
