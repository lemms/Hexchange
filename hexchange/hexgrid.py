"""Flat-topped hex grid in "odd-q" offset coordinates (column, row), the layout
used by most sci-fi sector maps: hex labels are ``CCRR`` with 1-based column
and row, and odd columns are shifted down by half a hex."""

from __future__ import annotations

import math


def label(col: int, row: int) -> str:
    return f"{col + 1:02d}{row + 1:02d}"


def offset_to_cube(col: int, row: int) -> tuple[int, int, int]:
    x = col
    z = row - (col - (col & 1)) // 2
    return x, -x - z, z


def distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    ax, ay, az = offset_to_cube(*a)
    bx, by, bz = offset_to_cube(*b)
    return max(abs(ax - bx), abs(ay - by), abs(az - bz))


def center(col: int, row: int, size: float = 1.0) -> tuple[float, float]:
    """Pixel centre of a flat-topped hex with circumradius ``size``."""
    x = size * 1.5 * col
    y = size * math.sqrt(3) * (row + 0.5 * (col & 1))
    return x, y


def within(col: int, row: int, radius: int, width: int, height: int):
    """All in-bounds hexes within ``radius`` of (col, row), excluding itself."""
    for c in range(max(0, col - radius), min(width, col + radius + 1)):
        for r in range(max(0, row - radius - 1), min(height, row + radius + 2)):
            if (c, r) != (col, row) and distance((col, row), (c, r)) <= radius:
                yield c, r
