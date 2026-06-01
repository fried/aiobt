"""Network configuration and address detection for aiobt.

Provides :class:`NetworkConfig` — an immutable configuration object
that controls IPv4/IPv6 binding, local peer discovery, address
auto-detection, and DSCP traffic classification.
"""

from __future__ import annotations

import enum
import socket

from dataclasses import dataclass, field


class DSCPValue(enum.IntEnum):
    """Common DSCP codepoints (RFC 2474, RFC 8622, RFC 4594).

    The value stored is the raw 6-bit DSCP field.  To set the IP TOS
    byte, shift left by 2: ``tos = dscp << 2``.  IPv6 traffic class
    uses the same layout.
    """

    # --- Default / Best Effort ---
    CS0 = 0
    """Default forwarding / best effort (DSCP 0)."""

    # --- Low-priority / scavenger ---
    LE = 1
    """Lower-Effort (RFC 8622).  Below best effort — ideal for bulk
    transfers like BitTorrent that should yield to everything else."""

    CS1 = 8
    """Class Selector 1 — scavenger / low-priority bulk data."""

    # --- Assured Forwarding (AF) classes ---
    AF11 = 10
    AF12 = 12
    AF13 = 14

    AF21 = 18
    AF22 = 20
    AF23 = 22

    AF31 = 26
    AF32 = 28
    AF33 = 30

    AF41 = 34
    AF42 = 36
    AF43 = 38

    # --- Class Selectors ---
    CS2 = 16
    CS3 = 24
    CS4 = 32
    CS5 = 40
    CS6 = 48
    CS7 = 56

    # --- Expedited Forwarding ---
    EF = 46
    """Expedited Forwarding — low latency, low jitter (voice/video)."""

    VOICE_ADMIT = 44
    """Voice-Admit (RFC 5765)."""


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

    dscp: DSCPValue | int = DSCPValue.CS0
    """DSCP codepoint applied to all outgoing sockets.

    Accepts a :class:`DSCPValue` enum member or a raw integer (0–63).
    Default is ``CS0`` (best-effort).  For polite bulk transfers that
    should yield to interactive traffic, use ``DSCPValue.LE`` or
    ``DSCPValue.CS1``.
    """

    def __post_init__(self) -> None:
        for addr in self.bind_addresses:
            if not isinstance(addr, str) or not addr:
                raise ValueError(
                    f"bind address must be a non-empty string, got {addr!r}"
                )
        dscp_val = int(self.dscp)
        if dscp_val < 0 or dscp_val > 63:
            raise ValueError(f"DSCP value must be 0–63, got {dscp_val}")


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


def dscp_to_tos(dscp: DSCPValue | int) -> int:
    """Convert a DSCP codepoint (0–63) to the full IP TOS byte value.

    The TOS / traffic-class byte is ``(DSCP << 2) | ECN``.  ECN bits
    are left at zero (not-ECT) since we never set them ourselves.
    """
    return int(dscp) << 2


def apply_dscp(sock: socket.socket, dscp: DSCPValue | int) -> None:
    """Set the DSCP codepoint on *sock*.

    Works for both IPv4 (``IP_TOS``) and IPv6 (``IPV6_TCLASS``)
    sockets.  The call is a no-op when *dscp* is ``CS0`` (0) since
    that's already the kernel default.

    Silently ignores ``OSError`` so unprivileged processes that cannot
    set TOS don't crash — the packet just goes out as best effort.
    """
    val = int(dscp)
    if val == 0:
        return  # CS0 is the default; nothing to set

    tos = dscp_to_tos(val)
    try:
        family = sock.family
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, tos)
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
    except OSError:
        # Some platforms/containers restrict TOS changes — degrade
        # silently to best-effort rather than crashing.
        pass
