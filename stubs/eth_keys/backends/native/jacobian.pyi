from typing import Tuple

def inv(a: int, n: int) -> int: ...
def to_jacobian(p: Tuple[int, int]) -> Tuple[int, int, int]: ...
def jacobian_double(p: Tuple[int, int, int]) -> Tuple[int, int, int]: ...
def jacobian_add(p: Tuple[int, int, int], q: Tuple[int, int, int]) -> Tuple[int, int, int]: ...
def from_jacobian(p: Tuple[int, int, int]) -> Tuple[int, int]: ...
def jacobian_multiply(a: Tuple[int, int, int], n: int) -> Tuple[int, int, int]: ...
def fast_multiply(a: Tuple[int, int], n: int) -> Tuple[int, int]: ...
def fast_add(a, b): ...
