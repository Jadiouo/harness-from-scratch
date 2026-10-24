UNITS = ["B", "KB", "MB", "GB", "TB"]


def human_size(n):
    size = float(n)
    for unit in UNITS:
        if size < 1024 or unit == UNITS[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024


def parse_size(text):
    number, unit = text.split()
    return int(float(number) * 1024 ** UNITS.index(unit))
