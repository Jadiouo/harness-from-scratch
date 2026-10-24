def center_text(s, width, fill=" "):
    return s.center(width, fill)


def indent_lines(s, prefix="    "):
    return "\n".join(prefix + line for line in s.splitlines())
