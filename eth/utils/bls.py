from typing import (  # noqa: F401
    Dict,
    Iterable,
    Tuple,
    Union,
)

from eth_utils import (
    big_endian_to_int,
)

from py_ecc.optimized_bls12_381 import (  # NOQA
    G1,
    G2,
    Z1,
    Z2,
    neg,
    add,
    multiply,
    FQ,
    FQ2,
    FQ12,
    FQP,
    pairing,
    normalize,
    field_modulus as q,
    b,
    b2,
    is_on_curve,
    curve_order,
    final_exponentiate
)
from eth.beacon.utils.hash import hash_


G2_cofactor = 305502333931268344200999753193121504214466019254188142667664032982267604182971884026507427359259977847832272839041616661285803823378372096355777062779109  # noqa: E501
qmod = q ** 2 - 1
eighth_roots_of_unity = [
    FQ2([1, 1]) ** ((qmod * k) // 8)
    for k in range(8)
]


#
# Helpers
#
def FQP_point_to_FQ2_point(pt: Tuple[FQP, FQP, FQP]) -> Tuple[FQ2, FQ2, FQ2]:
    """
    Transform FQP to FQ2 for type hinting.
    """
    return (
        FQ2(pt[0].coeffs),
        FQ2(pt[1].coeffs),
        FQ2(pt[2].coeffs),
    )


def modular_squareroot(value: int) -> int:
    """
    ``modular_squareroot(x)`` returns the value ``y`` such that ``y**2 % q == x``,
    and None if this is not possible. In cases where there are two solutions,
    the value with higher imaginary component is favored;
    if both solutions have equal imaginary component the value with higher real
    component is favored.
    """
    candidate_squareroot = value ** ((qmod + 8) // 16)
    check = candidate_squareroot ** 2 / value
    if check in eighth_roots_of_unity[::2]:
        x1 = candidate_squareroot / eighth_roots_of_unity[eighth_roots_of_unity.index(check) // 2]
        x2 = FQ2([-x1.coeffs[0], -x1.coeffs[1]])
        # x2 = - x2
        return x1 if (x1.coeffs[1], x1.coeffs[0]) > (x2.coeffs[1], x2.coeffs[0]) else x2
    return None


def hash_to_G2(message: bytes, domain: int) -> Tuple[FQ2, FQ2, FQ2]:
    domain_in_bytes = domain.to_bytes(8, 'big')
    x1 = big_endian_to_int(hash_(domain_in_bytes + b'\x01' + message))
    x2 = big_endian_to_int(hash_(domain_in_bytes + b'\x02' + message))
    x_coordinate = FQ2([x1, x2])  # x1 + x2 * i
    while 1:
        x_cubed_plus_b2 = x_coordinate ** 3 + FQ2([4, 4])
        y_coordinate = modular_squareroot(x_cubed_plus_b2)
        if y_coordinate is not None:
            break
        x_coordinate += FQ2([1, 0])  # Add one until we get a quadratic residue

    return multiply(
        (x_coordinate, y_coordinate, FQ2([1, 0])),
        G2_cofactor
    )


#
# G1
#
def compress_G1(pt: Tuple[FQ, FQ, FQ]) -> int:
    x, y = normalize(pt)
    return x.n + 2**383 * (y.n % 2)


def decompress_G1(p: int) -> Tuple[FQ, FQ, FQ]:
    if p == 0:
        return (FQ(1), FQ(1), FQ(0))
    x = p % 2**383
    y_mod_2 = p // 2**383
    y = pow((x**3 + b.n) % q, (q + 1) // 4, q)
    assert pow(y, 2, q) == (x**3 + b.n) % q
    if y % 2 != y_mod_2:
        y = q - y
    return (FQ(x), FQ(y), FQ(1))


#
# G2
#
def compress_G2(pt: Tuple[FQP, FQP, FQP]) -> Tuple[int, int]:
    assert is_on_curve(pt, b2)
    x, y = normalize(pt)
    return (
        int(x.coeffs[0] + 2**383 * (y.coeffs[0] % 2)),
        int(x.coeffs[1])
    )


def decompress_G2(p: bytes) -> Tuple[FQP, FQP, FQP]:
    x1 = p[0] % 2**383
    y1_mod_2 = p[0] // 2**383
    x2 = p[1]
    x = FQ2([x1, x2])
    if x == FQ2([0, 0]):
        return FQ2([1, 0]), FQ2([1, 0]), FQ2([0, 0])
    y = modular_squareroot(x**3 + b2)
    if y.coeffs[0] % 2 != y1_mod_2:
        y = FQ2((y * -1).coeffs)
    assert is_on_curve((x, y, FQ2([1, 0])), b2)
    return x, y, FQ2([1, 0])


#
# APIs
#
def sign(message: bytes,
         privkey: int,
         domain: int) -> Tuple[int, int]:
    return compress_G2(
        multiply(
            hash_to_G2(message, domain),
            privkey
        )
    )


def privtopub(k: int) -> int:
    return compress_G1(multiply(G1, k))


def verify(m: bytes, pub: int, sig: bytes, domain: int) -> bool:
    final_exponentiation = final_exponentiate(
        pairing(FQP_point_to_FQ2_point(decompress_G2(sig)), G1, False) *
        pairing(FQP_point_to_FQ2_point(hash_to_G2(m, domain)), neg(decompress_G1(pub)), False)
    )
    return final_exponentiation == FQ12.one()


def aggregate_sigs(sigs: Iterable[bytes]) -> Tuple[int, int]:
    o = Z2
    for s in sigs:
        o = FQP_point_to_FQ2_point(add(o, decompress_G2(s)))
    return compress_G2(o)


def aggregate_pubs(pubs: Iterable[int]) -> int:
    o = Z1
    for p in pubs:
        o = add(o, decompress_G1(p))
    return compress_G1(o)


def multi_verify(pubs, msgs, sig, domain):
    len_msgs = len(msgs)
    assert len(pubs) == len_msgs

    o = FQ12([1] + [0] * 11)
    for m in set(msgs):
        # aggregate the pubs
        group_pub = Z1
        for i in range(len_msgs):
            if msgs[i] == m:
                group_pub = add(group_pub, decompress_G1(pubs[i]))

        o *= pairing(hash_to_G2(m, domain), group_pub, False)
    o *= pairing(decompress_G2(sig), neg(G1), False)

    final_exponentiation = final_exponentiate(o)
    return final_exponentiation == FQ12.one()
