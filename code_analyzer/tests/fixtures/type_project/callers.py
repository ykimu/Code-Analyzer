from typing import Optional

from alpha import Alpha
from beta import Beta


def make() -> Alpha:
    return Alpha()


def helper():
    return 1


def by_annotation(x: Alpha):
    return x.save()


def by_string(x: "Alpha"):
    return x.save()


def by_optional(x: Optional[Alpha]):
    return x.save()


def by_constructor():
    y = Beta()
    return y.save()


def by_direct():
    return Alpha.save()


def by_return():
    return make().save()


def by_unknown(z):
    return z.save()


def by_shadow():
    x = Alpha()
    x = helper()
    return x.save()
