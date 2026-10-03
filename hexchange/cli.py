"""``hexchange`` command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import io as hio
from .economy import Simulation
from .generate import generate, profile
from .routes import find_routes


def _common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--settings-dir", action="append", default=[],
                    help="extra directory with *.setting.json files (repeatable; also $HEXCHANGE_SETTINGS)")


def cmd_settings(a) -> int:
    for name, path in hio.list_settings(a.settings_dir).items():
        print(f"{name:20s} {path}")
    return 0


def cmd_generate(a) -> int:
    setting = hio.load_setting(a.setting, a.settings_dir)
    camp = generate(setting, name=a.name, width=a.width, height=a.height, density=a.density,
                    polities=a.polities, seed=a.seed)
    sim = Simulation(camp)
    if a.warmup:
        sim.warmup(a.warmup)
    hio.save_campaign(camp, a.out)
    print(f"{camp.name}: {len(camp.systems)} systems, {len(camp.lanes)} lanes, "
          f"{len(camp.polities)} polities -> {a.out}")
    return 0


def cmd_run(a) -> int:
    camp = hio.load_campaign(a.campaign)
    sim = Simulation(camp)
    sim.step(a.weeks)
    hio.save_campaign(camp, a.out or a.campaign)
    print(f"tick {camp.state.tick}; active events: {[e.id for e in sim.active_events()] or 'none'}")
    return 0


def cmd_info(a) -> int:
    camp = hio.load_campaign(a.campaign)
    s = camp.setting
    print(f"{camp.name} ({s.name}) {camp.width}x{camp.height}, tick {camp.state.tick}")
    print(f"{len(camp.systems)} systems, {len(camp.lanes)} lanes, {len(camp.polities)} polities, "
          f"{len(camp.events)} events")
    if a.system:
        sim = Simulation(camp)
        x = camp.systems[sim.ix.sys[a.system]]
        print(f"\n{x.id} {x.name} {profile(s, x.attrs)} {' '.join(x.codes)} polity={x.polity}")
        print(f"{'good':24s} {'price':>12s} {'buy':>12s} {'sell':>12s} {'stock':>10s}")
        for q in sim.market(a.system):
            name = next(g.name for g in s.goods if g.id == q.good)
            print(f"{name:24s} {q.price:12.0f} {q.buy:12.0f} {q.sell:12.0f} {q.stock:10.1f}"
                  f"{'' if q.legal else '  (illegal)'}")
    return 0


def cmd_routes(a) -> int:
    camp = hio.load_campaign(a.campaign)
    sim = Simulation(camp)
    for r in find_routes(sim, a.origin, cargo_tons=a.cargo, jump=a.jump, max_jumps=a.max_jumps,
                         lanes_only=not a.offlane, include_illegal=a.illegal, top=a.top):
        print(f"{r.good:10s} {r.origin}->{r.destination} ({r.jumps} jumps{', ' + str(r.offlane_jumps) + ' off-lane' if r.offlane_jumps else ''}) "
              f"qty {r.quantity:8.1f}  buy {r.buy:10.0f} sell {r.sell:10.0f}  profit {r.profit:12.0f}"
              f"  risk {r.risk:.0%}{'' if r.legal else '  SMUGGLING'}")
    return 0


def cmd_schema(a) -> int:
    for p in hio.export_schemas(a.out):
        print(p)
    return 0


def cmd_serve(a) -> int:
    try:
        import uvicorn

        from .server import create_app
    except ImportError:
        print("the web UI needs the server extra: pip install 'hexchange[server]'", file=sys.stderr)
        return 1
    app = create_app(a.campaign, a.settings_dir)
    print(f"Hexchange: GM view http://{a.host}:{a.port}/   player view http://{a.host}:{a.port}/player")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hexchange", description="Hex-grid galactic economy engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("settings", help="list available setting files")
    _common(sp)
    sp.set_defaults(fn=cmd_settings)

    sp = sub.add_parser("generate", help="generate a random sector")
    _common(sp)
    sp.add_argument("--setting", default="generic")
    sp.add_argument("--out", required=True, type=Path)
    sp.add_argument("--name", default="New Sector")
    sp.add_argument("--width", type=int, default=32)
    sp.add_argument("--height", type=int, default=40)
    sp.add_argument("--density", type=float, default=0.4)
    sp.add_argument("--polities", type=int, default=4)
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--warmup", type=int, default=16, help="weeks to settle the economy (0 = none)")
    sp.set_defaults(fn=cmd_generate)

    sp = sub.add_parser("run", help="advance a campaign by N weeks")
    sp.add_argument("campaign", type=Path)
    sp.add_argument("--weeks", type=int, default=1)
    sp.add_argument("--out", type=Path)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("info", help="summarise a campaign or show a system's market")
    sp.add_argument("campaign", type=Path)
    sp.add_argument("--system")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("routes", help="best trade routes from a system")
    sp.add_argument("campaign", type=Path)
    sp.add_argument("--origin", required=True)
    sp.add_argument("--cargo", type=float, default=100)
    sp.add_argument("--jump", type=int, default=2)
    sp.add_argument("--max-jumps", type=int, default=3)
    sp.add_argument("--offlane", action="store_true", help="allow uncharted jumps")
    sp.add_argument("--illegal", action="store_true", help="include contraband")
    sp.add_argument("--top", type=int, default=15)
    sp.set_defaults(fn=cmd_routes)

    sp = sub.add_parser("schema", help="export JSON Schema for setting and campaign files")
    sp.add_argument("--out", type=Path, default=Path("schemas"))
    sp.set_defaults(fn=cmd_schema)

    sp = sub.add_parser("serve", help="run the web UI")
    _common(sp)
    sp.add_argument("campaign", nargs="?", help="campaign file to open (created on save if missing)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
