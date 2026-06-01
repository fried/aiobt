"""Runtime introspection for compiled Cython extensions.

Check whether aiobt is using compiled Cython extensions or falling
back to pure Python::

    >>> from aiobt._compiled import is_compiled, compilation_status
    >>> is_compiled("bencode")
    False
    >>> compilation_status()
    {'bencode': False}
"""

from __future__ import annotations

import importlib

# Every module that supports optional Cython compilation.
CYTHON_MODULES: tuple[str, ...] = ("bencode", "piece", "protocol")


def is_compiled(module_name: str = "bencode") -> bool:
    """Return ``True`` if *module_name* is loaded from a compiled extension.

    Parameters
    ----------
    module_name:
        Bare module name within the ``aiobt`` package (e.g. ``"bencode"``).
    """
    try:
        mod = importlib.import_module(f"aiobt.{module_name}")
    except ImportError:
        return False
    path = getattr(mod, "__file__", None)
    if path is None:
        return False
    return not path.endswith(".py")


def compilation_status() -> dict[str, bool]:
    """Return a mapping of ``{module_name: is_compiled}`` for every
    Cython-eligible module."""
    return {name: is_compiled(name) for name in CYTHON_MODULES}
