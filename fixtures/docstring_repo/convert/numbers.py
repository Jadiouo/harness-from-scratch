def to_binary(n):
    return bin(n)[2:]


def from_binary(text):
    return int(text, 2)


def to_roman(n):
    table = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
             (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for value, symbol in table:
        while n >= value:
            out += symbol
            n -= value
    return out
