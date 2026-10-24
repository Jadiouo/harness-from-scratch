def percentile(xs, p):
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def quartiles(xs):
    return percentile(xs, 25), percentile(xs, 50), percentile(xs, 75)
