"""Shared fixtures."""

from __future__ import annotations

import pytest

from helpers import FakeClock, Recorder


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()
