import math


def euclidean(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def manhattan(p, q):
    return abs(p[0] - q[0]) + abs(p[1] - q[1])
