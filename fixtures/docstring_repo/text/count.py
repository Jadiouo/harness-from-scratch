from collections import Counter


def word_count(s):
    return len(s.split())


def char_freq(s):
    return dict(Counter(s))


def longest_word(s):
    words = s.split()
    return max(words, key=len) if words else ""
