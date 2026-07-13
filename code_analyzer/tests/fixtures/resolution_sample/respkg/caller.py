"""Calls `shared()` by bare name with no import binding: project-wide
name lookup finds two candidates (dup1.shared, dup2.shared) -> ambiguous
inferred edges (one per candidate)."""


def use():
    return shared()
