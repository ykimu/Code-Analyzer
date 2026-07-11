"""Utility helpers."""
import os


def read_env(name):
    return os.environ.get(name)
