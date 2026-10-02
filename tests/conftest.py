import pytest

import hexchange as hx


@pytest.fixture(scope="session")
def setting():
    return hx.load_setting("generic")


@pytest.fixture()
def small(setting):
    """A small warmed-up sector (fresh per test, since tests mutate it)."""
    camp = hx.generate(setting, name="Test", width=16, height=16, density=0.45, polities=3, seed=3)
    sim = hx.Simulation(camp)
    sim.warmup(10)
    return sim
