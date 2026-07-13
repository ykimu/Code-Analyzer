"""Calls `shared()` via an explicit import binding: single candidate ->
certain."""
from .dup1 import shared


def run():
    return shared()
