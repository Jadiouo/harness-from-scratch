import math


def deg_to_rad(deg):
    return deg * math.pi / 180


def normalize_angle(deg):
    return deg % 360
