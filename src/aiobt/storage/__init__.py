"""Pluggable storage backend protocol and public exports."""

from .base import StorageBackend
from .compact import CompactStorage
from .disk import DiskStorage
from .queue import FileQueue

__all__ = [
    "CompactStorage",
    "DiskStorage",
    "FileQueue",
    "StorageBackend",
]
