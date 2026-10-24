import math


def perimeter(points):
    total = 0.0
    for i, p in enumerate(points):
        q = points[(i + 1) % len(points)]
        total += math.hypot(p[0] - q[0], p[1] - q[1])
    return total


def centroid(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return sum(xs) / len(points), sum(ys) / len(points)
