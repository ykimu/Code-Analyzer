"""Service layer that imports models (relative import)."""
from .models import User, make_user


def build(name):
    user = make_user(name)
    greeting = user.greet()
    return greeting


def direct(name):
    u = User(name)
    return u
