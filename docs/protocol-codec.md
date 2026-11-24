# Port-9000 "mode 2" obfuscation: specification

The DVR's client protocol (TCP 9000) obfuscates message headers (bytes 2-15) and payloads with
the permutation below. It was derived from the protocol's observed behaviour: the input-to-output
bit mapping recorded for lengths 2-512 and a range of larger sizes. `dvrbridge/dvr9000/codec.py`
implements it.

## 1. Summary

`encode` is a keyless bit permutation of an `n`-byte buffer (`n >= 2`).
Read the buffer as a single bit string, most significant bit of each byte
first. Arrange those `N = 8n` bits in a circle and repeatedly remove every
third bit (the Josephus elimination with step 3), starting the count at
bit position 2. The order in which bits are removed is the encoded bit string,
also written back MSB first. `decode` is the exact inverse.

There are no special cases by `n mod 3` or anything else. The period-24
regularity visible in the bit mapping comes from the first lap removing every third
bit. `n = 1` would be well defined mathematically, but the protocol never
transforms 1-byte buffers, so implementations reject it (section 6).

## 2. Bit numbering

Two numberings are used:

* **Table index** (the one `codec.permutation()` returns): `t = 8*byte + bit`, where
  `bit 0` is the least significant bit of that byte.
* **Stream position** (the one used in this spec): `p = 8*byte + (7 - bit)`,
  so position 0 is the MSB of byte 0, position 7 is the LSB of byte 0,
  position 8 is the MSB of byte 1, and so on. Both inputs and outputs are
  numbered this way. It is the bit order you get from
  `int.from_bytes(data, "big")` printed in binary with leading zeros.

The conversion is its own inverse: `p = 8*(t // 8) + 7 - t % 8`.

## 3. The permutation (elimination form)

Let `N = 8n`. Let `src[j]` mean "output stream position `j` takes the bit
from input stream position `src[j]`". It is computed as follows:

1. Place the positions `0, 1, ..., N-1` in a circle in that order.
2. Put a pointer on position `2`.
3. Repeat until the circle is empty:
   * Count `1` on the element under the pointer, `2` on the next surviving
     element clockwise, and `3` on the one after that (wrapping around the
     circle as needed).
   * Remove the element that got `3` and append it to `src`.
   * Move the pointer to the element that followed the removed one.

Equivalently, with 0-based list indexing, where `circle` is a list and
`i` is the pointer index:

```
circle = [0, 1, ..., N-1];  i = 2
while circle:
    i = (i + 2) mod len(circle)
    src.append(circle.pop(i))
    # i now already points at the element after the removed one
    # (take i mod len(circle) if the circle is still non-empty)
```

Encoding: `out_bit[j] = in_bit[src[j]]` for `j = 0..N-1` (stream positions).
Decoding: `out_bit[src[j]] = in_bit[j]`.

In the table-index convention (`codec.permutation(n)`), `perm[t]` is the output table index of input
table index `t`. If `j` is the output position with `src[j] = p`, then
`perm[tableindex(p)] = tableindex(j)`.

### Worked example, n = 2 (N = 16)

The first lap removes 4, 7, 10, 13, then wraps to 0 (counting 14, 15, 0).
The full elimination order is:

```
src = 4 7 10 13 0 3 8 12 1 6 14 5 15 11 2 9
```

So output stream bit 0 is input stream bit 4 (the MSB-first bit 4 of byte 0,
which is table bit 3). In table form that gives `perm[3] = 7`, and the
recorded table indeed has `perms["2"][3] == 7`.

## 4. Equivalent lap-by-lap form (for efficient implementation)

Popping elements one at a time costs O(N^2). The same order can be produced
one lap of the circle at a time using only slicing, in O(N) total, because
each lap removes a third of what remains.

Rotate first so that the counting start is at index 0:
`seq = positions[2:] + positions[:2]` (i.e. `2, 3, ..., N-1, 0, 1`).
Then, while `L = len(seq) >= 3`:

1. `m = L // 3`, `r = L % 3`.
2. The removed elements of this lap are `seq[2], seq[5], ..., seq[3m-1]`,
   i.e. `seq[2::3]`, which holds exactly `m` elements. Append them to `src`
   in that order.
3. Delete them from `seq`. That leaves `2m + r` elements: the `2m` survivors
   from indices `< 3m`, then the `r` tail elements that the lap never reached.
4. The next count starts at the element right after the last removed one,
   which is the first tail element, or index 0 when `r = 0`. So rotate the
   tail to the front: `seq = seq[2m:] + seq[:2m]`.

When `L` is 1 or 2, counting to 3 from index 0 lands on index 0 again
(`2 mod 1 = 2 mod 2 = 0`), and the element after it (if any) is removed next.
So the remaining `seq` is appended to `src` as it is, and the process ends.

The lap lengths depend only on `N`: `L_0 = N`, `L_{k+1} = L_k - floor(L_k / 3)`
while `L_k >= 3`. That is roughly `log_{3/2}(N)` laps (about 40 laps for a
1 MB buffer).

### Inverting a lap (decode)

Split the encoded stream into the per-lap chunks (sizes `floor(L_k/3)` in
order) and the final tail of fewer than 3 elements. Start with `seq = tail`
and undo the laps in reverse order. For a lap with length `L`, `m = L // 3`,
`r = L % 3`, and removed chunk `c`:

1. Undo the rotation: `d = seq[r:] + seq[:r]` (`len(d) = 2m + r`).
2. Rebuild the pre-lap list `pre` of length `L`: `pre[2::3] = c`,
   `pre[0::3] = d[0::2]`, `pre[1::3] = d[1::2]`. The slice lengths always agree.
3. Set `seq = pre`.

Finally undo the initial rotation: `bits = seq[N-2:] + seq[:N-2]`.

## 5. Applying it to bytes

* Encode: `s = MSB-first bit string of data` (length `8n`), run the lap form
  on the characters of `s`, read the result back as an `8n`-bit big-endian
  integer and emit `n` bytes.
* Decode: the same with the inverse lap form.

The lap form only uses slicing, so the same code can run on a string of bits
or on a list of integer positions. Running it on `list(range(N))` gives `src`,
which is what `codec.permutation(n)` reports.

## 6. Interface contract

* `encode(b"") == decode(b"") == b""`.
* Length 1 raises `ValueError`. The protocol never uses it.
* Any `n >= 2`: output length equals input length,
  `decode(encode(x)) == x`, and `encode(decode(x)) == x`.
