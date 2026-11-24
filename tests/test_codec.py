import json
import os
import struct
from pathlib import Path

import pytest

from dvrbridge.dvr9000 import codec

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "codec_vectors.json").read_text())


@pytest.mark.parametrize("v", VECTORS, ids=lambda v: f"n{len(v['plain']) // 2}")
def test_known_answer_vectors(v):
    plain, encoded = bytes.fromhex(v["plain"]), bytes.fromhex(v["encoded"])
    assert codec.encode(plain) == encoded
    assert codec.decode(encoded) == plain


def test_worked_example_from_the_spec():
    # docs/protocol-codec.md, n = 2: output stream bit j takes input stream bit src[j]
    src = [4, 7, 10, 13, 0, 3, 8, 12, 1, 6, 14, 5, 15, 11, 2, 9]
    perm = codec.permutation(2)
    to_table = lambda p: (p & ~7) | (7 - (p & 7))  # noqa: E731
    for j, p in enumerate(src):
        assert perm[to_table(p)] == to_table(j)


@pytest.mark.parametrize("n", [2, 3, 4, 5, 7, 14, 16, 36, 68, 80, 136, 256, 1232, 65537])
def test_round_trip(n):
    data = bytes((i * 37 + 11) & 0xFF for i in range(n))
    assert codec.decode(codec.encode(data)) == data
    assert codec.encode(codec.decode(data)) == data


def test_random_round_trip():
    for n in (9, 33, 100, 515, 1 << 20):
        data = os.urandom(n)
        assert codec.decode(codec.encode(data)) == data


@pytest.mark.parametrize("n", [2, 3, 10, 99, 300])
def test_permutation_is_a_bijection(n):
    assert sorted(codec.permutation(n)) == list(range(8 * n))


def test_captured_login_header_vector():
    raw = bytes.fromhex("02000601000000400000040080010001")
    assert raw[0] & 3 == 2
    plain = raw[:2] + codec.decode(raw[2:16])
    b0, b1, ack, mtype, sub, size, seq = struct.unpack("<BBHHHII", plain)
    assert (mtype, sub, size, seq, ack) == (0x1F, 0, 80, 1, 0)
    assert b0 & 3 == 2


def test_empty_passes_through_and_one_byte_is_refused():
    assert codec.decode(b"") == b"" and codec.encode(b"") == b""
    with pytest.raises(ValueError):
        codec.encode(b"x")
    with pytest.raises(ValueError):
        codec.decode(b"x")


def test_accepts_bytearray_and_memoryview():
    data = bytes(range(40))
    assert codec.encode(bytearray(data)) == codec.encode(memoryview(data)) == codec.encode(data)
