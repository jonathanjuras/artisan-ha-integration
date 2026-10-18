"""Rate of rise: least-squares slope over a sliding time window."""

from __future__ import annotations

from collections import deque

MIN_POINTS = 5
MIN_SPAN_FRACTION = 0.5


class RorCalculator:
    """Ring buffer of `(x, value)` points; `x` is seconds (roast time or monotonic)."""

    def __init__(self, window_s: float) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._window = window_s
        self._points: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self._points.clear()

    def add(self, x: float, value: float | None) -> None:
        if value is None:
            return
        if self._points and x < self._points[-1][0]:
            # The x-axis jumped backwards (timer restarted); old points would skew the fit.
            self._points.clear()
        self._points.append((x, value))
        cutoff = x - self._window
        while self._points[0][0] < cutoff:
            self._points.popleft()

    def value(self) -> float | None:
        """Slope in degrees per minute, one decimal, or None without enough data."""
        points = self._points
        n = len(points)
        if n < MIN_POINTS:
            return None
        if points[-1][0] - points[0][0] < self._window * MIN_SPAN_FRACTION:
            return None
        mean_x = sum(p[0] for p in points) / n
        mean_y = sum(p[1] for p in points) / n
        sxx = sum((p[0] - mean_x) ** 2 for p in points)
        sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
        # sxx > 0 is guaranteed by the span check above (window is positive).
        slope = sxy / sxx * 60.0
        return round(slope, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
