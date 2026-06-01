"""Tests for aiobt.network — address detection and configuration."""

from __future__ import annotations

import unittest

import attrs

from aiobt.network import (
    AddressFamily,
    NetworkConfig,
    detect_address_families,
    resolve_families,
)


class TestNetworkConfig(unittest.TestCase):
    """Test NetworkConfig frozen attrs model."""

    def test_defaults(self) -> None:
        cfg = NetworkConfig()
        self.assertEqual(cfg.address_family, AddressFamily.AUTO)
        self.assertTrue(cfg.lsd_enabled)
        self.assertEqual(cfg.lsd_announce_interval, 300.0)
        self.assertEqual(cfg.bind_addresses, ())

    def test_frozen(self) -> None:
        cfg = NetworkConfig()
        with self.assertRaises(attrs.exceptions.FrozenInstanceError):
            cfg.lsd_enabled = False  # type: ignore[misc]

    def test_ipv6_only(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.IPV6_ONLY)
        self.assertEqual(cfg.address_family, AddressFamily.IPV6_ONLY)

    def test_lsd_disabled(self) -> None:
        cfg = NetworkConfig(lsd_enabled=False)
        self.assertFalse(cfg.lsd_enabled)

    def test_bind_addresses(self) -> None:
        cfg = NetworkConfig(bind_addresses=("192.168.1.50", "fd00::1"))
        self.assertEqual(cfg.bind_addresses, ("192.168.1.50", "fd00::1"))

    def test_bind_addresses_validation_empty_string(self) -> None:
        with self.assertRaises(ValueError):
            NetworkConfig(bind_addresses=("",))

    def test_custom_announce_interval(self) -> None:
        cfg = NetworkConfig(lsd_announce_interval=60.0)
        self.assertEqual(cfg.lsd_announce_interval, 60.0)


class TestResolveFamilies(unittest.TestCase):
    """Test resolve_families logic."""

    def test_ipv4_only(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.IPV4_ONLY)
        self.assertEqual(resolve_families(cfg), (True, False))

    def test_ipv6_only(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.IPV6_ONLY)
        self.assertEqual(resolve_families(cfg), (False, True))

    def test_dual(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.DUAL)
        self.assertEqual(resolve_families(cfg), (True, True))

    def test_disabled(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.DISABLED)
        self.assertEqual(resolve_families(cfg), (False, False))

    def test_auto_returns_bools(self) -> None:
        cfg = NetworkConfig(address_family=AddressFamily.AUTO)
        v4, v6 = resolve_families(cfg)
        self.assertIsInstance(v4, bool)
        self.assertIsInstance(v6, bool)

    def test_bind_addresses_infer_ipv4(self) -> None:
        cfg = NetworkConfig(bind_addresses=("10.0.0.1",))
        v4, v6 = resolve_families(cfg)
        self.assertTrue(v4)
        self.assertFalse(v6)

    def test_bind_addresses_infer_ipv6(self) -> None:
        cfg = NetworkConfig(bind_addresses=("::1",))
        v4, v6 = resolve_families(cfg)
        self.assertFalse(v4)
        self.assertTrue(v6)

    def test_bind_addresses_infer_dual(self) -> None:
        cfg = NetworkConfig(bind_addresses=("192.168.1.1", "fd00::1"))
        v4, v6 = resolve_families(cfg)
        self.assertTrue(v4)
        self.assertTrue(v6)


class TestDetectAddressFamilies(unittest.TestCase):
    """Test runtime address family detection."""

    def test_returns_tuple_of_bools(self) -> None:
        result = detect_address_families()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)
        self.assertIsInstance(result[1], bool)

    def test_at_least_one_family(self) -> None:
        """Any machine running tests should have at least IPv4."""
        v4, v6 = detect_address_families()
        self.assertTrue(v4 or v6)


if __name__ == "__main__":
    unittest.main()
