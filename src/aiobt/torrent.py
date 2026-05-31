"""Torrent metadata parsing and frozen data models.

All models are immutable ``attrs.frozen`` classes.  Torrent files are
parsed from raw bytes (the on-disk ``.torrent`` format) into a
:class:`TorrentMeta` instance via :func:`parse_torrent_file` or
:func:`parse_torrent_bytes`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import PurePosixPath

import attrs

from .bencode import BencodeValue, DecodeError, decode, encode

# ---------------------------------------------------------------------------
# Type aliases (PEP 695)
# ---------------------------------------------------------------------------

type InfoHash = bytes
"""20-byte SHA-1 digest of the bencoded ``info`` dictionary."""

type PieceHash = bytes
"""20-byte SHA-1 digest of a single piece."""

# SHA-1 digest length in bytes
_SHA1_LEN = 20


# ---------------------------------------------------------------------------
# Frozen data models
# ---------------------------------------------------------------------------


@attrs.frozen
class FileEntry:
    """A single file within a multi-file torrent."""

    path: tuple[str, ...]
    """Path components relative to the torrent root directory."""

    length: int
    """File size in bytes."""

    @property
    def relative_path(self) -> PurePosixPath:
        """Return the path as a :class:`~pathlib.PurePosixPath`."""
        return PurePosixPath(*self.path)


@attrs.frozen
class TorrentInfo:
    """The ``info`` dictionary of a torrent — immutable."""

    name: str
    """Suggested name for the file or root directory."""

    piece_length: int
    """Number of bytes per piece (except possibly the last one)."""

    pieces_raw: bytes
    """Concatenated 20-byte SHA-1 hashes for every piece."""

    length: int | None = None
    """Total size in bytes (single-file mode only)."""

    files: tuple[FileEntry, ...] | None = None
    """File list (multi-file mode only)."""

    private: bool = False
    """Whether the torrent is flagged as private (BEP 27)."""

    @property
    def is_single_file(self) -> bool:
        return self.length is not None

    @property
    def piece_count(self) -> int:
        count, remainder = divmod(len(self.pieces_raw), _SHA1_LEN)
        if remainder:
            raise ValueError(
                f"pieces blob length {len(self.pieces_raw)} "
                f"is not a multiple of {_SHA1_LEN}"
            )
        return count

    def piece_hash(self, index: int) -> PieceHash:
        """Return the expected SHA-1 hash for piece *index*."""
        start = index * _SHA1_LEN
        end = start + _SHA1_LEN
        if start < 0 or end > len(self.pieces_raw):
            raise IndexError(f"piece index {index} out of range")
        return self.pieces_raw[start:end]

    @property
    def total_length(self) -> int:
        """Total size of all content in bytes."""
        if self.length is not None:
            return self.length
        if self.files is not None:
            return sum(f.length for f in self.files)
        raise ValueError("torrent has neither length nor files")


@attrs.frozen
class TorrentMeta:
    """Complete parsed torrent metadata — immutable."""

    info: TorrentInfo
    """The ``info`` dictionary."""

    info_hash: InfoHash
    """20-byte SHA-1 of the bencoded ``info`` dict."""

    announce: str | None = None
    """Primary tracker URL."""

    announce_list: tuple[tuple[str, ...], ...] | None = None
    """BEP 12 announce-list (list of tracker tiers)."""

    creation_date: int | None = None
    """Unix timestamp when the torrent was created."""

    comment: str | None = None
    """Free-form comment."""

    created_by: str | None = None
    """Name/version of the program that created the torrent."""

    @property
    def total_length(self) -> int:
        return self.info.total_length

    @property
    def piece_count(self) -> int:
        return self.info.piece_count

    def tracker_urls(self) -> list[str]:
        """Return a flat, deduplicated list of all tracker URLs."""
        seen: set[str] = set()
        urls: list[str] = []
        if self.announce and self.announce not in seen:
            urls.append(self.announce)
            seen.add(self.announce)
        if self.announce_list:
            for tier in self.announce_list:
                for url in tier:
                    if url not in seen:
                        urls.append(url)
                        seen.add(url)
        return urls


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _bytes_or_none(d: dict[bytes, BencodeValue], key: bytes) -> bytes | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, bytes):
        raise DecodeError(f"expected bytes for key {key!r}, got {type(v).__name__}")
    return v


def _str_or_none(d: dict[bytes, BencodeValue], key: bytes) -> str | None:
    raw = _bytes_or_none(d, key)
    return raw.decode("utf-8", errors="surrogateescape") if raw is not None else None


def _int_or_none(d: dict[bytes, BencodeValue], key: bytes) -> int | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, int):
        raise DecodeError(f"expected int for key {key!r}, got {type(v).__name__}")
    return v


def _require_bytes(d: dict[bytes, BencodeValue], key: bytes) -> bytes:
    v = _bytes_or_none(d, key)
    if v is None:
        raise DecodeError(f"missing required key {key!r}")
    return v


def _require_int(d: dict[bytes, BencodeValue], key: bytes) -> int:
    v = _int_or_none(d, key)
    if v is None:
        raise DecodeError(f"missing required key {key!r}")
    return v


def _require_dict(
    d: dict[bytes, BencodeValue], key: bytes
) -> dict[bytes, BencodeValue]:
    v = d.get(key)
    if v is None:
        raise DecodeError(f"missing required key {key!r}")
    if not isinstance(v, dict):
        raise DecodeError(f"expected dict for key {key!r}, got {type(v).__name__}")
    return v


def _parse_files(raw_files: list[BencodeValue]) -> tuple[FileEntry, ...]:
    """Parse the ``files`` list from a multi-file info dict."""
    entries: list[FileEntry] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise DecodeError(f"file entry must be a dict, got {type(item).__name__}")
        length = _require_int(item, b"length")
        raw_path = item.get(b"path")
        if not isinstance(raw_path, list):
            raise DecodeError("file entry missing 'path' list")
        path_parts: list[str] = []
        for part in raw_path:
            if not isinstance(part, bytes):
                raise DecodeError(
                    f"path component must be bytes, got {type(part).__name__}"
                )
            path_parts.append(part.decode("utf-8", errors="surrogateescape"))
        entries.append(FileEntry(path=tuple(path_parts), length=length))
    return tuple(entries)


def _parse_announce_list(
    raw: list[BencodeValue],
) -> tuple[tuple[str, ...], ...]:
    """Parse BEP 12 announce-list."""
    tiers: list[tuple[str, ...]] = []
    for tier_item in raw:
        if not isinstance(tier_item, list):
            raise DecodeError("announce-list tier must be a list")
        urls: list[str] = []
        for url in tier_item:
            if not isinstance(url, bytes):
                raise DecodeError("tracker URL must be bytes")
            urls.append(url.decode("utf-8", errors="surrogateescape"))
        tiers.append(tuple(urls))
    return tuple(tiers)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_torrent_bytes(data: bytes) -> TorrentMeta:
    """Parse a ``.torrent`` file from raw *data* bytes."""
    top = decode(data)
    if not isinstance(top, dict):
        raise DecodeError(f"torrent must be a dict, got {type(top).__name__}")

    info_dict = _require_dict(top, b"info")
    info_hash: InfoHash = hashlib.sha1(encode(info_dict)).digest()

    # --- info fields ---
    name_raw = _require_bytes(info_dict, b"name")
    name = name_raw.decode("utf-8", errors="surrogateescape")
    piece_length = _require_int(info_dict, b"piece length")
    pieces_raw = _require_bytes(info_dict, b"pieces")

    length = _int_or_none(info_dict, b"length")
    files: tuple[FileEntry, ...] | None = None

    raw_files = info_dict.get(b"files")
    if raw_files is not None:
        if not isinstance(raw_files, list):
            raise DecodeError("'files' must be a list")
        files = _parse_files(raw_files)

    if length is None and files is None:
        raise DecodeError("info dict has neither 'length' nor 'files'")

    private_val = _int_or_none(info_dict, b"private")
    private = private_val == 1

    info = TorrentInfo(
        name=name,
        piece_length=piece_length,
        pieces_raw=pieces_raw,
        length=length,
        files=files,
        private=private,
    )

    # --- top-level fields ---
    announce = _str_or_none(top, b"announce")

    announce_list: tuple[tuple[str, ...], ...] | None = None
    raw_al = top.get(b"announce-list")
    if isinstance(raw_al, list):
        announce_list = _parse_announce_list(raw_al)

    return TorrentMeta(
        info=info,
        info_hash=info_hash,
        announce=announce,
        announce_list=announce_list,
        creation_date=_int_or_none(top, b"creation date"),
        comment=_str_or_none(top, b"comment"),
        created_by=_str_or_none(top, b"created by"),
    )


def parse_torrent_file(path: str) -> TorrentMeta:
    """Read and parse a ``.torrent`` file from *path*."""
    from pathlib import Path

    return parse_torrent_bytes(Path(path).read_bytes())
