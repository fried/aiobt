#!/usr/bin/env python3
"""Benchmark: Pure Python vs Cython for aiobt hot paths.

Run twice to compare:
    PYTHONPATH=src python3 bench/bench_cython.py    # pure Python
    python3 bench/bench_cython.py                    # compiled (pip install -e .)

Each test runs the operation N times and reports ops/sec + time per op.
"""

from __future__ import annotations

import hashlib
import os
import struct
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

from aiobt._compiled import compilation_status
from aiobt.bencode import decode, encode
from aiobt.piece import PieceSpec, PieceTracker
from aiobt.protocol import (
    Bitfield,
    Cancel,
    Choke,
    Handshake,
    Have,
    Interested,
    KeepAlive,
    NotInterested,
    Piece,
    Request,
    Unchoke,
    parse_message,
)


@dataclass
class Result:
    name: str
    ops: int
    elapsed: float

    @property
    def ops_per_sec(self) -> float:
        return self.ops / self.elapsed if self.elapsed > 0 else float("inf")

    @property
    def ns_per_op(self) -> float:
        return (self.elapsed / self.ops) * 1e9 if self.ops > 0 else 0


def bench(name: str, fn, n: int = 100_000) -> Result:
    """Time *fn* over *n* iterations. fn() is called with no args."""
    # Warmup
    for _ in range(min(1000, n // 10)):
        fn()

    start = time.perf_counter()
    for _ in range(n):
        fn()
    elapsed = time.perf_counter() - start
    return Result(name=name, ops=n, elapsed=elapsed)


# ---------------------------------------------------------------------------
# Bencode benchmarks
# ---------------------------------------------------------------------------

# Realistic torrent-shaped dict
_BENCODE_DICT = {
    b"announce": b"udp://tracker.example.com:6969/announce",
    b"comment": b"Test torrent for benchmarking purposes",
    b"created by": b"aiobt/0.1.0",
    b"creation date": 1717200000,
    b"info": {
        b"length": 1048576,
        b"name": b"benchmark-test-file.bin",
        b"piece length": 262144,
        b"pieces": os.urandom(80),  # 4 pieces worth of SHA-1
    },
}
_BENCODE_ENCODED = encode(_BENCODE_DICT)


def _bench_bencode_encode():
    encode(_BENCODE_DICT)


def _bench_bencode_decode():
    decode(_BENCODE_ENCODED)


# Larger dict — simulate a multi-file torrent with many pieces
_LARGE_PIECES = os.urandom(20 * 1500)  # 1500 pieces
_LARGE_DICT = {
    b"announce": b"udp://tracker.example.com:6969/announce",
    b"info": {
        b"files": [
            {b"length": 10485760, b"path": [f"file_{i:04d}.dat".encode()]}
            for i in range(50)
        ],
        b"name": b"large-torrent",
        b"piece length": 524288,
        b"pieces": _LARGE_PIECES,
    },
}
_LARGE_ENCODED = encode(_LARGE_DICT)


def _bench_bencode_encode_large():
    encode(_LARGE_DICT)


def _bench_bencode_decode_large():
    decode(_LARGE_ENCODED)


# ---------------------------------------------------------------------------
# Protocol benchmarks
# ---------------------------------------------------------------------------

# Pre-build message bytes for parsing
_KEEPALIVE_DATA = b""
_CHOKE_DATA = struct.pack("B", 0)
_UNCHOKE_DATA = struct.pack("B", 1)
_INTERESTED_DATA = struct.pack("B", 2)
_HAVE_DATA = struct.pack("!BI", 4, 42)
_REQUEST_DATA = struct.pack("!BIII", 6, 100, 0, 16384)
_CANCEL_DATA = struct.pack("!BIII", 8, 100, 0, 16384)

# Piece message — 16 KiB block (the most common message during transfer)
_BLOCK = os.urandom(16384)
_PIECE_DATA = struct.pack("!BII", 7, 42, 0) + _BLOCK

# Bitfield — 1500 pieces = 188 bytes
_BITFIELD_RAW = os.urandom(188)
_BITFIELD_DATA = bytes([5]) + _BITFIELD_RAW
_BITFIELD_OBJ = Bitfield(data=_BITFIELD_RAW)

# Handshake
_HANDSHAKE_OBJ = Handshake(
    info_hash=os.urandom(20),
    peer_id=os.urandom(20),
)
_HANDSHAKE_BYTES = _HANDSHAKE_OBJ.to_bytes()

# Message objects for to_bytes benchmarks
_HAVE_OBJ = Have(index=42)
_REQUEST_OBJ = Request(index=100, begin=0, length=16384)
_PIECE_OBJ = Piece(index=42, begin=0, block=_BLOCK)


def _bench_parse_keepalive():
    parse_message(_KEEPALIVE_DATA)


def _bench_parse_choke():
    parse_message(_CHOKE_DATA)


def _bench_parse_have():
    parse_message(_HAVE_DATA)


def _bench_parse_request():
    parse_message(_REQUEST_DATA)


def _bench_parse_piece():
    parse_message(_PIECE_DATA)


def _bench_parse_bitfield():
    parse_message(_BITFIELD_DATA)


def _bench_handshake_to_bytes():
    _HANDSHAKE_OBJ.to_bytes()


def _bench_handshake_from_bytes():
    Handshake.from_bytes(_HANDSHAKE_BYTES)


def _bench_keepalive_to_bytes():
    KeepAlive().to_bytes()


def _bench_choke_to_bytes():
    Choke().to_bytes()


def _bench_have_to_bytes():
    _HAVE_OBJ.to_bytes()


def _bench_request_to_bytes():
    _REQUEST_OBJ.to_bytes()


def _bench_piece_to_bytes():
    _PIECE_OBJ.to_bytes()


def _bench_bitfield_has_piece():
    # Check 100 piece indices
    bf = _BITFIELD_OBJ
    for i in range(100):
        bf.has_piece(i * 15)  # spread across the bitfield


# ---------------------------------------------------------------------------
# Piece tracker benchmarks
# ---------------------------------------------------------------------------

# Build a realistic tracker: 1500 pieces, 512 KiB each, ~768 MB torrent
_PT_PIECE_LENGTH = 524288
_PT_TOTAL_LENGTH = _PT_PIECE_LENGTH * 1500
_PT_PIECES_RAW = os.urandom(20 * 1500)


def _bench_tracker_init():
    PieceTracker(
        piece_length=_PT_PIECE_LENGTH,
        total_length=_PT_TOTAL_LENGTH,
        pieces_raw=_PT_PIECES_RAW,
    )


# Tracker with some pieces downloaded and availability data
_PT = PieceTracker(
    piece_length=_PT_PIECE_LENGTH,
    total_length=_PT_TOTAL_LENGTH,
    pieces_raw=_PT_PIECES_RAW,
)
# Simulate 500 pieces already downloaded
for _i in range(500):
    _PT.mark_have(_i)
# Simulate 50 pending
for _i in range(500, 550):
    _PT.mark_pending(_i)
# Simulate availability from 20 peers
for _peer in range(20):
    _peer_pieces = set(range(_peer * 50, min((_peer + 1) * 75, 1500)))
    _PT.update_availability(_peer_pieces)


def _bench_select_piece():
    _PT.select_piece()


# Fresh tracker for availability update benchmarks
_peer_set_small = set(range(0, 200))
_peer_set_large = set(range(0, 1000))


def _bench_update_availability_small():
    pt = PieceTracker(
        piece_length=_PT_PIECE_LENGTH,
        total_length=_PT_TOTAL_LENGTH,
        pieces_raw=_PT_PIECES_RAW,
    )
    pt.update_availability(_peer_set_small)


def _bench_update_availability_large():
    pt = PieceTracker(
        piece_length=_PT_PIECE_LENGTH,
        total_length=_PT_TOTAL_LENGTH,
        pieces_raw=_PT_PIECES_RAW,
    )
    pt.update_availability(_peer_set_large)


# Verify piece — dominated by SHA-1 (C) but tests the wrapper overhead
_VERIFY_DATA = os.urandom(524288)
_VERIFY_HASH = hashlib.sha1(_VERIFY_DATA).digest()


def _bench_verify_piece():
    PieceTracker.verify_piece(_VERIFY_DATA, _VERIFY_HASH)


def _bench_mark_have():
    pt = PieceTracker(
        piece_length=_PT_PIECE_LENGTH,
        total_length=_PT_TOTAL_LENGTH,
        pieces_raw=_PT_PIECES_RAW,
    )
    for i in range(1500):
        pt.mark_have(i)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main():
    status = compilation_status()
    mode = "CYTHON" if any(status.values()) else "PURE PYTHON"

    print(f"\n{'=' * 72}")
    print(f"  aiobt benchmark — {mode}")
    print(f"{'=' * 72}")
    print(f"  Module status: {status}")
    print(f"{'=' * 72}\n")

    groups: list[tuple[str, list[tuple[str, object, int]]]] = [
        (
            "Bencode",
            [
                ("encode (small dict)", _bench_bencode_encode, 200_000),
                ("decode (small dict)", _bench_bencode_decode, 200_000),
                ("encode (large/1500 pieces)", _bench_bencode_encode_large, 20_000),
                ("decode (large/1500 pieces)", _bench_bencode_decode_large, 20_000),
            ],
        ),
        (
            "Protocol — parse_message",
            [
                ("parse keepalive", _bench_parse_keepalive, 500_000),
                ("parse choke", _bench_parse_choke, 500_000),
                ("parse have", _bench_parse_have, 500_000),
                ("parse request", _bench_parse_request, 500_000),
                ("parse piece (16 KiB)", _bench_parse_piece, 500_000),
                ("parse bitfield (1500 pcs)", _bench_parse_bitfield, 500_000),
            ],
        ),
        (
            "Protocol — to_bytes",
            [
                ("keepalive.to_bytes", _bench_keepalive_to_bytes, 500_000),
                ("choke.to_bytes", _bench_choke_to_bytes, 500_000),
                ("have.to_bytes", _bench_have_to_bytes, 500_000),
                ("request.to_bytes", _bench_request_to_bytes, 500_000),
                ("piece.to_bytes (16 KiB)", _bench_piece_to_bytes, 500_000),
            ],
        ),
        (
            "Protocol — handshake",
            [
                ("handshake.to_bytes", _bench_handshake_to_bytes, 500_000),
                ("handshake.from_bytes", _bench_handshake_from_bytes, 500_000),
            ],
        ),
        (
            "Protocol — bitfield",
            [
                ("has_piece × 100", _bench_bitfield_has_piece, 50_000),
            ],
        ),
        (
            "Piece tracker",
            [
                ("PieceTracker() init (1500 pcs)", _bench_tracker_init, 5_000),
                ("select_piece (500 have, 50 pending)", _bench_select_piece, 50_000),
                (
                    "update_availability (200 pcs)",
                    _bench_update_availability_small,
                    5_000,
                ),
                (
                    "update_availability (1000 pcs)",
                    _bench_update_availability_large,
                    2_000,
                ),
                ("verify_piece (512 KiB)", _bench_verify_piece, 20_000),
                ("mark_have × 1500", _bench_mark_have, 2_000),
            ],
        ),
    ]

    all_results: list[Result] = []

    for group_name, tests in groups:
        print(f"  {group_name}")
        print(f"  {'-' * 50}")
        for name, fn, n in tests:
            r = bench(name, fn, n)
            all_results.append(r)
            print(
                f"    {name:40s}  {r.ops_per_sec:>12,.0f} ops/s  ({r.ns_per_op:>8,.0f} ns/op)"
            )
        print()

    print(f"{'=' * 72}")
    print(f"  Total operations: {sum(r.ops for r in all_results):,}")
    print(f"  Total time:       {sum(r.elapsed for r in all_results):.2f}s")
    print(f"  Mode:             {mode}")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
