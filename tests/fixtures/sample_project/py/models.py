"""Domain models."""


class Base:
    def describe(self):
        return "base"


class User(Base):
    def __init__(self, name):
        self.name = name

    def greet(self):
        prefix = self.describe()
        return prefix + self.name


def make_user(name):
    u = User(name)
    return u
