import re


def snake_to_camel(s):
    head, *rest = s.split("_")
    return head + "".join(w.capitalize() for w in rest)


def camel_to_snake(s):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
