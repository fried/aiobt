"""setup.py — Cython extension compilation for aiobt.

setuptools calls this automatically during ``python -m build``.
When Cython is available, bencode (and future eligible modules) are
compiled as C extensions.  The pure-Python .py sources are always
included in the wheel alongside any .so/.pyd, so import falls back
gracefully on platforms without a compiled extension.
"""

from __future__ import annotations

import os
from pathlib import Path

from setuptools import setup

# Attempt Cython compilation; fall back to pure-Python wheel if unavailable.
ext_modules = []

try:
    from Cython.Build import cythonize

    # Only compile if the source exists (sanity check for sdist).
    bencode_src = Path("src/aiobt/bencode.py")
    if bencode_src.exists():
        try:
            ext_modules = cythonize(
                [str(bencode_src)],
                language_level="3str",
                compiler_directives={
                    "boundscheck": False,
                    "wraparound": False,
                    "cdivision": True,
                },
            )
            # Fix the module name: cythonize uses the file path, but
            # setuptools needs the dotted package name.
            for ext in ext_modules:
                ext.name = ext.name.replace("src.", "")
        except Exception:
            # Cython compilation failed (e.g. unsupported syntax) —
            # fall back to pure-Python wheel.
            ext_modules = []
except ImportError:
    pass

setup(ext_modules=ext_modules)
