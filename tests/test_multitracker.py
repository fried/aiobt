"""Tests for BEP 12 multi-tracker announce."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import AsyncMock, patch

import later.unittest

from aiobt.client import _build_tracker_tiers, _TorrentSession, ClientConfig
from aiobt.events import EventEmitter
from aiobt.torrent import TorrentInfo, TorrentMeta
from aiobt.tracker import AnnounceResponse, TrackerError


def _make_meta(
    *,
    announce: str | None = None,
    announce_list: tuple[tuple[str, ...], ...] | None = None,
) -> TorrentMeta:
    """Create minimal TorrentMeta for tracker tests."""
    pieces = os.urandom(20)  # 1 piece
    info = TorrentInfo(
        name="test",
        piece_length=65536,
        pieces_raw=pieces,
        length=65536,
    )
    info_dict_bytes = b"d4:name4:test12:piece lengthi65536e6:pieces20:"
    info_dict_bytes += pieces + b"e"
    info_hash = hashlib.sha1(info_dict_bytes).digest()
    return TorrentMeta(
        info=info,
        info_hash=os.urandom(20),  # doesn't matter for tracker tests
        announce=announce,
        announce_list=announce_list,
    )


class BuildTiersTest(later.unittest.TestCase):
    """Test _build_tracker_tiers helper."""

    async def test_announce_only(self) -> None:
        meta = _make_meta(announce="http://tracker1.example.com/announce")
        tiers = _build_tracker_tiers(meta)
        self.assertEqual(tiers, [["http://tracker1.example.com/announce"]])

    async def test_announce_list_overrides_announce(self) -> None:
        """BEP 12: announce-list takes precedence over announce."""
        meta = _make_meta(
            announce="http://old.example.com/announce",
            announce_list=(
                (
                    "http://tier1a.example.com/announce",
                    "http://tier1b.example.com/announce",
                ),
                ("http://tier2.example.com/announce",),
            ),
        )
        tiers = _build_tracker_tiers(meta)
        self.assertEqual(len(tiers), 2)
        self.assertEqual(len(tiers[0]), 2)
        self.assertIn("http://tier1a.example.com/announce", tiers[0])
        self.assertIn("http://tier1b.example.com/announce", tiers[0])
        self.assertEqual(tiers[1], ["http://tier2.example.com/announce"])
        # "old" announce should NOT appear
        flat = [url for tier in tiers for url in tier]
        self.assertNotIn("http://old.example.com/announce", flat)

    async def test_no_trackers(self) -> None:
        meta = _make_meta()
        tiers = _build_tracker_tiers(meta)
        self.assertEqual(tiers, [])

    async def test_empty_tiers_filtered(self) -> None:
        meta = _make_meta(
            announce_list=(
                (),  # empty tier
                ("http://real.example.com/announce",),
            ),
        )
        tiers = _build_tracker_tiers(meta)
        self.assertEqual(len(tiers), 1)
        self.assertEqual(tiers[0], ["http://real.example.com/announce"])

    async def test_tiers_are_mutable_copies(self) -> None:
        """Returned tiers should be mutable (for URL promotion)."""
        meta = _make_meta(
            announce_list=(
                ("http://a.example.com/announce", "http://b.example.com/announce"),
            ),
        )
        tiers = _build_tracker_tiers(meta)
        tiers[0].remove("http://a.example.com/announce")
        tiers[0].insert(0, "http://b.example.com/announce")
        # Should not raise


class TieredAnnounceTest(later.unittest.TestCase):
    """Test _TorrentSession.do_announce with tiered trackers."""

    def _make_session(
        self,
        announce_list: tuple[tuple[str, ...], ...],
    ) -> _TorrentSession:
        from aiobt.network import NetworkConfig
        from aiobt.storage.compact import CompactStorage

        meta = _make_meta(announce_list=announce_list)
        storage = CompactStorage("/tmp/test-unused")
        config = ClientConfig(
            listen_port=6881,
            network=NetworkConfig(lsd_enabled=False),
        )
        return _TorrentSession(meta, storage, config)

    async def test_first_tier_success(self) -> None:
        """Announce succeeds on the first tier."""
        session = self._make_session(
            announce_list=(
                ("http://good.example.com/announce",),
                ("http://backup.example.com/announce",),
            ),
        )
        ok_response = AnnounceResponse(interval=1800, peers=())

        with patch("aiobt.client.announce", new_callable=AsyncMock) as mock_announce:
            mock_announce.return_value = ok_response

            # Create a minimal handle stub
            handle = type("Handle", (), {"_session": session})()
            result = await session.do_announce(handle=handle, event="started")

            self.assertEqual(result.interval, 1800)
            # Only the first tier URL should have been tried
            mock_announce.assert_called_once()
            call_url = mock_announce.call_args[0][0]
            self.assertEqual(call_url, "http://good.example.com/announce")

    async def test_fallback_to_second_tier(self) -> None:
        """First tier fails, second tier succeeds."""
        session = self._make_session(
            announce_list=(
                ("http://dead.example.com/announce",),
                ("http://alive.example.com/announce",),
            ),
        )
        ok_response = AnnounceResponse(interval=900, peers=(("1.2.3.4", 6881),))

        async def side_effect(url, request):
            if "dead" in url:
                raise TrackerError("connection refused")
            return ok_response

        with patch("aiobt.client.announce", side_effect=side_effect):
            handle = type("Handle", (), {"_session": session})()
            result = await session.do_announce(handle=handle)

            self.assertEqual(result.interval, 900)
            self.assertEqual(len(result.peers), 1)

    async def test_all_tiers_fail(self) -> None:
        """All trackers fail → TrackerError raised."""
        session = self._make_session(
            announce_list=(
                ("http://dead1.example.com/announce",),
                ("http://dead2.example.com/announce",),
            ),
        )

        with patch("aiobt.client.announce", new_callable=AsyncMock) as mock:
            mock.side_effect = TrackerError("nope")

            handle = type("Handle", (), {"_session": session})()
            with self.assertRaises(TrackerError) as ctx:
                await session.do_announce(handle=handle)
            self.assertIn("all trackers failed", str(ctx.exception))

    async def test_url_promotion(self) -> None:
        """Successful URL is promoted to front of its tier."""
        session = self._make_session(
            announce_list=(
                (
                    "http://slow.example.com/announce",
                    "http://fast.example.com/announce",
                ),
            ),
        )
        ok_response = AnnounceResponse(interval=1800, peers=())

        async def side_effect(url, request):
            if "slow" in url:
                raise TrackerError("timeout")
            return ok_response

        with patch("aiobt.client.announce", side_effect=side_effect):
            handle = type("Handle", (), {"_session": session})()
            await session.do_announce(handle=handle)

            # "fast" should now be first in tier 0
            self.assertEqual(
                session._tracker_tiers[0][0],
                "http://fast.example.com/announce",
            )

    async def test_no_trackers_raises(self) -> None:
        """Empty tracker list raises TrackerError."""
        session = self._make_session(announce_list=())
        handle = type("Handle", (), {"_session": session})()
        with self.assertRaises(TrackerError):
            await session.do_announce(handle=handle)

    async def test_single_announce_no_list(self) -> None:
        """Single announce URL (no announce-list) still works."""
        from aiobt.network import NetworkConfig
        from aiobt.storage.compact import CompactStorage

        meta = _make_meta(announce="http://solo.example.com/announce")
        storage = CompactStorage("/tmp/test-unused")
        config = ClientConfig(
            listen_port=6881,
            network=NetworkConfig(lsd_enabled=False),
        )
        session = _TorrentSession(meta, storage, config)
        ok_response = AnnounceResponse(interval=600, peers=())

        with patch("aiobt.client.announce", new_callable=AsyncMock) as mock:
            mock.return_value = ok_response
            handle = type("Handle", (), {"_session": session})()
            result = await session.do_announce(handle=handle)
            self.assertEqual(result.interval, 600)
            mock.assert_called_once()
