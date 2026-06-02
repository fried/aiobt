"""Torrent file creation — build .torrent metadata from files on disk.

Provides :func:`create_torrent` which scans one or more paths, hashes
their content into pieces, and returns a :class:`TorrentMeta` that can
be serialized with :meth:`TorrentMeta.to_bytes` or :meth:`TorrentMeta.write`.

Piece-size selection follows the widely-used heuristic of targeting
~1 500–2 000 pieces, clamped to powers of two between 16 KiB and 16 MiB.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from pathlib import Path

from dataclasses import dataclass

from .bencode import BencodeValue, encode
from .torrent import FileEntry, TorrentInfo, TorrentMeta

# ---------------------------------------------------------------------------
# Piece-size auto-selection
# ---------------------------------------------------------------------------

# Boundaries (bytes)
_MIN_PIECE_SIZE = 16 * 1024  # 16 KiB
_MAX_PIECE_SIZE = 16 * 1024 * 1024  # 16 MiB
_TARGET_PIECES = 1500  # aim for this many pieces


def optimal_piece_size(total_bytes: int) -> int:
    """Pick the best power-of-two piece size for *total_bytes*.

    Strategy: target ~1 500 pieces (balances overhead vs granularity),
    round up to the next power of two, then clamp between 16 KiB and
    16 MiB.  The result is always a power of two.

    >>> optimal_piece_size(700 * 1024 * 1024)  # 700 MB  -> 512 KiB
    524288
    >>> optimal_piece_size(4_000_000_000)  # ~4 GB   -> 4 MiB
    4194304
    >>> optimal_piece_size(50_000_000_000)  # 50 GB   -> 16 MiB (capped)
    16777216
    >>> optimal_piece_size(1024)  # tiny    -> 16 KiB (floor)
    16384
    """
    if total_bytes <= 0:
        return _MIN_PIECE_SIZE

    raw = total_bytes / _TARGET_PIECES
    # Round up to next power of two
    power = 1 << max(0, math.ceil(math.log2(max(raw, 1))))
    return max(_MIN_PIECE_SIZE, min(power, _MAX_PIECE_SIZE))


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileSpec:
    """Internal: a file to include in the torrent."""

    disk_path: Path
    """Absolute path on disk."""

    torrent_path: tuple[str, ...]
    """Path components inside the torrent (relative to the root name)."""

    length: int
    """File size in bytes."""


def _scan_path(path: Path) -> tuple[str, list[_FileSpec]]:
    """Scan *path* and return ``(root_name, file_specs)``.

    If *path* is a file, the torrent is single-file.
    If *path* is a directory, all files underneath are included
    (sorted for deterministic ordering), and the directory name
    becomes the torrent root name.

    Hidden files (starting with ``"."``) and empty files are skipped.
    """
    path = path.resolve()

    if path.is_file():
        return path.name, [
            _FileSpec(
                disk_path=path,
                torrent_path=(path.name,),
                length=path.stat().st_size,
            )
        ]

    if not path.is_dir():
        raise FileNotFoundError(f"path does not exist: {path}")

    root_name = path.name
    specs: list[_FileSpec] = []

    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        # Skip hidden files
        if any(part.startswith(".") for part in child.relative_to(path).parts):
            continue
        size = child.stat().st_size
        if size == 0:
            continue
        rel = child.relative_to(path)
        specs.append(
            _FileSpec(
                disk_path=child,
                torrent_path=tuple(rel.parts),
                length=size,
            )
        )

    if not specs:
        raise ValueError(f"no files found under {path}")

    return root_name, specs


def _scan_paths(paths: list[Path]) -> tuple[str, list[_FileSpec]]:
    """Handle one-or-many input paths.

    * Single file → single-file torrent
    * Single directory → multi-file torrent rooted at directory name
    * Multiple paths → multi-file torrent; root name from common parent
      or first path's parent
    """
    if len(paths) == 1:
        return _scan_path(paths[0])

    # Multiple explicit paths — create a multi-file torrent
    all_specs: list[_FileSpec] = []
    for p in paths:
        p = p.resolve()
        if p.is_file():
            all_specs.append(
                _FileSpec(
                    disk_path=p,
                    torrent_path=(p.name,),
                    length=p.stat().st_size,
                )
            )
        elif p.is_dir():
            _, dir_specs = _scan_path(p)
            for spec in dir_specs:
                # Prefix with directory name
                all_specs.append(
                    _FileSpec(
                        disk_path=spec.disk_path,
                        torrent_path=(p.name, *spec.torrent_path),
                        length=spec.length,
                    )
                )
        else:
            raise FileNotFoundError(f"path does not exist: {p}")

    if not all_specs:
        raise ValueError("no files found in provided paths")

    # Root name: common parent or first path's parent
    try:
        common = Path(os.path.commonpath([p.resolve() for p in paths]))
        root_name = common.name or paths[0].resolve().parent.name
    except ValueError:
        root_name = paths[0].resolve().parent.name

    return root_name, all_specs


# ---------------------------------------------------------------------------
# Piece hashing
# ---------------------------------------------------------------------------


def _hash_pieces(
    specs: list[_FileSpec],
    piece_length: int,
) -> bytes:
    """Read files in order, hash each piece, return concatenated SHA-1s.

    Files are treated as one continuous byte stream per the BitTorrent
    spec — a piece may span multiple files.
    """
    pieces: list[bytes] = []
    hasher = hashlib.sha1()
    bytes_in_piece = 0

    for spec in specs:
        with open(spec.disk_path, "rb") as f:
            while True:
                remaining = piece_length - bytes_in_piece
                chunk = f.read(remaining)
                if not chunk:
                    break
                hasher.update(chunk)
                bytes_in_piece += len(chunk)
                if bytes_in_piece == piece_length:
                    pieces.append(hasher.digest())
                    hasher = hashlib.sha1()
                    bytes_in_piece = 0

    # Final partial piece
    if bytes_in_piece > 0:
        pieces.append(hasher.digest())

    return b"".join(pieces)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_torrent(
    path: str | Path | list[str | Path],
    *,
    trackers: list[str] | list[list[str]] | None = None,
    piece_length: int | None = None,
    comment: str | None = None,
    private: bool = False,
    created_by: str = "aiobt",
) -> TorrentMeta:
    """Create a :class:`TorrentMeta` from files on disk.

    Parameters
    ----------
    path:
        A single file, a single directory, or a list of files/directories.
        Directories are scanned recursively (hidden files skipped).
    trackers:
        Tracker URLs.  A flat list produces a single-tier announce-list.
        A nested list produces multi-tier (BEP 12).  The first URL is
        also set as the ``announce`` field.
    piece_length:
        Override the automatic piece size (must be a power of two).
        When *None*, :func:`optimal_piece_size` picks based on total size.
    comment:
        Optional free-form comment embedded in the torrent.
    private:
        Set the private flag (BEP 27) — disables DHT/PEX.
    created_by:
        Creator string (default ``"aiobt"``).

    Returns
    -------
    TorrentMeta
        Frozen dataclass with ``.write(path)`` and ``.to_bytes()`` methods.
    """
    # Normalize input paths
    if isinstance(path, (str, Path)):
        paths = [Path(path)]
    else:
        paths = [Path(p) for p in path]

    root_name, specs = _scan_paths(paths)
    total_size = sum(s.length for s in specs)

    # Piece size
    if piece_length is not None:
        if piece_length < _MIN_PIECE_SIZE:
            raise ValueError(
                f"piece_length {piece_length} below minimum {_MIN_PIECE_SIZE}"
            )
        if piece_length & (piece_length - 1) != 0:
            raise ValueError(f"piece_length must be a power of two, got {piece_length}")
    else:
        piece_length = optimal_piece_size(total_size)

    # Hash all pieces
    pieces_raw = _hash_pieces(specs, piece_length)

    # Build TorrentInfo
    is_single = len(specs) == 1 and len(specs[0].torrent_path) == 1
    files: tuple[FileEntry, ...] | None = None
    length: int | None = None

    if is_single:
        length = specs[0].length
    else:
        files = tuple(FileEntry(path=s.torrent_path, length=s.length) for s in specs)

    info = TorrentInfo(
        name=root_name,
        piece_length=piece_length,
        pieces_raw=pieces_raw,
        length=length,
        files=files,
        private=private,
    )

    # Compute info_hash
    info_dict = _info_to_bencode(info)
    info_hash = hashlib.sha1(encode(info_dict)).digest()

    # Tracker URLs
    announce: str | None = None
    announce_list: tuple[tuple[str, ...], ...] | None = None

    if trackers:
        if isinstance(trackers[0], str):
            # Flat list → single tier
            flat: list[str] = trackers  # type: ignore[assignment]
            announce = flat[0]
            announce_list = (tuple(flat),)
        else:
            # Nested list → multi-tier
            tiers: list[list[str]] = trackers  # type: ignore[assignment]
            announce_list = tuple(tuple(tier) for tier in tiers)
            # First URL from first tier
            for tier in tiers:
                if tier:
                    announce = tier[0]
                    break

    return TorrentMeta(
        info=info,
        info_hash=info_hash,
        announce=announce,
        announce_list=announce_list,
        creation_date=int(time.time()),
        comment=comment,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _info_to_bencode(info: TorrentInfo) -> dict[bytes, BencodeValue]:
    """Convert a TorrentInfo to a bencode-ready dict."""
    d: dict[bytes, BencodeValue] = {
        b"name": info.name.encode("utf-8"),
        b"piece length": info.piece_length,
        b"pieces": info.pieces_raw,
    }

    if info.length is not None:
        d[b"length"] = info.length

    if info.files is not None:
        file_list: list[BencodeValue] = []
        for f in info.files:
            file_list.append(
                {
                    b"length": f.length,
                    b"path": [part.encode("utf-8") for part in f.path],
                }
            )
        d[b"files"] = file_list

    if info.private:
        d[b"private"] = 1

    return d


def torrent_to_bytes(meta: TorrentMeta) -> bytes:
    """Serialize a TorrentMeta to bencoded .torrent bytes."""
    top: dict[bytes, BencodeValue] = {
        b"info": _info_to_bencode(meta.info),
    }

    if meta.announce is not None:
        top[b"announce"] = meta.announce.encode("utf-8")

    if meta.announce_list is not None:
        al: list[BencodeValue] = []
        for tier in meta.announce_list:
            al.append([url.encode("utf-8") for url in tier])
        top[b"announce-list"] = al

    if meta.creation_date is not None:
        top[b"creation date"] = meta.creation_date

    if meta.comment is not None:
        top[b"comment"] = meta.comment.encode("utf-8")

    if meta.created_by is not None:
        top[b"created by"] = meta.created_by.encode("utf-8")

    return encode(top)
