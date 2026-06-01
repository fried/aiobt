"""Tests for aiobt.network — address detection, configuration, and DSCP."""

from __future__ import annotations

import socket
import unittest
from dataclasses import FrozenInstanceError

from aiobt.network import (
    AddressFamily,
    DSCPValue,
    NetworkConfig,
    apply_dscp,
    detect_address_families,
    dscp_to_tos,
    resolve_families,
)


class TestNetworkConfig(unittest.TestCase):
    """Test NetworkConfig frozen dataclass model."""

    def test_defaults(self) -> None:
        cfg = NetworkConfig()
        self.assertEqual(cfg.address_family, AddressFamily.AUTO)
        self.assertTrue(cfg.lsd_enabled)
        self.assertEqual(cfg.lsd_announce_interval, 300.0)
        self.assertEqual(cfg.bind_addresses, ())

    def test_frozen(self) -> None:
        cfg = NetworkConfig()
        with self.assertRaises(FrozenInstanceError):
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


class TestDSCPValue(unittest.TestCase):
    """Test DSCPValue enum constants."""

    def test_cs0_is_zero(self) -> None:
        self.assertEqual(int(DSCPValue.CS0), 0)

    def test_le_is_one(self) -> None:
        self.assertEqual(int(DSCPValue.LE), 1)

    def test_cs1_is_eight(self) -> None:
        self.assertEqual(int(DSCPValue.CS1), 8)

    def test_ef_is_46(self) -> None:
        self.assertEqual(int(DSCPValue.EF), 46)

    def test_af11_is_10(self) -> None:
        self.assertEqual(int(DSCPValue.AF11), 10)

    def test_all_values_in_range(self) -> None:
        for member in DSCPValue:
            self.assertGreaterEqual(int(member), 0)
            self.assertLessEqual(int(member), 63)


class TestDscpToTos(unittest.TestCase):
    """Test DSCP-to-TOS byte conversion."""

    def test_cs0_to_tos(self) -> None:
        self.assertEqual(dscp_to_tos(DSCPValue.CS0), 0)

    def test_le_to_tos(self) -> None:
        # LE (1) -> TOS 4  (0b00000100)
        self.assertEqual(dscp_to_tos(DSCPValue.LE), 4)

    def test_cs1_to_tos(self) -> None:
        # CS1 (8) -> TOS 32  (0b00100000)
        self.assertEqual(dscp_to_tos(DSCPValue.CS1), 32)

    def test_ef_to_tos(self) -> None:
        # EF (46) -> TOS 184  (0b10111000)
        self.assertEqual(dscp_to_tos(DSCPValue.EF), 184)

    def test_raw_int(self) -> None:
        self.assertEqual(dscp_to_tos(0), 0)
        self.assertEqual(dscp_to_tos(63), 252)

    def test_shift_preserves_ecn_bits(self) -> None:
        """ECN bits (lower 2) must be zero."""
        for dscp in range(64):
            tos = dscp_to_tos(dscp)
            self.assertEqual(
                tos & 0x03, 0, f"DSCP {dscp} -> TOS {tos} has ECN bits set"
            )


class TestApplyDscp(unittest.TestCase):
    """Test apply_dscp socket helper."""

    def test_cs0_is_noop(self) -> None:
        """CS0 (default) should not call setsockopt."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Should not raise
            apply_dscp(sock, DSCPValue.CS0)
            apply_dscp(sock, 0)
        finally:
            sock.close()

    def test_set_cs1_ipv4(self) -> None:
        """CS1 should set TOS on an IPv4 socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            apply_dscp(sock, DSCPValue.CS1)
            tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            self.assertEqual(tos, dscp_to_tos(DSCPValue.CS1))
        finally:
            sock.close()

    def test_set_le_ipv4(self) -> None:
        """LE should set TOS on an IPv4 socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            apply_dscp(sock, DSCPValue.LE)
            tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            self.assertEqual(tos, dscp_to_tos(DSCPValue.LE))
        finally:
            sock.close()

    def test_set_raw_int_ipv4(self) -> None:
        """Raw integer DSCP should work too."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            apply_dscp(sock, 46)  # EF
            tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            self.assertEqual(tos, 184)
        finally:
            sock.close()

    def test_set_cs1_ipv6(self) -> None:
        """CS1 should set TCLASS on an IPv6 socket."""
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        except OSError:
            self.skipTest("IPv6 not available")
        try:
            apply_dscp(sock, DSCPValue.CS1)
            tclass = sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS)
            self.assertEqual(tclass, dscp_to_tos(DSCPValue.CS1))
        finally:
            sock.close()


class TestNetworkConfigDSCP(unittest.TestCase):
    """Test DSCP field on NetworkConfig."""

    def test_default_is_cs0(self) -> None:
        cfg = NetworkConfig()
        self.assertEqual(cfg.dscp, DSCPValue.CS0)

    def test_set_enum(self) -> None:
        cfg = NetworkConfig(dscp=DSCPValue.LE)
        self.assertEqual(cfg.dscp, DSCPValue.LE)

    def test_set_raw_int(self) -> None:
        cfg = NetworkConfig(dscp=8)
        self.assertEqual(int(cfg.dscp), 8)

    def test_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            NetworkConfig(dscp=-1)

    def test_rejects_over_63(self) -> None:
        with self.assertRaises(ValueError):
            NetworkConfig(dscp=64)

    def test_boundary_63_ok(self) -> None:
        cfg = NetworkConfig(dscp=63)
        self.assertEqual(int(cfg.dscp), 63)


if __name__ == "__main__":
    unittest.main()
