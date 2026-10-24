import re


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return s.strip("-")


def truncate(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"
