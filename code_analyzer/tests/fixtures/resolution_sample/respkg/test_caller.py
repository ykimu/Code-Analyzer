"""Test file (name starts with test_): same certain import-bound call as
main.py, but this file's call edge must be excluded from
resolution.calls.non_test."""
from .dup1 import shared


def test_it():
    assert shared() == 1
