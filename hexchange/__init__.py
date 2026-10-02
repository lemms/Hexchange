"""Hexchange: a hex-grid galactic economy engine.

Library use (no web dependencies needed)::

    import hexchange as hx

    setting = hx.load_setting("generic")            # or a path, or a name in $HEXCHANGE_SETTINGS
    camp = hx.generate(setting, width=32, height=40, density=0.4, polities=4, seed=1)
    sim = hx.Simulation(camp)
    sim.warmup()
    sim.step(4)                                      # four weeks
    print(sim.quote(camp.systems[0].id, "food"))
    for r in hx.find_routes(sim, camp.systems[0].id, cargo_tons=200, jump=2)[:5]:
        print(r)
    sim.add_event(hx.Event(id="war1", type="war", name="Border war", start=camp.state.tick,
                           targets=hx.Targets(polities=["P1", "P2"])))
    hx.save_campaign(camp, "sector.hexchange.json")

The web UI lives in the optional ``hexchange.server`` module (``pip install hexchange[server]``).
"""

from .economy import Quote, Simulation
from .equilibrium import Curves, Network, Solution, solve as solve_equilibrium
from .events import EVENT_HELP
from .generate import generate, profile
from .io import (export_schemas, list_settings, load_campaign, load_setting, save_campaign,
                 setting_dirs)
from .model import (Campaign, Event, Good, Lane, Polity, Setting, StarSystem, Targets, Trade)
from .routes import Route, find_routes

__version__ = "0.1.0"

__all__ = [
    "Campaign", "Curves", "EVENT_HELP", "Event", "Good", "Lane", "Network", "Polity", "Quote", "Route",
    "Setting", "Simulation", "Solution", "StarSystem", "Targets", "Trade", "export_schemas", "find_routes",
    "generate", "list_settings", "load_campaign", "load_setting", "profile", "save_campaign",
    "setting_dirs", "solve_equilibrium", "__version__",
]
