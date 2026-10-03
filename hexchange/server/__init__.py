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

from .. import io as hio
from ..economy import Simulation
from ..events import EVENT_HELP
from ..generate import generate, profile
from ..model import Campaign, Event, PlayerView, relation_key
from ..routes import find_routes

WEB = Path(__file__).parent / "web"


class GenerateRequest(BaseModel):
    setting: str
    name: str = "New Sector"
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


def _map_payload(sim: Simulation, player: bool = False) -> dict[str, Any]:
    """Map data.  For players, only systems the GM has revealed are included, plus
    the lanes leading out of them.  The far end of such a lane is sent as an
    anonymous position only (``unknown``), so nothing hidden reaches the browser."""
    c, s = sim.camp, sim.camp.setting
    visible = set(c.player_view.visible_systems)
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
            "attributes": [{"key": a.key, "name": a.name, "kind": a.kind, "values": a.values,
                            "descriptions": a.descriptions} for a in s.attributes],
            "codes": [{"code": x.code, "name": x.name} for x in s.codes],
            "goods": [{"id": g.id, "name": g.name, "category": g.category, "tags": g.tags,
                       "base_price": g.base_price, "tons_per_unit": g.tons_per_unit} for g in s.goods],
            "max_jump": s.lanes.max_jump,
            "roles": s.roles.model_dump(),
        },
        "systems": [{"id": x.id, "name": x.name, "col": x.col, "row": x.row, "profile": profile(s, x.attrs),
                     "attrs": x.attrs, "codes": x.codes, "polity": x.polity, "visible": True} for x in systems],
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
                            density=req.density, polities=req.polities, seed=req.seed)
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
    def set_player_view(view: PlayerView):
        with st.lock:
            sim = st.need()
            sim.camp.player_view = view
            st.save()
            return view.model_dump()

    @app.get("/api/player/campaign")
    def player_campaign():
        with st.lock:
            return _map_payload(st.need(), player=True)

    @app.get("/api/player/prices")
    def player_prices(good: str):
        with st.lock:
            sim = st.need()
            return _prices(sim, good, set(sim.camp.player_view.visible_systems))

    @app.get("/api/player/system/{sid}")
    def player_system(sid: str):
        with st.lock:
            sim = st.need()
            if sid not in sim.camp.player_view.visible_systems:
                raise HTTPException(403, "no market data for this system")
            data = _system(sim, sid)
            data["events"] = []
            for row in data["market"]:
                row.pop("production", None)
                row.pop("supply", None)
                row.pop("demand", None)
            return data

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
