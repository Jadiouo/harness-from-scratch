def count_vowels(s):
    return sum(1 for c in s.lower() if c in "aeiou")


def is_palindrome(s):
    cleaned = [c.lower() for c in s if c.isalnum()]
    return cleaned == cleaned[::-1]
