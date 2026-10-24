def min_max(xs):
    lo, hi = min(xs), max(xs)
    return [(x - lo) / (hi - lo) for x in xs] if hi != lo else [0.0 for _ in xs]


def z_scores(xs):
    m = sum(xs) / len(xs)
    sd = (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5
    return [(x - m) / sd for x in xs] if sd else [0.0 for _ in xs]
