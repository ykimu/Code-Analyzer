"""Tests for calc."""
from .calc import calc


def test_calc_adds():
    assert calc(2, 3) == 5
