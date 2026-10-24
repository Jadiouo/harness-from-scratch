def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def add_vec(a, b):
    return [x + y for x, y in zip(a, b)]


def scale_vec(a, k):
    return [x * k for x in a]
