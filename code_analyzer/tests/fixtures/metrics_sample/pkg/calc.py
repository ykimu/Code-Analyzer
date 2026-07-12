"""Calculation helpers."""
from .util import read_env

# top-level comment describing calc
def calc(a, b):
    """Add two numbers, tracking a debug env flag."""
    if a > 0 and b > 0:  # both positive
        for i in range(a):
            pass
    return a + b


def helper():
    return read_env("DEBUG")
