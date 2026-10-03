# Hexchange

A galactic economy engine for tabletop sci-fi campaigns. Hexchange simulates a hex-grid
galaxy: star systems produce and consume goods, charted hyperlanes link them, and smugglers
make uncharted jumps. Prices settle into a spatial equilibrium that players can exploit by
trading with their ships. The GM adds wars, embargoes, tariffs, piracy, disasters and booms,
and the economy reacts.

Hexchange is genre-neutral. Everything specific to a game comes from a **setting file**:
world attributes and how they're rolled, classification codes, port classes, goods and
recipes, names. A small generic setting is bundled; write your own for any universe.

It's a Python library first (`import hexchange`), with a command-line tool and a local web UI
on top.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[server]'      # library + web UI
.venv/bin/pip install -e .                # library only (numpy, scipy, pydantic)
```

## Quick start

```bash
hexchange settings                                      # list available settings
hexchange serve campaign.hexchange.json                 # web UI: http://127.0.0.1:8000
```

In the UI, click **New galaxy…** to start a campaign with one random sector (add more in the
Map tab). Changes autosave to the campaign file;
**Save** lets you save elsewhere or download a copy. The GM view is at `/` and the player
view at `/player`. **Party and information:** the GM places the party at a system and moves it around. Players
see that market live. When they leave, they keep the prices as they were on departure, with
no further updates. The GM can also sell them a one-off courier report for a distant system.
Players see nothing until the GM reveals systems: then only those systems and their markets, plus the lanes leading out of them to uncharted (unnamed) endpoints. Hidden systems are never sent to the player's browser.

Without the UI:

```bash
hexchange generate --setting generic --out sector.hexchange.json --seed 7
hexchange run sector.hexchange.json --weeks 4
hexchange info sector.hexchange.json --system 0304
hexchange routes sector.hexchange.json --origin 0304 --cargo 200 --jump 2
hexchange schema --out schemas/                         # JSON Schema for both file types
```

### Your own settings

Setting files are named `*.setting.json`. Hexchange finds them in directories given with
`--settings-dir` (repeatable), in `$HEXCHANGE_SETTINGS` (`:`-separated), and in the bundled
`hexchange/settings/`. Keep game-specific settings outside this repository.

```bash
export HEXCHANGE_SETTINGS=~/Documents/programming/hexchange-settings
hexchange serve --settings-dir ~/my-settings campaign.hexchange.json
```

## Library use

```python
import hexchange as hx

setting = hx.load_setting("generic")                 # name, or a path to a .setting.json
camp = hx.generate(setting, width=32, height=40, density=0.4, polities=4, seed=1)
sim = hx.Simulation(camp)
sim.warmup()                                          # settle into a steady state
sim.step(4)                                           # four weeks

q = sim.quote(camp.systems[0].id, "food")             # price, buy/sell, stock, legality
routes = hx.find_routes(sim, camp.systems[0].id, cargo_tons=200, jump=2)
sim.trade(camp.systems[0].id, "food", 50)             # players buy 50 units
sim.move_party(camp.systems[1].id)                    # GM moves the party; old prices are remembered
sim.market_report(camp.systems[5].id, note="courier") # one-off snapshot of a distant market
sim.add_event(hx.Event(id="war1", type="war", name="Border war", start=camp.state.tick,
                       targets=hx.Targets(polities=["P1", "P2"])))
hx.save_campaign(camp, "sector.hexchange.json")
```

The spatial price equilibrium solver can also be used on its own:
`hx.solve_equilibrium(curves, network)`.

## How the economy works

Each tick is one week.

1. **Production:** raw goods are extracted according to world codes. Manufactured goods follow
   input–output recipes, limited by the inputs bought the previous week.
2. **Markets:** each system has supply and demand curves per good. Supply is output plus trader
   stock; demand is consumers plus industry restocking its inputs. A small "substitutes"
   backstop caps shortages at a few times the normal price.
3. **Trade:** for each good, the engine solves the **spatial price equilibrium**
   (Samuelson 1952; Takayama & Judge 1964) on the network of charted lanes and uncharted jumps.
   Where goods flow, the price difference equals the transport cost; where it doesn't pay,
   nothing flows; saturated lanes leave price cliffs. The solver minimises a convex function
   of prices with Newton's method. Its Hessian is a weighted graph Laplacian, the same
   structure as a finite-element stiffness matrix, so prices behave like a potential field
   diffusing over the lanes, with transport cost as friction.
4. **Costs:** freight per ton per hex, tolls, port fees, risk premiums, and customs between
   polities (higher when their relations are hostile).
5. **Calibration:** production is scaled per good so that galaxy-wide supply matches demand at
   the base price. Any setting therefore yields sensible prices, and local conditions create
   the gradients players exploit. The setting's `volume_scale` sets how many tons a week move,
   which decides how much a player ship's trading moves prices.
6. **Trade inertia** (on by default): commercial shipping on a link can only grow gradually
   from week to week. After a war, embargo or boom, price gaps open and close over several
   weeks, diffusing in time as well as across the map. That window is what players exploit.

### Building and editing the galaxy

The GM can change the map at any time, including mid-campaign:
- **Edit map** (header button or the **Map** tab):
  - click an empty hex to add a system (blank attributes are rolled with the setting's dice,
    and codes are worked out automatically);
  - click a system to edit, reroll or delete it, or to connect it to shift-selected systems;
  - click a lane to change its capacity, risk or toll, or delete it.
  - **Auto-lane** links systems to their neighbours the way the generator does.
- **Sectors:** a galaxy is a patchwork of sectors on one hex grid. Add a sector north,
  south, east or west of another, either random (new lanes join it to its neighbours across
  the border, existing lanes never change, optionally with new polities) or blank to build
  by hand. All sectors share one economy, so trade, smuggling, route finding and the party
  move freely between them.
- **Political regions:** in the Politics tab you can found a polity (with the selected system
  as capital), rename and recolour it, change its capital, **paint** systems into it by
  clicking the map, assign selected systems, grow it outward from its capital, or dissolve
  it.

Edits keep everything else intact: existing systems keep their prices, stocks and history,
and references to removed systems (events, party, player knowledge) are cleaned up. The same
operations are available to code in `hexchange.edit` (call `sim.rebuild()` afterwards).

### Laws and contraband

Each good can have a default law-level threshold (`illegal_above_law`). Each polity can
override it, by good or by tag, with `legal`, `illegal`, or "illegal above law N". At
generation, polities roll their laws from the setting's `polity_legality` presets, so
neighbouring regions differ. A `legality` event changes laws temporarily, e.g. martial law.
The GM edits polity laws in the Politics tab.

### Smugglers

Every pair of systems within jump range without a charted lane is an **uncharted jump**: costly,
risky and narrow. Wars, embargoes, tariffs and lane disruptions act only on charted lanes, so
smugglers route around them. Contraband, meaning goods banned at the destination by its law level or
polity laws, can only arrive by uncharted jumps.

In tests, a total embargo cut a polity's legal trade to zero, while smuggling across its
border rose 3.6× and import prices inside rose up to 3.9×.

### Events

Events are non-destructive modifiers. Ending one restores the galaxy exactly. The built-in
types are `war`, `embargo`, `tariff`, `lane_disruption`, `piracy`, `disaster`, `boom`,
`relations`, and the generic `modifier` / `player_action`, which take `field`, `op` and
`value`. Players' political impact is recorded as a `player_action` event by the GM. Random
events (the setting's presets) can be switched on per campaign.

## File formats

| File | Contents |
|---|---|
| `*.setting.json` | `roles` (which attributes mean population, tech, port, law), `attributes` (dice, modifiers, tables, rules), `profile` (display string), `codes`, `ports`, `goods` (price, tons per unit, recipe, production and demand by code, legality), `lanes`, names, random-event presets |
| `*.hexchange.json` (optionally `.gz`) | One campaign: an embedded copy of its setting, systems, polities and relations, lanes, events, player trades, simulation state and price history |

JSON Schemas are in `schemas/`. Files carry a `schema_version`, and newer versions are
rejected rather than misread.

### Performance

One simulated week takes about 1.7 s for a two-sector Traveller galaxy (1,072 systems, 27
goods) and about 0.65 s for a four-sector generic galaxy (2,135 systems, 12 goods), measured
on this machine's CPU. Weeks just after a sector is added run slower while the new trade
settles.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests cover the hex maths, deterministic generation, lane connectivity and jump limits,
the solver's equilibrium conditions, capacity and conservation, events reverting exactly,
smugglers bypassing embargoes, wars raising weapons prices, player trading narrowing price
gaps, contraband never arriving on charted lanes, file round-trips (a reloaded campaign
continues identically), validation, and the web API including player-view restrictions.

## Licence

MIT (see `LICENSE`). Game-specific setting files you write may be subject to their
publishers' fan policies. Keep them outside this repository.
