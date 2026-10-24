def covariance(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)


def correlation(xs, ys):
    sx = covariance(xs, xs) ** 0.5
    sy = covariance(ys, ys) ** 0.5
    return covariance(xs, ys) / (sx * sy)
