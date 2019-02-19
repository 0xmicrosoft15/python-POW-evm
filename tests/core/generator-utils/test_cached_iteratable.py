from eth._utils.generator import CachedIterable

from eth_utils.toolz import (
    first,
    nth,
)
import itertools


def test_cached_generator():
    use_once = itertools.count()
    repeated_use = CachedIterable(use_once)

    for find_val in [1, 0, 10, 5]:
        assert find_val == nth(find_val, repeated_use)


def test_laziness():
    def crash_after_first_val():
        yield 1
        raise Exception("oops, iterated past first value")

    repeated_use = CachedIterable(crash_after_first_val())
    assert first(repeated_use) == 1
    assert first(repeated_use) == 1
