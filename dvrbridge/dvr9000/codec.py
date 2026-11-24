"""The port-9000 protocol's "mode 2" obfuscation: a keyless, length-dependent bit permutation.

The buffer is read as one MSB-first bit string. ``encode`` emits the bits in
the order a step-3 Josephus elimination removes them from a circle of all
``8 * len(data)`` bits, with counting starting at bit 2. ``decode`` restores
the original order. See docs/protocol-codec.md for the full description.

Both directions work one lap of the circle at a time with C-level slicing,
so a megabyte buffer takes about 40 laps of linear work. The lap schedule
(which depends only on the length) is cached.
"""
from __future__ import annotations

from functools import lru_cache
from typing import MutableSequence, TypeVar

__all__ = ["encode", "decode", "permutation"]

_START = 2  # the count of 1 falls on stream position 2, so position 4 goes first

S = TypeVar("S", bound=MutableSequence)


@lru_cache(maxsize=512)
def _lap_lengths(nbits: int) -> tuple[int, ...]:
    """Circle size at the start of each full lap (every size >= 3)."""
    lengths = []
    size = nbits
    while size >= 3:
        lengths.append(size)
        size -= size // 3
    return tuple(lengths)


def _deal(seq: S) -> S:
    """Return the elements of ``seq`` in elimination order (docs/protocol-codec.md, section 4)."""
    seq = seq[_START:] + seq[:_START]
    out = seq[:0]
    while len(seq) >= 3:
        m = len(seq) // 3
        out += seq[2::3]
        del seq[2::3]
        seq = seq[2 * m:] + seq[: 2 * m]
    out += seq
    return out


def _undeal(seq: S) -> S:
    """Inverse of ``_deal`` (docs/protocol-codec.md, "Inverting a lap")."""
    nbits = len(seq)
    laps = _lap_lengths(nbits)
    chunks = []
    pos = 0
    for size in laps:
        m = size // 3
        chunks.append(seq[pos:pos + m])
        pos += m
    cur = seq[pos:]
    for size, chunk in zip(reversed(laps), reversed(chunks)):
        r = size % 3
        d = cur[r:] + cur[:r]
        pre = d + chunk  # any sequence of the right type and length; fully overwritten
        pre[2::3] = chunk
        pre[0::3] = d[0::2]
        pre[1::3] = d[1::2]
        cur = pre
    return cur[-_START:] + cur[:-_START]


def _check_length(n: int) -> None:
    if n == 1:
        raise ValueError("1-byte buffers cannot be transformed")


def _to_bits(data: bytes) -> bytearray:
    return bytearray(format(int.from_bytes(data, "big"), f"0{8 * len(data)}b"), "ascii")


def _from_bits(bits: bytearray, n: int) -> bytes:
    return int(bits, 2).to_bytes(n, "big")


def encode(data: bytes) -> bytes:
    """Obfuscate ``data`` (bytes-like, length 0 or >= 2)."""
    n = len(data)
    if n == 0:
        return b""
    _check_length(n)
    return _from_bits(_deal(_to_bits(data)), n)


def decode(data: bytes) -> bytes:
    """Exact inverse of :func:`encode`."""
    n = len(data)
    if n == 0:
        return b""
    _check_length(n)
    return _from_bits(_undeal(_to_bits(data)), n)


def _table_index(p: int) -> int:
    """MSB-first stream position <-> LSB-first table index (self-inverse)."""
    return (p & ~7) | (7 - (p & 7))


@lru_cache(maxsize=64)
def permutation(n: int) -> tuple[int, ...]:
    """``perm[i]`` = output bit index of input bit i (bit index = byte*8 + bit,
    bit 0 = LSB), i.e. the same convention as the recorded tables."""
    if n < 2:
        raise ValueError("permutation is defined for n >= 2")
    src = _deal(list(range(8 * n)))
    perm = [0] * (8 * n)
    for j, p in enumerate(src):
        perm[_table_index(p)] = _table_index(j)
    return tuple(perm)
