"""Network configuration and address detection for aiobt.

Provides :class:`NetworkConfig` — an immutable configuration object
that controls IPv4/IPv6 binding, local peer discovery, and address
auto-detection.
"""

from __future__ import annotations

import enum
import socket

from dataclasses import dataclass, field


class AddressFamily(enum.Enum):
    """Which address families to enable."""

    AUTO = "auto"
    """Detect available families at startup (default)."""

    IPV4_ONLY = "ipv4_only"
    """Force IPv4 only."""

    IPV6_ONLY = "ipv6_only"
    """Force IPv6 only."""

    DUAL = "dual"
    """Force both IPv4 and IPv6."""

    DISABLED = "disabled"
    """Disable all networking (testing only)."""


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Immutable network configuration.

    By default, aiobt detects which address families are available on
    the machine and enables them automatically.  Local Service Discovery
    (BEP 26) is enabled by default for every active address family.

    Examples
    --------
    Fully automatic (recommended)::

        config = NetworkConfig()

    IPv6 only, no LSD::

        config = NetworkConfig(
            address_family=AddressFamily.IPV6_ONLY,
            lsd_enabled=False,
        )

    Explicit bind addresses::

        config = NetworkConfig(
            bind_addresses=["192.168.1.50", "fd00::1"],
        )
    """

    address_family: AddressFamily = AddressFamily.AUTO
    """Which address families to use.  ``AUTO`` probes at startup."""

    lsd_enabled: bool = True
    """Enable Local Service Discovery (BEP 26) by default."""

    lsd_announce_interval: float = 300.0
    """Seconds between LSD announce rounds (default 5 min)."""

    bind_addresses: tuple[str, ...] = field(default_factory=tuple)
    """Explicit addresses to bind listeners to.

    When empty (default), binds to ``0.0.0.0`` and/or ``::`` based
    on the resolved address family.  When provided, these override
    address family detection — each address is bound as-is.
    """

    def __post_init__(self) -> None:
        for addr in self.bind_addresses:
            if not isinstance(addr, str) or not addr:
                raise ValueError(
                    f"bind address must be a non-empty string, got {addr!r}"
                )


def detect_address_families() -> tuple[bool, bool]:
    """Probe whether the machine has usable IPv4 and IPv6 addresses.

    Returns ``(has_ipv4, has_ipv6)``.  Detection binds an ephemeral
    UDP socket to a non-routable address — no traffic leaves the host.
    """
    has_ipv4 = False
    has_ipv6 = False

    # IPv4: try to "connect" a UDP socket to a non-routable address
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))  # TEST-NET, never routed
            has_ipv4 = True
    except OSError:
        pass

    # IPv6: same trick with a documentation-range address
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as s:
            s.connect(("2001:db8::1", 1))  # documentation prefix
            has_ipv6 = True
    except OSError:
        pass

    return has_ipv4, has_ipv6


def resolve_families(config: NetworkConfig) -> tuple[bool, bool]:
    """Resolve the effective ``(use_ipv4, use_ipv6)`` from *config*.

    When ``bind_addresses`` are provided, families are inferred from
    the address literals.  Otherwise the ``address_family`` setting
    is consulted, with ``AUTO`` triggering runtime detection.
    """
    if config.bind_addresses:
        use_v4 = False
        use_v6 = False
        for addr in config.bind_addresses:
            try:
                socket.inet_pton(socket.AF_INET6, addr)
                use_v6 = True
            except OSError:
                try:
                    socket.inet_pton(socket.AF_INET, addr)
                    use_v4 = True
                except OSError:
                    # Hostname — assume IPv4
                    use_v4 = True
        return use_v4, use_v6

    match config.address_family:
        case AddressFamily.AUTO:
            return detect_address_families()
        case AddressFamily.IPV4_ONLY:
            return True, False
        case AddressFamily.IPV6_ONLY:
            return False, True
        case AddressFamily.DUAL:
            return True, True
        case AddressFamily.DISABLED:
            return False, False
