def bounding_box(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def contains_point(box, point):
    x0, y0, x1, y1 = box
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1
