"""Shared test fixtures for aiobt."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiobt.bencode import encode


@pytest.fixture
def tmp_torrent(tmp_path: Path) -> Path:
    """Create a minimal single-file .torrent in *tmp_path*."""
    content = b"Hello, BitTorrent!" * 100  # 1800 bytes
    piece_length = 512
    pieces = b""

    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + piece_length]
        pieces += hashlib.sha1(chunk).digest()
        offset += piece_length

    info: dict[bytes, object] = {
        b"name": b"hello.txt",
        b"piece length": piece_length,
        b"pieces": pieces,
        b"length": len(content),
    }

    torrent: dict[bytes, object] = {
        b"announce": b"http://tracker.example.com/announce",
        b"info": info,
        b"comment": b"test torrent",
        b"created by": b"aiobt-tests",
        b"creation date": 1717100000,
    }

    torrent_path = tmp_path / "test.torrent"
    torrent_path.write_bytes(encode(torrent))  # type: ignore[arg-type]
    return torrent_path


@pytest.fixture
def tmp_multi_torrent(tmp_path: Path) -> Path:
    """Create a minimal multi-file .torrent in *tmp_path*."""
    file1 = b"File one content " * 50   # 850 bytes
    file2 = b"Second file data " * 100  # 1700 bytes
    content = file1 + file2

    piece_length = 1024
    pieces = b""

    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + piece_length]
        pieces += hashlib.sha1(chunk).digest()
        offset += piece_length

    info: dict[bytes, object] = {
        b"name": b"test_dir",
        b"piece length": piece_length,
        b"pieces": pieces,
        b"files": [
            {b"length": len(file1), b"path": [b"subdir", b"file1.txt"]},
            {b"length": len(file2), b"path": [b"file2.txt"]},
        ],
    }

    torrent: dict[bytes, object] = {
        b"announce": b"http://tracker.example.com/announce",
        b"announce-list": [
            [b"http://tracker1.example.com/announce"],
            [
                b"http://tracker2.example.com/announce",
                b"http://tracker3.example.com/announce",
            ],
        ],
        b"info": info,
    }

    torrent_path = tmp_path / "multi.torrent"
    torrent_path.write_bytes(encode(torrent))  # type: ignore[arg-type]
    return torrent_path
