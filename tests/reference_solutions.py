"""A known-good and a known-bad solution for every task in experiments/tasks.json.

Measurement 5 takes the test suite as ground truth, so the suite has to be
able to tell the two apart: the good one must pass and the bad one must fail.
tests/test_measurement_disagreement.py runs both through the real sandbox.

Each bad solution is a plausible mistake -- an off-by-one, a missed edge case,
an ignored constraint -- not a syntax error. That is the class of bug a
reviewer can wave through, which is what the measurement counts.
"""
from __future__ import annotations

from textwrap import dedent


def _code(source: str) -> str:
    return dedent(source).strip() + "\n"


#: task id -> (good, bad)
REFERENCES: dict[str, tuple[str, str]] = {
    "t01": (_code("""
        def binary_search(items, target):
            lo, hi = 0, len(items) - 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if items[mid] == target:
                    return mid
                if items[mid] < target:
                    lo = mid + 1
                else:
                    hi = mid - 1
            return -1
    """), _code("""
        def binary_search(items, target):
            lo, hi = 0, len(items) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if items[mid] == target:
                    return mid
                if items[mid] < target:
                    lo = mid + 1
                else:
                    hi = mid - 1
            return -1
    """)),
    "t02": (_code("""
        def flatten(nested):
            out, stack = [], [iter(nested)]
            while stack:
                for item in stack[-1]:
                    if isinstance(item, list):
                        stack.append(iter(item))
                        break
                    out.append(item)
                else:
                    stack.pop()
            return out
    """), _code("""
        def flatten(nested):
            out = []
            for item in nested:
                if isinstance(item, list):
                    out.extend(flatten(item))
                else:
                    out.append(item)
            return out
    """)),
    "t03": (_code("""
        from collections import OrderedDict

        class LRUCache:
            def __init__(self, capacity):
                self.capacity = capacity
                self.data = OrderedDict()

            def get(self, key):
                if key not in self.data:
                    return -1
                self.data.move_to_end(key)
                return self.data[key]

            def put(self, key, value):
                self.data[key] = value
                self.data.move_to_end(key)
                if len(self.data) > self.capacity:
                    self.data.popitem(last=False)
    """), _code("""
        from collections import OrderedDict

        class LRUCache:
            def __init__(self, capacity):
                self.capacity = capacity
                self.data = OrderedDict()

            def get(self, key):
                return self.data.get(key, -1)

            def put(self, key, value):
                self.data[key] = value
                if len(self.data) > self.capacity:
                    self.data.popitem(last=False)
    """)),
    "t04": (_code("""
        def is_palindrome(text):
            chars = [c.lower() for c in text if c.isalnum()]
            return chars == chars[::-1]
    """), _code("""
        def is_palindrome(text):
            text = text.lower()
            return text == text[::-1]
    """)),
    "t05": (_code("""
        def fizzbuzz(n):
            out = []
            for i in range(1, n + 1):
                if i % 15 == 0:
                    out.append("FizzBuzz")
                elif i % 3 == 0:
                    out.append("Fizz")
                elif i % 5 == 0:
                    out.append("Buzz")
                else:
                    out.append(str(i))
            return out
    """), _code("""
        def fizzbuzz(n):
            out = []
            for i in range(1, n + 1):
                if i % 3 == 0:
                    out.append("Fizz")
                elif i % 5 == 0:
                    out.append("Buzz")
                elif i % 15 == 0:
                    out.append("FizzBuzz")
                else:
                    out.append(str(i))
            return out
    """)),
    "t06": (_code("""
        import re
        from collections import Counter

        def word_count(text):
            return dict(Counter(re.findall(r"[a-z]+", text.lower())))
    """), _code("""
        import re
        from collections import Counter

        def word_count(text):
            return dict(Counter(re.findall(r"[A-Za-z]+", text)))
    """)),
    "t07": (_code("""
        def two_sum(nums, target):
            seen = {}
            for j, value in enumerate(nums):
                if target - value in seen:
                    return (seen[target - value], j)
                seen.setdefault(value, j)
            return None
    """), _code("""
        def two_sum(nums, target):
            for i in range(len(nums)):
                for j in range(i, len(nums)):
                    if nums[i] + nums[j] == target:
                        return (i, j)
            return None
    """)),
    "t08": (_code("""
        def merge_intervals(intervals):
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            return merged
    """), _code("""
        def merge_intervals(intervals):
            merged = []
            for start, end in intervals:
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            return merged
    """)),
    "t09": (_code("""
        VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

        def roman_to_int(numeral):
            total = 0
            for i, ch in enumerate(numeral):
                value = VALUES[ch]
                if i + 1 < len(numeral) and VALUES[numeral[i + 1]] > value:
                    total -= value
                else:
                    total += value
            return total
    """), _code("""
        VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

        def roman_to_int(numeral):
            return sum(VALUES[ch] for ch in numeral)
    """)),
    "t10": (_code("""
        TABLE = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                 (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]

        def int_to_roman(number):
            out = []
            for value, symbol in TABLE:
                while number >= value:
                    out.append(symbol)
                    number -= value
            return "".join(out)
    """), _code("""
        TABLE = [(1000, "M"), (500, "D"), (100, "C"), (50, "L"), (10, "X"),
                 (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]

        def int_to_roman(number):
            out = []
            for value, symbol in TABLE:
                while number >= value:
                    out.append(symbol)
                    number -= value
            return "".join(out)
    """)),
    "t11": (_code("""
        def is_balanced(text):
            pairs = {")": "(", "]": "[", "}": "{"}
            stack = []
            for ch in text:
                if ch in "([{":
                    stack.append(ch)
                elif ch in pairs:
                    if not stack or stack.pop() != pairs[ch]:
                        return False
            return not stack
    """), _code("""
        def is_balanced(text):
            depth = {"(": 0, "[": 0, "{": 0}
            closing = {")": "(", "]": "[", "}": "{"}
            for ch in text:
                if ch in depth:
                    depth[ch] += 1
                elif ch in closing:
                    depth[closing[ch]] -= 1
                    if depth[closing[ch]] < 0:
                        return False
            return all(v == 0 for v in depth.values())
    """)),
    "t12": (_code("""
        def gcd(a, b):
            a, b = abs(a), abs(b)
            while b:
                a, b = b, a % b
            return a
    """), _code("""
        def gcd(a, b):
            while b:
                a, b = b, a % b
            return a
    """)),
    "t13": (_code("""
        def is_prime(n):
            if n < 2:
                return False
            i = 2
            while i * i <= n:
                if n % i == 0:
                    return False
                i += 1
            return True
    """), _code("""
        def is_prime(n):
            if n < 2:
                return False
            for i in range(2, int(n ** 0.5)):
                if n % i == 0:
                    return False
            return True
    """)),
    "t14": (_code("""
        def fibonacci(n):
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return a
    """), _code("""
        def fibonacci(n):
            a, b = 0, 1
            for _ in range(n - 1):
                a, b = b, a + b
            return b
    """)),
    "t15": (_code("""
        def rotate_matrix(matrix):
            return [list(row) for row in zip(*matrix[::-1])]
    """), _code("""
        def rotate_matrix(matrix):
            return [list(row) for row in zip(*matrix)][::-1]
    """)),
    "t16": (_code("""
        def chunk(items, size):
            if size < 1:
                raise ValueError("size must be at least 1")
            return [items[i:i + size] for i in range(0, len(items), size)]
    """), _code("""
        def chunk(items, size):
            if size < 1:
                raise ValueError("size must be at least 1")
            return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]
    """)),
    "t17": (_code("""
        def run_length_encode(text):
            out = []
            i = 0
            while i < len(text):
                j = i
                while j < len(text) and text[j] == text[i]:
                    j += 1
                out.append(f"{text[i]}{j - i}")
                i = j
            return "".join(out)
    """), _code("""
        def run_length_encode(text):
            out = []
            count = 1
            for i in range(1, len(text)):
                if text[i] == text[i - 1]:
                    count += 1
                else:
                    out.append(f"{text[i - 1]}{count}")
                    count = 1
            return "".join(out)
    """)),
    "t18": (_code("""
        def caesar_cipher(text, shift):
            out = []
            for ch in text:
                if "a" <= ch <= "z":
                    out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
                elif "A" <= ch <= "Z":
                    out.append(chr((ord(ch) - 65 + shift) % 26 + 65))
                else:
                    out.append(ch)
            return "".join(out)
    """), _code("""
        def caesar_cipher(text, shift):
            out = []
            for ch in text.lower():
                if "a" <= ch <= "z":
                    out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
                else:
                    out.append(ch)
            return "".join(out)
    """)),
    "t19": (_code("""
        from collections import Counter

        def are_anagrams(a, b):
            def norm(s):
                return Counter(s.replace(" ", "").lower())
            return norm(a) == norm(b)
    """), _code("""
        def are_anagrams(a, b):
            return set(a.replace(" ", "").lower()) == set(b.replace(" ", "").lower())
    """)),
    "t20": (_code("""
        def median(nums):
            ordered = sorted(nums)
            mid = len(ordered) // 2
            if len(ordered) % 2:
                return ordered[mid]
            return (ordered[mid - 1] + ordered[mid]) / 2
    """), _code("""
        def median(nums):
            nums.sort()
            mid = len(nums) // 2
            if len(nums) % 2:
                return nums[mid]
            return (nums[mid - 1] + nums[mid]) / 2
    """)),
    "t21": (_code("""
        def second_largest(nums):
            distinct = sorted(set(nums))
            return distinct[-2] if len(distinct) >= 2 else None
    """), _code("""
        def second_largest(nums):
            if len(nums) < 2:
                return None
            return sorted(nums)[-2]
    """)),
    "t22": (_code("""
        def longest_common_prefix(strings):
            if not strings:
                return ""
            prefix = strings[0]
            for s in strings[1:]:
                while not s.startswith(prefix):
                    prefix = prefix[:-1]
            return prefix
    """), _code("""
        def longest_common_prefix(strings):
            if not strings:
                return ""
            if len(strings) == 1:
                return strings[0]
            a, b = strings[0], strings[1]
            i = 0
            while i < min(len(a), len(b)) and a[i] == b[i]:
                i += 1
            return a[:i]
    """)),
    "t23": (_code("""
        def dedupe(items):
            seen = set()
            out = []
            for item in items:
                if item not in seen:
                    seen.add(item)
                    out.append(item)
            return out
    """), _code("""
        def dedupe(items):
            return list(set(items))
    """)),
    "t24": (_code("""
        def matrix_multiply(a, b):
            if not a or not b or any(len(row) != len(b) for row in a):
                raise ValueError("incompatible shapes")
            cols = list(zip(*b))
            return [[sum(x * y for x, y in zip(row, col)) for col in cols] for row in a]
    """), _code("""
        def matrix_multiply(a, b):
            cols = list(zip(*b))
            return [[sum(x * y for x, y in zip(row, col)) for col in cols] for row in a]
    """)),
    "t25": (_code("""
        def levenshtein(a, b):
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
                prev = cur
            return prev[-1]
    """), _code("""
        def levenshtein(a, b):
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    same = prev[j - 1] if ca == cb else float("inf")
                    cur.append(min(prev[j] + 1, cur[j - 1] + 1, same))
                prev = cur
            return prev[-1]
    """)),
    "t26": (_code("""
        def is_leap_year(year):
            return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    """), _code("""
        def is_leap_year(year):
            return year % 4 == 0 and year % 100 != 0
    """)),
    "t27": (_code("""
        from collections import Counter

        def top_k_frequent(nums, k):
            counts = Counter(nums)
            return sorted(counts, key=lambda v: (-counts[v], v))[:k]
    """), _code("""
        from collections import Counter

        def top_k_frequent(nums, k):
            return [value for value, _ in Counter(nums).most_common(k)]
    """)),
    "t28": (_code("""
        import csv

        def parse_csv_line(line):
            return next(csv.reader([line]))
    """), _code("""
        def parse_csv_line(line):
            return [field.strip('"') for field in line.split(",")]
    """)),
    "t29": (_code("""
        def max_subarray_sum(nums):
            best = current = nums[0]
            for value in nums[1:]:
                current = max(value, current + value)
                best = max(best, current)
            return best
    """), _code("""
        def max_subarray_sum(nums):
            best = current = 0
            for value in nums:
                current = max(0, current + value)
                best = max(best, current)
            return best
    """)),
    "t30": (_code("""
        def topo_sort(deps):
            order, done, active = [], set(), set()

            def visit(node):
                if node in done:
                    return
                if node in active:
                    raise ValueError(f"cycle through {node!r}")
                active.add(node)
                for dep in deps.get(node, []):
                    visit(dep)
                active.discard(node)
                done.add(node)
                order.append(node)

            for node in deps:
                visit(node)
            return order
    """), _code("""
        def topo_sort(deps):
            order, seen = [], set()

            def visit(node):
                if node in seen:
                    return
                seen.add(node)
                for dep in deps.get(node, []):
                    visit(dep)
                order.append(node)

            for node in deps:
                visit(node)
            return order
    """)),
}
