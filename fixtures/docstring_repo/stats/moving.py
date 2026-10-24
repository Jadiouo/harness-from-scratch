def moving_average(xs, window):
    return [sum(xs[i:i + window]) / window for i in range(len(xs) - window + 1)]


def cumulative_sum(xs):
    out, total = [], 0
    for x in xs:
        total += x
        out.append(total)
    return out
