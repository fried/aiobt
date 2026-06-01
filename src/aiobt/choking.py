"""BEP 3 choking algorithm — tit-for-tat with optimistic unchoke.

Every *rechoke_interval* seconds (default 10):

-  **Downloading**: rank peers by download rate (bytes they sent us
   recently).  Unchoke the top *max_unchoked* interested peers, choke
   the rest.
-  **Seeding**: rank peers by upload rate (bytes we sent them
   recently).  Unchoke the top *max_unchoked* interested peers.

Every *optimistic_interval* seconds (default 30):

-  Pick one random choked-but-interested peer and unchoke it
   (optimistic unchoke), giving new arrivals a chance to prove
   themselves.
"""

from __future__ import annotations

import asyncio
import random

from .protocol import Choke, Unchoke

# Re-exports for type hints only — the actual types are resolved at
# runtime to avoid circular imports.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .peer import PeerConnection


# ---------------------------------------------------------------------------
# Per-peer rate tracker
# ---------------------------------------------------------------------------


class PeerRates:
    """Mutable per-peer rate tracker used by the choking manager."""

    __slots__ = (
        "bytes_down_interval",
        "bytes_up_interval",
        "am_choking",
        "peer_interested",
        "peer",
    )

    def __init__(self, peer: PeerConnection) -> None:
        self.peer = peer
        self.bytes_down_interval: int = 0
        self.bytes_up_interval: int = 0
        self.am_choking: bool = True
        self.peer_interested: bool = False

    def reset_interval(self) -> None:
        """Zero the per-interval counters (called each rechoke)."""
        self.bytes_down_interval = 0
        self.bytes_up_interval = 0


# ---------------------------------------------------------------------------
# ChokingManager
# ---------------------------------------------------------------------------


class ChokingManager:
    """BEP 3 choking algorithm for one torrent session.

    Parameters
    ----------
    max_unchoked:
        Number of regular unchoke slots (default 4).
    rechoke_interval:
        Seconds between rechoke rounds (default 10).
    optimistic_interval:
        Seconds between optimistic unchoke rotation (default 30).
    """

    def __init__(
        self,
        *,
        max_unchoked: int = 4,
        rechoke_interval: float = 10.0,
        optimistic_interval: float = 30.0,
    ) -> None:
        self._max_unchoked = max_unchoked
        self._rechoke_interval = rechoke_interval
        self._optimistic_interval = optimistic_interval
        self._rates: dict[tuple[str, int], PeerRates] = {}
        self._optimistic_addr: tuple[str, int] | None = None
        self._ticks_since_optimistic: int = 0
        self._optimistic_every: int = max(
            1, round(optimistic_interval / rechoke_interval)
        )
        self._is_seeding: bool = False
        self._wake_event: asyncio.Event = asyncio.Event()

    # ----- peer registration -----------------------------------------------

    def register(self, addr: tuple[str, int], peer: PeerConnection) -> PeerRates:
        """Register a peer and return its rate tracker."""
        rates = PeerRates(peer)
        self._rates[addr] = rates
        return rates

    def unregister(self, addr: tuple[str, int]) -> None:
        """Remove a peer."""
        self._rates.pop(addr, None)
        if self._optimistic_addr == addr:
            self._optimistic_addr = None

    def wake(self) -> None:
        """Trigger an immediate rechoke (e.g. when peer interest changes)."""
        self._wake_event.set()

    @property
    def is_seeding(self) -> bool:
        return self._is_seeding

    @is_seeding.setter
    def is_seeding(self, value: bool) -> None:
        self._is_seeding = value

    # ----- main loop -------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run the rechoke loop until *stop_event* is set."""
        tasks: list[asyncio.Task[object]] = []
        try:
            while not stop_event.is_set():
                await self._rechoke()
                self._wake_event.clear()
                wake_task = asyncio.create_task(self._wake_event.wait())
                stop_task = asyncio.create_task(stop_event.wait())
                timer_task = asyncio.create_task(asyncio.sleep(self._rechoke_interval))
                tasks = [wake_task, stop_task, timer_task]
                try:
                    done, pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    tasks.clear()
                    if stop_task in done:
                        break
                except asyncio.CancelledError:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()
                    raise
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            return

    async def _rechoke(self) -> None:
        """Run one rechoke cycle."""
        self._ticks_since_optimistic += 1
        rotate_optimistic = self._ticks_since_optimistic >= self._optimistic_every
        if rotate_optimistic:
            self._ticks_since_optimistic = 0

        interested: list[tuple[tuple[str, int], PeerRates]] = [
            (addr, r)
            for addr, r in self._rates.items()
            if r.peer_interested and r.peer.is_connected
        ]

        if not interested:
            return

        # Rank by the relevant rate
        if self._is_seeding:
            interested.sort(key=lambda x: x[1].bytes_up_interval, reverse=True)
        else:
            interested.sort(key=lambda x: x[1].bytes_down_interval, reverse=True)

        # Pick unchoke set: top N
        to_unchoke: set[tuple[str, int]] = set()
        for addr, _ in interested[: self._max_unchoked]:
            to_unchoke.add(addr)

        # Optimistic unchoke: one random choked+interested peer
        if rotate_optimistic:
            choked_interested = [
                addr for addr, _ in interested if addr not in to_unchoke
            ]
            if choked_interested:
                self._optimistic_addr = random.choice(choked_interested)
            else:
                self._optimistic_addr = None

        if self._optimistic_addr is not None:
            to_unchoke.add(self._optimistic_addr)

        # Apply choke/unchoke decisions
        for addr, rates in self._rates.items():
            if not rates.peer.is_connected:
                continue
            should_unchoke = addr in to_unchoke
            if should_unchoke and rates.am_choking:
                rates.am_choking = False
                try:
                    await rates.peer.send_message(Unchoke())
                except ConnectionError, OSError:
                    pass
            elif not should_unchoke and not rates.am_choking:
                rates.am_choking = True
                try:
                    await rates.peer.send_message(Choke())
                except ConnectionError, OSError:
                    pass

        # Reset interval counters
        for rates in self._rates.values():
            rates.reset_interval()
