"""Rate-of-rise thresholds and math."""

from __future__ import annotations

import pytest

from artisan_ha_bridge.ror import RorCalculator


def feed(calc: RorCalculator, points: list[tuple[float, float]]) -> None:
    for x, y in points:
        calc.add(x, y)


def test_needs_five_points() -> None:
    calc = RorCalculator(30.0)
    feed(calc, [(0.0, 100.0), (5.0, 105.0), (10.0, 110.0), (15.0, 115.0)])
    assert calc.value() is None
    calc.add(20.0, 120.0)
    assert calc.value() == 60.0


def test_needs_half_window_span() -> None:
    calc = RorCalculator(30.0)
    feed(calc, [(float(i), 100.0 + i) for i in range(15)])  # spans 14 s < 15 s
    assert calc.value() is None
    calc.add(15.0, 115.0)  # spans exactly 15 s = 50%
    assert calc.value() == 60.0


def test_window_drops_old_points() -> None:
    calc = RorCalculator(10.0)
    # Fast rise first, then flat: only the flat tail is inside the window.
    feed(calc, [(float(i), 10.0 * i) for i in range(10)])
    feed(calc, [(float(i), 90.0) for i in range(10, 25)])
    assert calc.value() == 0.0


def test_least_squares_with_noise_and_rounding() -> None:
    calc = RorCalculator(30.0)
    noise = [0.1, -0.1, 0.05, -0.05, 0.0, 0.1, -0.1, 0.02, -0.03]
    feed(calc, [(2.0 * i, 150.0 + 0.2 * 2.0 * i + noise[i]) for i in range(9)])
    value = calc.value()
    assert value is not None
    assert value == pytest.approx(12.0, abs=0.6)
    assert round(value, 1) == value


def test_negative_zero_normalized() -> None:
    calc = RorCalculator(10.0)
    feed(calc, [(float(i), 100.0 - 1e-9 * i) for i in range(10)])
    value = calc.value()
    assert value == 0.0
    assert str(value) == "0.0"


def test_none_values_ignored_and_reset() -> None:
    calc = RorCalculator(10.0)
    feed(calc, [(float(i), 100.0 + i) for i in range(10)])
    calc.add(11.0, None)
    assert calc.value() == 60.0
    calc.reset()
    assert calc.value() is None


def test_backwards_x_clears_buffer() -> None:
    calc = RorCalculator(10.0)
    feed(calc, [(100.0 + i, 100.0 + i) for i in range(10)])
    assert calc.value() == 60.0
    calc.add(0.0, 50.0)
    assert calc.value() is None


def test_invalid_window() -> None:
    with pytest.raises(ValueError, match="positive"):
        RorCalculator(0.0)
