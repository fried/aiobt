"""Tests for Message Stream Encryption / Protocol Encryption (MSE/PE)."""

from __future__ import annotations

import asyncio
import os
import struct
import unittest

from aiobt.mse import (
    EncryptionPolicy,
    EncryptedStream,
    PlaintextStream,
    RC4,
    _dh_keypair,
    _dh_secret,
    _derive_keys,
    mse_initiate,
    mse_receive,
)


# ---------------------------------------------------------------------------
# RC4 tests
# ---------------------------------------------------------------------------


class TestRC4(unittest.TestCase):
    """RC4 cipher basics."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        key = os.urandom(20)
        plaintext = os.urandom(1024)
        enc = RC4(key)
        dec = RC4(key)
        ciphertext = enc.process(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        recovered = dec.process(ciphertext)
        self.assertEqual(recovered, plaintext)

    def test_encrypt_decrypt_with_discard(self) -> None:
        key = os.urandom(20)
        plaintext = os.urandom(512)
        enc = RC4(key, discard=1024)
        dec = RC4(key, discard=1024)
        ciphertext = enc.process(plaintext)
        recovered = dec.process(ciphertext)
        self.assertEqual(recovered, plaintext)

    def test_discard_changes_keystream(self) -> None:
        """Discarding bytes produces a different keystream."""
        key = os.urandom(20)
        data = os.urandom(64)
        c1 = RC4(key, discard=0).process(data)
        c2 = RC4(key, discard=1024).process(data)
        self.assertNotEqual(c1, c2)

    def test_streaming(self) -> None:
        """Processing in chunks gives the same result as all at once."""
        key = os.urandom(20)
        data = os.urandom(2048)

        full = RC4(key).process(data)

        chunked = b""
        rc4 = RC4(key)
        for i in range(0, len(data), 100):
            chunked += rc4.process(data[i : i + 100])

        self.assertEqual(chunked, full)

    def test_empty_data(self) -> None:
        rc4 = RC4(b"key")
        self.assertEqual(rc4.process(b""), b"")


# ---------------------------------------------------------------------------
# DH key exchange tests
# ---------------------------------------------------------------------------


class TestDHKeyExchange(unittest.TestCase):
    """Diffie-Hellman key agreement."""

    def test_shared_secret_agreement(self) -> None:
        """Both sides derive the same shared secret."""
        xa, ya_bytes = _dh_keypair()
        xb, yb_bytes = _dh_keypair()

        secret_a = _dh_secret(yb_bytes, xa)
        secret_b = _dh_secret(ya_bytes, xb)

        self.assertEqual(secret_a, secret_b)
        self.assertEqual(len(secret_a), 96)

    def test_different_keypairs(self) -> None:
        """Each call generates a different keypair."""
        _, ya1 = _dh_keypair()
        _, ya2 = _dh_keypair()
        self.assertNotEqual(ya1, ya2)

    def test_public_key_length(self) -> None:
        _, pub = _dh_keypair()
        self.assertEqual(len(pub), 96)


# ---------------------------------------------------------------------------
# Key derivation tests
# ---------------------------------------------------------------------------


class TestKeyDerivation(unittest.TestCase):
    """RC4 key derivation from DH secret."""

    def test_initiator_and_receiver_keys_are_swapped(self) -> None:
        """Initiator's encrypt key == receiver's decrypt key and vice versa."""
        secret = os.urandom(96)
        info_hash = os.urandom(20)

        enc_a, dec_a = _derive_keys(secret, info_hash, initiator=True)
        enc_b, dec_b = _derive_keys(secret, info_hash, initiator=False)

        plaintext = os.urandom(256)

        # A encrypts → B decrypts
        ct = enc_a.process(plaintext)
        pt = dec_b.process(ct)
        self.assertEqual(pt, plaintext)

        # B encrypts → A decrypts
        plaintext2 = os.urandom(256)
        ct2 = enc_b.process(plaintext2)
        pt2 = dec_a.process(ct2)
        self.assertEqual(pt2, plaintext2)


# ---------------------------------------------------------------------------
# Stream wrapper tests
# ---------------------------------------------------------------------------


class TestEncryptedStream(unittest.TestCase):
    """EncryptedStream read/write interface."""

    def test_readexactly_and_write(self) -> None:
        """Data encrypted by writer is decrypted by reader."""
        key = os.urandom(20)
        plaintext = b"Hello, encrypted world!"

        async def run() -> None:
            b_reader = asyncio.StreamReader()

            # Encrypt and feed into the reader
            enc_a = RC4(key, discard=1024)

            ct = enc_a.process(plaintext)
            b_reader.feed_data(ct)

            # b decrypts using its own RC4 (same key → same keystream for test)
            dec_b = RC4(key, discard=1024)
            stream = EncryptedStream(
                b_reader,
                None,  # type: ignore[arg-type] — writer not used here
                RC4(key, discard=1024),  # encrypt (unused)
                dec_b,
            )
            result = await stream.readexactly(len(plaintext))
            self.assertEqual(result, plaintext)

        asyncio.run(run())


# ---------------------------------------------------------------------------
# TCP pipe helper
# ---------------------------------------------------------------------------


async def _create_tcp_pipe() -> tuple[
    asyncio.StreamReader,
    asyncio.StreamWriter,
    asyncio.StreamReader,
    asyncio.StreamWriter,
]:
    """Create a bidirectional pipe using a real TCP loopback connection."""
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.get_running_loop().create_future()
    )

    async def _on_connect(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted.set_result((reader, writer))

    server = await asyncio.start_server(_on_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    a_reader, a_writer = await asyncio.open_connection("127.0.0.1", port)
    b_reader, b_writer = await accepted

    server.close()

    return a_reader, a_writer, b_reader, b_writer


def _close_writers(*writers: asyncio.StreamWriter) -> None:
    """Best-effort close for StreamWriters."""
    for w in writers:
        try:
            w.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Full MSE handshake tests
# ---------------------------------------------------------------------------


class TestMSEHandshake(unittest.TestCase):
    """Full MSE/PE handshake between initiator and receiver."""

    def test_rc4_handshake(self) -> None:
        """Both sides negotiate RC4 encryption successfully."""
        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(
                        a_reader, a_writer, info_hash, policy=EncryptionPolicy.PREFERRED
                    ),
                    mse_receive(
                        b_reader, b_writer, info_hash, policy=EncryptionPolicy.PREFERRED
                    ),
                )

                self.assertTrue(result_a.encrypted)
                self.assertTrue(result_b.encrypted)
                self.assertIsInstance(result_a.stream, EncryptedStream)
                self.assertIsInstance(result_b.stream, EncryptedStream)
                self.assertEqual(result_b.info_hash, info_hash)

                # Verify data exchange works after handshake
                msg = b"Hello from A!"
                result_a.stream.write(msg)
                await result_a.stream.drain()
                received = await result_b.stream.readexactly(len(msg))
                self.assertEqual(received, msg)

                msg2 = b"Hello from B!"
                result_b.stream.write(msg2)
                await result_b.stream.drain()
                received2 = await result_a.stream.readexactly(len(msg2))
                self.assertEqual(received2, msg2)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_forced_rc4_handshake(self) -> None:
        """FORCED policy on both sides → RC4."""
        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(
                        a_reader, a_writer, info_hash, policy=EncryptionPolicy.FORCED
                    ),
                    mse_receive(
                        b_reader, b_writer, info_hash, policy=EncryptionPolicy.FORCED
                    ),
                )
                self.assertTrue(result_a.encrypted)
                self.assertTrue(result_b.encrypted)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_plaintext_negotiation(self) -> None:
        """Receiver DISABLED → plaintext selected when initiator offers both."""
        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(
                        a_reader, a_writer, info_hash, policy=EncryptionPolicy.PREFERRED
                    ),
                    mse_receive(
                        b_reader, b_writer, info_hash, policy=EncryptionPolicy.DISABLED
                    ),
                )
                self.assertFalse(result_a.encrypted)
                self.assertFalse(result_b.encrypted)
                self.assertIsInstance(result_a.stream, PlaintextStream)
                self.assertIsInstance(result_b.stream, PlaintextStream)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_forced_rejects_plaintext_only_peer(self) -> None:
        """FORCED receiver rejects an initiator offering only plaintext."""
        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                with self.assertRaises((ValueError, ConnectionError)):
                    await asyncio.gather(
                        mse_initiate(
                            a_reader,
                            a_writer,
                            info_hash,
                            policy=EncryptionPolicy.DISABLED,
                        ),
                        mse_receive(
                            b_reader,
                            b_writer,
                            info_hash,
                            policy=EncryptionPolicy.FORCED,
                        ),
                    )
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_multi_hash_lookup(self) -> None:
        """Receiver can identify the correct info_hash from a set."""
        target_hash = os.urandom(20)
        decoy1 = os.urandom(20)
        decoy2 = os.urandom(20)
        lookup = {decoy1: decoy1, target_hash: target_hash, decoy2: decoy2}

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(a_reader, a_writer, target_hash),
                    mse_receive(b_reader, b_writer, lookup),
                )
                self.assertEqual(result_b.info_hash, target_hash)
                self.assertTrue(result_a.encrypted)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_data_exchange_after_handshake(self) -> None:
        """Large bidirectional data transfer works after MSE handshake."""
        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(a_reader, a_writer, info_hash),
                    mse_receive(b_reader, b_writer, info_hash),
                )

                # Send a larger block (simulating BT piece data)
                block = os.urandom(16384)  # 16 KiB — standard block size
                result_a.stream.write(block)
                await result_a.stream.drain()
                received = await result_b.stream.readexactly(len(block))
                self.assertEqual(received, block)

                # Reverse direction
                block2 = os.urandom(16384)
                result_b.stream.write(block2)
                await result_b.stream.drain()
                received2 = await result_a.stream.readexactly(len(block2))
                self.assertEqual(received2, block2)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_bt_handshake_over_mse(self) -> None:
        """Full BT protocol handshake works over an MSE-encrypted stream."""
        from aiobt.protocol import HANDSHAKE_LENGTH, Handshake

        info_hash = os.urandom(20)
        peer_id_a = os.urandom(20)
        peer_id_b = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(a_reader, a_writer, info_hash),
                    mse_receive(b_reader, b_writer, info_hash),
                )

                # A sends BT handshake
                hs_a = Handshake(info_hash=info_hash, peer_id=peer_id_a)
                result_a.stream.write(hs_a.to_bytes())
                await result_a.stream.drain()

                # B receives and parses BT handshake
                hs_data = await result_b.stream.readexactly(HANDSHAKE_LENGTH)
                hs_parsed = Handshake.from_bytes(hs_data)
                self.assertEqual(hs_parsed.info_hash, info_hash)
                self.assertEqual(hs_parsed.peer_id, peer_id_a)

                # B sends BT handshake back
                hs_b = Handshake(info_hash=info_hash, peer_id=peer_id_b)
                result_b.stream.write(hs_b.to_bytes())
                await result_b.stream.drain()

                # A receives and parses
                hs_data2 = await result_a.stream.readexactly(HANDSHAKE_LENGTH)
                hs_parsed2 = Handshake.from_bytes(hs_data2)
                self.assertEqual(hs_parsed2.info_hash, info_hash)
                self.assertEqual(hs_parsed2.peer_id, peer_id_b)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())

    def test_encrypted_piece_message(self) -> None:
        """BT Piece message survives MSE encryption roundtrip."""
        from aiobt.protocol import Piece, Request, parse_message

        info_hash = os.urandom(20)

        async def run() -> None:
            a_reader, a_writer, b_reader, b_writer = await _create_tcp_pipe()
            try:
                result_a, result_b = await asyncio.gather(
                    mse_initiate(a_reader, a_writer, info_hash),
                    mse_receive(b_reader, b_writer, info_hash),
                )

                # A sends a Request
                req = Request(index=42, begin=0, length=16384)
                req_bytes = req.to_bytes()
                result_a.stream.write(req_bytes)
                await result_a.stream.drain()

                # B receives the request
                raw = await result_b.stream.readexactly(len(req_bytes))
                length = struct.unpack("!I", raw[:4])[0]
                msg = parse_message(raw[4 : 4 + length])
                self.assertIsInstance(msg, Request)
                self.assertEqual(msg.index, 42)
                self.assertEqual(msg.begin, 0)
                self.assertEqual(msg.length, 16384)

                # B sends a Piece back
                block_data = os.urandom(16384)
                piece = Piece(index=42, begin=0, block=block_data)
                piece_bytes = piece.to_bytes()
                result_b.stream.write(piece_bytes)
                await result_b.stream.drain()

                # A receives the piece
                raw2 = await result_a.stream.readexactly(len(piece_bytes))
                length2 = struct.unpack("!I", raw2[:4])[0]
                msg2 = parse_message(raw2[4 : 4 + length2])
                self.assertIsInstance(msg2, Piece)
                self.assertEqual(msg2.index, 42)
                self.assertEqual(msg2.begin, 0)
                self.assertEqual(msg2.block, block_data)
            finally:
                _close_writers(a_writer, b_writer)

        asyncio.run(run())


class TestEncryptionPolicy(unittest.TestCase):
    """EncryptionPolicy enum correctness."""

    def test_values(self) -> None:
        self.assertEqual(EncryptionPolicy.DISABLED.value, "disabled")
        self.assertEqual(EncryptionPolicy.PREFERRED.value, "preferred")
        self.assertEqual(EncryptionPolicy.FORCED.value, "forced")

    def test_from_string(self) -> None:
        self.assertEqual(EncryptionPolicy("disabled"), EncryptionPolicy.DISABLED)
        self.assertEqual(EncryptionPolicy("preferred"), EncryptionPolicy.PREFERRED)
        self.assertEqual(EncryptionPolicy("forced"), EncryptionPolicy.FORCED)
