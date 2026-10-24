import math


def variance(xs):
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def std_dev(xs):
    return math.sqrt(variance(xs))


def value_range(xs):
    return max(xs) - min(xs)
