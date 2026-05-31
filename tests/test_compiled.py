"""Tests for aiobt._compiled — runtime Cython introspection."""

from __future__ import annotations

import later.unittest

from aiobt._compiled import (
    CYTHON_MODULES,
    compilation_status,
    is_compiled,
)


class TestIsCompiled(later.unittest.TestCase):
    def test_returns_bool(self) -> None:
        result = is_compiled("bencode")
        self.assertIsInstance(result, bool)

    def test_nonexistent_module(self) -> None:
        self.assertFalse(is_compiled("does_not_exist_xyz"))

    def test_default_argument(self) -> None:
        # Default should be "bencode"
        result = is_compiled()
        self.assertIsInstance(result, bool)


class TestCythonModules(later.unittest.TestCase):
    def test_is_tuple(self) -> None:
        self.assertIsInstance(CYTHON_MODULES, tuple)

    def test_bencode_listed(self) -> None:
        self.assertIn("bencode", CYTHON_MODULES)


class TestCompilationStatus(later.unittest.TestCase):
    def test_returns_dict(self) -> None:
        status = compilation_status()
        self.assertIsInstance(status, dict)

    def test_all_modules_present(self) -> None:
        status = compilation_status()
        for mod in CYTHON_MODULES:
            self.assertIn(mod, status)
            self.assertIsInstance(status[mod], bool)

    def test_values_are_bool(self) -> None:
        for value in compilation_status().values():
            self.assertIsInstance(value, bool)
