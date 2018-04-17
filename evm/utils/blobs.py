import itertools
import math
from io import (
    BytesIO,
)

from typing import (
    Iterable,
    Iterator,
)

from eth_utils import (
    apply_to_return_value,
    int_to_big_endian,
    keccak,
)
from evm.utils.padding import (
    zpad_right,
)

from evm.constants import (
    CHUNK_SIZE,
    CHUNK_DATA_SIZE,
    COLLATION_SIZE,
    MAX_BLOB_SIZE,
)

from evm.exceptions import (
    ValidationError,
)

from cytoolz import (
    partition,
    pipe,
)


def iterate_chunks(collation_body: bytes) -> Iterator[bytes]:
    check_body_size(collation_body)
    for chunk_start in range(0, len(collation_body), CHUNK_SIZE):
        yield collation_body[chunk_start:chunk_start + CHUNK_SIZE]


def hash_layer(layer: Iterable[bytes]) -> Iterator[bytes]:
    for left, right in partition(2, layer):
        yield keccak(left + right)


def calc_merkle_root(leaves: Iterable[bytes]) -> bytes:
    leaves = list(leaves)  # convert potential iterator to list
    if len(leaves) == 0:
        raise ValidationError("No leaves given")

    n_layers = math.log2(len(leaves))
    if not n_layers.is_integer():
        raise ValidationError("Leave number is not a power of two")
    n_layers = int(n_layers)

    first_layer = (keccak(leaf) for leaf in leaves)

    root, *extras = pipe(first_layer, *itertools.repeat(hash_layer, n_layers))
    assert not extras, "Invariant: should only be a single value"
    return root


def calc_chunk_root(collation_body: bytes) -> bytes:
    check_body_size(collation_body)
    chunks = iterate_chunks(collation_body)
    return calc_merkle_root(chunks)


def check_body_size(body):
    if len(body) != COLLATION_SIZE:
        raise ValidationError("{} byte collation body exceeds maximum allowed size".format(
            len(body)
        ))
    return body


@apply_to_return_value(check_body_size)
@apply_to_return_value(lambda v: zpad_right(v, COLLATION_SIZE))
@apply_to_return_value(b"".join)
def serialize_blobs(blobs: Iterable[bytes]) -> Iterator[bytes]:
    """Serialize a sequence of blobs and return a collation body."""
    for i, blob in enumerate(blobs):
        if len(blob) == 0:
            raise ValidationError("Cannot serialize blob {} of length 0".format(i))
        if len(blob) > MAX_BLOB_SIZE:
            raise ValidationError("Cannot serialize blob {} of size {}".format(i, len(blob)))

        for blob_index in range(0, len(blob), CHUNK_DATA_SIZE):
            remaining_blob_bytes = len(blob) - blob_index

            if remaining_blob_bytes <= CHUNK_DATA_SIZE:
                length_bits = remaining_blob_bytes
            else:
                length_bits = 0

            flag_bits = 0  # TODO: second parameter? blobs as tuple `(flag, blob)`?
            indicator_byte = int_to_big_endian(length_bits | (flag_bits << 5))
            assert len(indicator_byte) == 1

            yield indicator_byte
            yield blob[blob_index:blob_index + CHUNK_DATA_SIZE]

        # end of range(0, N, k) is given by the largest multiple of k smaller than N, i.e.,
        # (ceil(N / k) - 1) * k where ceil(N / k) == -(-N // k)
        last_blob_index = (-(-len(blob) // CHUNK_DATA_SIZE) - 1) * CHUNK_DATA_SIZE
        assert last_blob_index == blob_index
        chunk_filler = b"\x00" * (CHUNK_DATA_SIZE - (len(blob) - last_blob_index))
        yield chunk_filler


def deserialize_blobs(body: bytes) -> Iterator[bytes]:
    """Deserialize the blobs encoded in a body, returning and iterator."""
    blob = BytesIO()
    for chunk in iterate_chunks(body):
        indicator_byte = chunk[0]
        flag_bits = indicator_byte & 0b11100000  # TODO: yield, filter, ...?  # noqa: F841
        length_bits = indicator_byte & 0b00011111

        if length_bits == 0:
            length = CHUNK_DATA_SIZE
            terminal = False
        else:
            length = length_bits
            terminal = True

        blob.write(chunk[1:length + 1])
        if terminal:
            yield blob.getvalue()
            blob = BytesIO()
