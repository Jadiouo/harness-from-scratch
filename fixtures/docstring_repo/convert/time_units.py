def seconds_to_hms(sec):
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def hms_to_seconds(text):
    h, m, s = (int(p) for p in text.split(":"))
    return h * 3600 + m * 60 + s
