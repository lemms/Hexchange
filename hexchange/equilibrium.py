"""Spatial price equilibrium on a graph (Samuelson 1952; Takayama & Judge 1964).

For one good, markets sit at the nodes with supply ``S_i(p)`` and demand
``D_i(p)``; directed arcs ``a = (t -> h)`` carry flow ``0 <= f_a <= cap_a`` at
unit cost ``c_a``.  Equilibrium means

* every market clears:   S_i - D_i = outflow_i - inflow_i
* if f_a > 0 then        p_h = p_t + c_a     (nobody leaves profit on the table)
* if p_h < p_t + c_a     then f_a = 0        (shipping would lose money)
* if p_h > p_t + c_a     then f_a = cap_a    (lane saturated, price cliff)

These are the optimality conditions of minimising, over node prices p,

    F(p) = sum_i [PS_i(p_i) + CS_i(p_i)] + sum_a cap_a * max(0, p_h - p_t - c_a)

where PS' = S and CS' = -D.  We smooth the hinge with a softplus of width
``tau`` (flows become ``cap * sigmoid((p_h - p_t - c)/tau)``) and minimise with
Newton's method.  The Hessian is a diagonal (local market stiffness) plus a
weighted graph Laplacian over the lanes -- the same structure as a finite
element stiffness matrix, which is why price fields behave like a diffusing
potential with friction.

Curves are sums of power-law terms ``A * (p/p_ref)^k`` (k > 0 supply, k < 0
demand), which keep F convex for p > 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve


@dataclass
class Curves:
    """Per-node curve terms: list of (amplitude array, exponent) pairs."""

    supply: list[tuple[np.ndarray, float]]
    demand: list[tuple[np.ndarray, float]]
    p_ref: float

    def quantities(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """S, D and their price derivatives."""
        x = p / self.p_ref
        S = np.zeros_like(p)
        dS = np.zeros_like(p)
        D = np.zeros_like(p)
        dD = np.zeros_like(p)
        for A, k in self.supply:
            q = A * x ** k
            S += q
            dS += k * q / p
        for A, e in self.demand:
            q = A * x ** (-e)
            D += q
            dD += -e * q / p
        return S, D, dS, dD

    def potential(self, p: np.ndarray) -> np.ndarray:
        """PS + CS per node (up to constants)."""
        x = p / self.p_ref
        out = np.zeros_like(p)
        for A, k in self.supply:
            out += A * self.p_ref * x ** (1 + k) / (1 + k)
        for A, e in self.demand:
            if abs(e - 1.0) < 1e-9:
                out += -A * self.p_ref * np.log(x)
            elif e == 0:
                out += -A * p
            else:
                out += A * self.p_ref * x ** (1 - e) / (e - 1)
        return out


@dataclass
class Network:
    n: int
    tail: np.ndarray
    head: np.ndarray
    cap: np.ndarray
    cost: np.ndarray


@dataclass
class Solution:
    price: np.ndarray
    flow: np.ndarray
    supply: np.ndarray
    demand: np.ndarray
    iterations: int
    residual: float


def _softplus(z):
    return np.where(z > 30, z, np.log1p(np.exp(np.minimum(z, 30))))


def _sigmoid(z):
    return 0.5 * (1 + np.tanh(0.5 * z))


def solve(curves: Curves, net: Network, p0: np.ndarray | None = None, tau: float | None = None,
          max_iter: int = 60, tol: float = 1e-6) -> Solution:
    n = net.n
    pr = curves.p_ref
    tau = tau if tau is not None else 0.01 * pr
    p = np.full(n, pr) if p0 is None else np.clip(np.asarray(p0, float), pr * 1e-3, pr * 1e3)
    t, h, cap, c = net.tail, net.head, net.cap, net.cost
    active = cap > 0
    t, h, cap, c = t[active], h[active], cap[active], c[active]

    # weak pin towards p_ref keeps nodes without any market well-posed
    scale = 0.0
    for A, _ in curves.supply + curves.demand:
        scale += float(np.mean(A))
    mu = 1e-6 * max(scale, 1e-9) / pr

    def objective(p):
        z = (p[h] - p[t] - c) / tau
        return (curves.potential(p).sum() + float(np.sum(cap * tau * _softplus(z)))
                + 0.5 * mu * float(np.sum((p - pr) ** 2)))

    def flows(p):
        z = (p[h] - p[t] - c) / tau
        return cap * _sigmoid(z), z

    F = objective(p)
    it = 0
    res = np.inf
    for it in range(1, max_iter + 1):
        S, D, dS, dD = curves.quantities(p)
        f, z = flows(p)
        g = S - D + mu * (p - pr)
        np.add.at(g, h, f)
        np.add.at(g, t, -f)
        volume = S + D + 1e-9
        np.add.at(volume, h, f)
        np.add.at(volume, t, f)
        res = float(np.max(np.abs(g) / volume))
        if res < tol:
            break
        sig = _sigmoid(z)
        w = cap * sig * (1 - sig) / tau
        diag = dS - dD + mu
        # links far from their switching point contribute ~nothing to the Hessian;
        # leaving them out keeps the matrix sparse (most smuggler links are idle)
        live = w > 1e-9 * max(float(w.max(initial=0.0)), float(diag.max(initial=0.0)), 1e-300)
        tl, hl, wl = t[live], h[live], w[live]
        rows = np.concatenate([np.arange(n), tl, hl, tl, hl])
        cols = np.concatenate([np.arange(n), tl, hl, hl, tl])
        vals = np.concatenate([diag, wl, wl, -wl, -wl])
        H = coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
        step = -spsolve(H, g)
        # keep prices positive: at most a 5x fall per Newton step
        neg = step < 0
        alpha = 1.0
        if neg.any():
            alpha = min(1.0, float(np.min(0.8 * p[neg] / -step[neg])))
        while True:
            p_new = p + alpha * step
            F_new = objective(p_new)
            if F_new <= F + 1e-4 * alpha * float(g @ step) or alpha < 1e-8:
                break
            alpha *= 0.5
        if alpha < 1e-8:
            break
        p, F = p_new, F_new
    S, D, _, _ = curves.quantities(p)
    f_all = np.zeros(len(net.cap))
    f_all[active] = flows(p)[0]
    return Solution(price=p, flow=f_all, supply=S, demand=D, iterations=it, residual=res)
