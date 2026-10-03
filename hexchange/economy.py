"""Weekly economy tick and the :class:`Simulation` facade.

Per tick:

1. Fold active events into modifiers (:mod:`hexchange.events`).
2. Production: raw goods are extracted at capacity; manufactured goods are
   limited by the input stock bought last week (input-output recipes).
3. Market curves per system and good:
   supply  = (output + a share of trader stock) x (p/p_ref)^0.6, plus a small
             "substitutes" backstop that only matters at several times p_ref;
   demand  = consumers x (p/p_ref)^-elasticity + industry restocking inputs.
4. For every good, solve the spatial price equilibrium on the network of
   charted lanes plus uncharted (smuggler) jumps -- :mod:`hexchange.equilibrium`.
5. Update stocks, record prices, flows and history.

Coupling between goods (inputs, lane capacity shared by tonnage) is carried
from one week to the next, so each week's solve is per good and fast.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from . import events as ev_mod, hexgrid
from .equilibrium import Curves, Network, solve
from .model import Campaign, Event, LaneFlow, MarketState, Trade, relation_key

SUPPLY_ELASTICITY = 0.6
STOCK_RELEASE = 0.35          # share of trader stock offered each week at p_ref
INPUT_ELASTICITY = 0.5
INPUT_WEEKS = 2.0             # industry keeps this many weeks of inputs
BACKSTOP_SHARE = 0.03         # substitutes, as a share of demand at p_ref
BACKSTOP_EXPONENT = 2.5
SPOILAGE = 0.01
STOCK_CAP_WEEKS = 12.0
BASE_SPREAD = 0.03            # bid/ask half-spread
CUSTOMS = 0.05                # duty between different polities (fraction of price)
HOSTILE_TARIFF = 0.25         # extra duty at relation -1


@dataclass
class Quote:
    system: str
    good: str
    price: float
    buy: float
    sell: float
    stock: float
    supply: float
    demand: float
    legal: bool


class Simulation:
    """Owns a :class:`~hexchange.model.Campaign` and advances its economy.

    Typical use::

        sim = Simulation(campaign)
        sim.warmup()                 # settle into a steady state
        sim.step(4)                  # four weeks
        sim.quote("0304", "food")
        sim.add_event(Event(...))
    """

    def __init__(self, campaign: Campaign, seed: int | None = None):
        self.camp = campaign
        self.rng = random.Random(campaign.options.seed if seed is None else seed)
        self._build()
        if campaign.state.market is None:
            self._init_state()

    # ------------------------------------------------------------------ setup
    def _build(self) -> None:
        c, s = self.camp, self.camp.setting
        self.ix = ev_mod.Index(c)
        self.N, self.G, self.E = len(c.systems), len(s.goods), len(c.lanes)
        roles = s.roles
        pop = np.array([float(x.attrs.get(roles.population, 0) or 0) for x in c.systems])
        tech = np.array([float(x.attrs.get(roles.tech, 0) or 0) for x in c.systems])
        self.weight = roles.population_weight_base ** pop
        self.pref = np.array([g.base_price for g in s.goods])
        self.tpu = np.array([g.tons_per_unit for g in s.goods])
        self.eps = np.array([g.demand.elasticity for g in s.goods])
        codes = [set(x.codes) for x in c.systems]
        law = (np.array([float(x.attrs.get(roles.law, 0) or 0) for x in c.systems]) if roles.law
               else np.zeros(self.N))
        self.law = law
        self.K = np.zeros((self.N, self.G))
        self.D0 = np.zeros((self.N, self.G))
        for k, g in enumerate(s.goods):
            pm = np.array([np.prod([g.production.codes.get(cd, 1.0) for cd in cs]) for cs in codes])
            ok = tech >= g.production.min_tech
            if g.production.requires_any:
                ok &= np.array([bool(cs & set(g.production.requires_any)) for cs in codes])
            self.K[:, k] = g.production.base * self.weight * pm * ok
            dm = np.array([np.prod([g.demand.codes.get(cd, 1.0) for cd in cs]) for cs in codes])
            tech_ok = np.where(tech >= g.demand.min_tech, 1.0, 0.25)
            self.D0[:, k] = g.demand.base * self.weight * dm * tech_ok
        gid = {g.id: k for k, g in enumerate(s.goods)}
        self.recipe = np.zeros((self.G, self.G))          # [output, input]
        for k, g in enumerate(s.goods):
            for inp, q in g.inputs.items():
                self.recipe[k, gid[inp]] = q
        self.manufactured = self.recipe.sum(1) > 0
        self.illegal = self.legality(ev_mod.build_modifiers(c, c.state.tick, self.ix))
        if c.options.calibrate:
            self._calibrate(c.options.supply_margin)
        # ports
        portattr = roles.port
        self.port_fee = np.array([s.ports[str(x.attrs.get(portattr))].fees
                                  if str(x.attrs.get(portattr)) in s.ports else 0.0 for x in c.systems])
        # lanes
        self.lane_len = np.array([ln.length for ln in c.lanes], float)
        self.lane_cap = np.array([ln.capacity for ln in c.lanes], float)
        self.lane_risk = np.array([ln.risk for ln in c.lanes], float)
        self.lane_toll = np.array([ln.toll for ln in c.lanes], float)
        # uncharted jumps: every pair within max_jump that has no charted lane
        lanes = {tuple(sorted((ln.a, ln.b))) for ln in c.lanes}
        pos = {(x.col, x.row): i for i, x in enumerate(c.systems)}
        pairs = []
        for i, x in enumerate(c.systems):
            for cc, rr in hexgrid.within(x.col, x.row, s.lanes.max_jump, c.width, c.height):
                j = pos.get((cc, rr))
                if j is not None and j > i and tuple(sorted((x.id, c.systems[j].id))) not in lanes:
                    pairs.append((i, j, hexgrid.distance((x.col, x.row), (cc, rr))))
        self.off_a = np.array([p[0] for p in pairs], int)
        self.off_b = np.array([p[1] for p in pairs], int)
        self.off_len = np.array([p[2] for p in pairs], float)
        self.share = np.full((self.E, self.G), 1.0 / max(1, self.G))
        saved = np.array(c.state.lane_shares, float)
        if saved.shape == (self.E, self.G):
            self.share = saved
        else:
            self._shares_from_flows()

    def _apply_rule(self, illegal: np.ndarray, rows: np.ndarray, cols: np.ndarray, value) -> None:
        if not rows.any() or not cols.any():
            return
        if value == "legal":
            illegal[np.ix_(rows, cols)] = False
        elif value == "illegal":
            illegal[np.ix_(rows, cols)] = True
        else:
            illegal[np.ix_(rows, cols)] = (self.law[rows] > float(value))[:, None]

    def legality(self, mods: ev_mod.Modifiers | None = None) -> np.ndarray:
        """(N, G) bool, True where a good is banned.

        Precedence, lowest first: the good's ``illegal_above_law``; polity tag
        rules; polity good rules; active ``legality`` events (in order)."""
        s = self.camp.setting
        illegal = np.zeros((self.N, self.G), bool)
        for k, g in enumerate(s.goods):
            if g.illegal_above_law is not None:
                illegal[:, k] = self.law > g.illegal_above_law
        tags = [set(g.tags) for g in s.goods]
        for pol in self.camp.polities:
            if not pol.legality:
                continue
            rows = self.ix.polity_of == pol.id
            ordered = sorted(pol.legality.items(), key=lambda kv: not kv[0].startswith("tag:"))
            for key, value in ordered:                     # tag rules first, then specific goods
                if key.startswith("tag:"):
                    cols = np.array([key[4:] in t for t in tags])
                elif key in self.ix.good:
                    cols = np.zeros(self.G, bool)
                    cols[self.ix.good[key]] = True
                else:
                    continue
                self._apply_rule(illegal, rows, cols, value)
        if mods is not None:
            for rows, cols, value in mods.legality:
                self._apply_rule(illegal, rows, cols, value)
        return illegal

    def refresh_legality(self) -> None:
        """Recompute legality now (after editing laws or events), not just at the next tick."""
        self.illegal = self.legality(ev_mod.build_modifiers(self.camp, self.camp.state.tick, self.ix))

    def _calibrate(self, margin: float) -> None:
        """Scale production per good so total supply = margin x total demand at p_ref.

        Goods are processed from the top of the recipe chain down, so the
        input needs of (already calibrated) factories count as demand."""
        depth = np.zeros(self.G)
        for _ in range(self.G):                      # longest path to a final good
            for k in range(self.G):
                users = np.flatnonzero(self.recipe[:, k])
                if len(users):
                    depth[k] = max(depth[k], 1 + depth[users].max())
        for k in np.argsort(depth):                  # final goods first
            demand = self.D0[:, k].sum() + (self.K @ self.recipe)[:, k].sum()
            supply = self.K[:, k].sum()
            if supply > 0 and demand > 0:
                self.K[:, k] *= margin * demand / supply

    def _shares_from_flows(self) -> None:
        if not self.camp.state.flows or self.E == 0:
            return
        tons = np.zeros((self.E, self.G))
        for f in self.camp.state.flows:
            e, k = self.ix.lane.get(f.lane), self.ix.good.get(f.good)
            if e is not None and k is not None:
                tons[e, k] += abs(f.amount) * self.tpu[k]
        self._update_shares(tons, blend=1.0)

    def _update_shares(self, tons: np.ndarray, blend: float = 0.4) -> None:
        tot = tons.sum(1, keepdims=True)
        target = np.where(tot > 0, tons / np.maximum(tot, 1e-12), 1.0 / self.G)
        sh = (1 - blend) * self.share + blend * target
        sh = np.maximum(sh, 0.25 / self.G)
        # stored rounded, and used rounded, so a reloaded campaign continues identically
        self.share = np.round(sh / sh.sum(1, keepdims=True), 6)
        self.camp.state.lane_shares = self.share.tolist()

    def _init_state(self) -> None:
        Y = self.K.copy()
        need = self.K @ self.recipe            # (N, G_in): inputs per week at capacity
        stock = 2.0 * (Y + self.D0)
        self.camp.state.market = MarketState(
            price=np.tile(self.pref, (self.N, 1)).round(2).tolist(),
            stock=stock.round(2).tolist(),
            input_stock=(INPUT_WEEKS * need).round(2).tolist(),
        )

    # ------------------------------------------------------------------ arrays
    def _arr(self, name: str) -> np.ndarray:
        m = self.camp.state.market
        a = np.array(getattr(m, name), float)
        return a.reshape(self.N, self.G) if a.size else np.zeros((self.N, self.G))

    @property
    def prices(self) -> np.ndarray:
        return self._arr("price")

    def relation(self, mods: ev_mod.Modifiers, pa: str | None, pb: str | None) -> float:
        if not pa or not pb or pa == pb:
            return 1.0
        k = relation_key(pa, pb)
        return mods.relations.get(k, self.camp.relations.get(k, 0.0))

    # ------------------------------------------------------------------ tick
    def step(self, weeks: int = 1) -> None:
        for _ in range(weeks):
            self._tick()

    def warmup(self, weeks: int = 16) -> None:
        """Run the economy without advancing the calendar, to reach a steady state."""
        tick = self.camp.state.tick
        for _ in range(weeks):
            self._tick(record=False)
        self.camp.state.tick = tick
        self.camp.state.history.clear()
        self.camp.state.history_ticks.clear()
        self._record_history()

    def _tick(self, record: bool = True) -> None:
        c, s, st = self.camp, self.camp.setting, self.camp.state
        tick = st.tick
        if c.options.random_events and record:
            for e in ev_mod.random_events(c, tick, self.rng):
                c.events.append(e)
        mods = ev_mod.build_modifiers(c, tick, self.ix)
        self.illegal = self.legality(mods)
        price, M, I = self._arr("price"), self._arr("stock"), self._arr("input_stock")

        # player trades queued for this week
        buy_q = np.zeros((self.N, self.G))
        sell_q = np.zeros((self.N, self.G))
        for tr in st.pending_trades:
            i, k = self.ix.sys.get(tr.system), self.ix.good.get(tr.good)
            if i is None or k is None:
                continue
            if tr.quantity > 0:
                buy_q[i, k] += tr.quantity
            else:
                sell_q[i, k] += -tr.quantity
        st.pending_trades.clear()

        # production
        K = self.K * mods.production
        need = K @ self.recipe                                   # inputs wanted at capacity
        avail = np.where(need > 0, np.minimum(1.0, I / np.maximum(need, 1e-12)), 1.0)
        util = np.ones((self.N, self.G))
        for k in np.flatnonzero(self.manufactured):
            ins = np.flatnonzero(self.recipe[k])
            util[:, k] = avail[:, ins].min(1)
        Y = K * util
        I = np.maximum(0.0, I - Y @ self.recipe)

        Dc0 = self.D0 * mods.demand
        Di0 = np.maximum(0.0, INPUT_WEEKS * need - I)
        S0 = Y + STOCK_RELEASE * M
        B0 = BACKSTOP_SHARE * (Dc0 + Di0)

        # network pieces shared by all goods
        pol = self.ix.polity_of
        a, b = self.ix.lane_a, self.ix.lane_b
        rel_tariff = np.array([0.0 if not pol[i] or not pol[j] or pol[i] == pol[j]
                               else CUSTOMS + HOSTILE_TARIFF * max(0.0, -self.relation(mods, pol[i], pol[j]))
                               for i, j in zip(a, b)])
        lp = s.lanes
        tail = np.concatenate([a, b, self.off_a, self.off_b])
        head = np.concatenate([b, a, self.off_b, self.off_a])
        n_l, n_o = self.E, len(self.off_a)
        off_risk = lp.offlane_risk + mods.offlane_risk[self.off_a] + mods.offlane_risk[self.off_b]

        new_price = np.zeros_like(price)
        S_tot = np.zeros_like(price)
        D_tot = np.zeros_like(price)
        lane_net = np.zeros((self.E, self.G))
        off_net = np.zeros((n_o, self.G))
        for k, g in enumerate(s.goods):
            pr = self.pref[k]
            curves = Curves(
                supply=[(S0[:, k], SUPPLY_ELASTICITY), (B0[:, k], BACKSTOP_EXPONENT), (sell_q[:, k], 0.0)],
                demand=[(Dc0[:, k], self.eps[k]), (Di0[:, k], INPUT_ELASTICITY), (buy_q[:, k], 0.0)],
                p_ref=pr)
            tpu = self.tpu[k]
            lane_cap_units = self.lane_cap * mods.lane_capacity[:, k] * self.share[:, k] / tpu
            cap_l = np.concatenate([lane_cap_units, lane_cap_units])
            # contraband never moves on charted lanes into a system that bans it
            cap_l[self.illegal[np.concatenate([b, a]), k]] = 0.0
            lane_cost = (lp.freight_rate * self.lane_len * tpu + self.lane_toll
                         + (self.lane_risk + mods.lane_risk) * pr
                         + (rel_tariff + mods.lane_tariff[:, k]) * pr)
            cost_l = np.concatenate([lane_cost + self.port_fee[b] * tpu, lane_cost + self.port_fee[a] * tpu])
            off_cap = np.full(2 * n_o, lp.base_capacity * lp.offlane_capacity / self.G / tpu)
            off_cost = np.tile(lp.freight_rate * lp.offlane_factor * self.off_len * tpu + off_risk * pr, 2)
            net = Network(self.N, tail, head, np.concatenate([cap_l, off_cap]),
                          np.concatenate([cost_l, off_cost]))
            sol = solve(curves, net, p0=price[:, k], tau=0.01 * pr)
            p = sol.price
            new_price[:, k] = p
            S_tot[:, k] = sol.supply
            D_tot[:, k] = sol.demand
            f = sol.flow
            lane_net[:, k] = f[:n_l] - f[n_l:2 * n_l]
            off_net[:, k] = f[2 * n_l:2 * n_l + n_o] - f[2 * n_l + n_o:]
            x = p / pr
            sold = S0[:, k] * x ** SUPPLY_ELASTICITY
            M[:, k] = M[:, k] + Y[:, k] - sold
            I[:, k] += Di0[:, k] * x ** (-INPUT_ELASTICITY)
        M = np.clip(M * (1 - SPOILAGE), 0.0, STOCK_CAP_WEEKS * (Y + self.D0 + Di0) + 1e-9)
        self._update_shares(np.abs(lane_net) * self.tpu)

        st.market = MarketState(price=new_price.round(4).tolist(), stock=M.round(3).tolist(),
                                input_stock=I.round(3).tolist(), supply=S_tot.round(3).tolist(),
                                demand=D_tot.round(3).tolist())
        thr_l = np.maximum(1e-3, 1e-3 * np.abs(lane_net).max(0, keepdims=True, initial=0.0))
        st.flows = [LaneFlow(lane=c.lanes[e].id, good=s.goods[k].id, amount=round(float(lane_net[e, k]), 3))
                    for e, k in zip(*np.nonzero(np.abs(lane_net) > thr_l))]
        # keep only flows that matter (1% of the largest lane flow of that good)
        thr_o = np.maximum(1e-2, 1e-2 * np.abs(lane_net).max(0, initial=0.0)[None, :])
        st.smuggling = [LaneFlow(lane=f"{c.systems[self.off_a[o]].id}~{c.systems[self.off_b[o]].id}",
                                 good=s.goods[k].id, amount=round(float(off_net[o, k]), 3))
                        for o, k in zip(*np.nonzero(np.abs(off_net) > thr_o))]
        if record:
            st.tick += 1
            self._record_history()
            self.refresh_legality()          # quotes reflect the new week's laws and events

    def _record_history(self) -> None:
        st = self.camp.state
        p = self._arr("price")
        st.history_ticks.append(st.tick)
        for k, g in enumerate(self.camp.setting.goods):
            st.history.setdefault(g.id, []).append([round(float(v), 2) for v in p[:, k]])
        keep = self.camp.options.history_length
        if len(st.history_ticks) > keep:
            del st.history_ticks[:-keep]
            for g in st.history:
                del st.history[g][:-keep]

    # ------------------------------------------------------------------ queries
    def spread(self, i: int) -> float:
        return BASE_SPREAD + 0.0005 * self.port_fee[i]

    def depth(self, i: int, k: int) -> float:
        """Units per 1 currency of price change: S'(p) - D'(p) at the current price."""
        m = self.camp.state.market
        p = self.prices[i, k]
        S = np.array(m.supply).reshape(self.N, self.G)[i, k] if m.supply else self.K[i, k]
        D = np.array(m.demand).reshape(self.N, self.G)[i, k] if m.demand else self.D0[i, k]
        return max(1e-6, (SUPPLY_ELASTICITY * S + self.eps[k] * D) / p)

    def quote(self, system: str, good: str) -> Quote:
        i, k = self.ix.sys[system], self.ix.good[good]
        m = self.camp.state.market
        p = float(self.prices[i, k])
        sp = self.spread(i)
        sup = float(np.array(m.supply).reshape(self.N, self.G)[i, k]) if m.supply else 0.0
        dem = float(np.array(m.demand).reshape(self.N, self.G)[i, k]) if m.demand else 0.0
        return Quote(system, good, p, p * (1 + sp), p * (1 - sp), float(self._arr("stock")[i, k]),
                     sup, dem, not bool(self.illegal[i, k]))

    def market(self, system: str) -> list[Quote]:
        return [self.quote(system, g.id) for g in self.camp.setting.goods]

    # ------------------------------------------------------------------ actions
    def trade(self, system: str, good: str, quantity: float, note: str = "") -> Trade:
        """Players buy (quantity > 0) or sell (< 0).  Executes at the quoted price
        plus market impact, and feeds into next week's equilibrium."""
        i, k = self.ix.sys[system], self.ix.good[good]
        q = self.quote(system, good)
        impact = 0.5 * quantity / self.depth(i, k)
        base = q.buy if quantity > 0 else q.sell
        price = max(0.01, base + impact)
        tr = Trade(tick=self.camp.state.tick, system=system, good=good, quantity=quantity,
                   price=round(price, 2), note=note)
        self.camp.trades.append(tr)
        self.camp.state.pending_trades.append(tr)
        # move the displayed price immediately so repeated trades see the impact
        m = self.camp.state.market
        m.price[i][k] = round(float(self.prices[i, k] + 2 * impact), 4)
        return tr

    def add_event(self, event: Event) -> Event:
        if any(e.id == event.id for e in self.camp.events):
            raise ValueError(f"event id {event.id!r} already exists")
        self.camp.events.append(event)
        self.refresh_legality()
        return event

    def remove_event(self, event_id: str) -> None:
        self.camp.events = [e for e in self.camp.events if e.id != event_id]
        self.refresh_legality()

    def active_events(self) -> list[Event]:
        return [e for e in self.camp.events if e.active(self.camp.state.tick)]
