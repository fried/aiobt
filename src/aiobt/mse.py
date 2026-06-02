"""Message Stream Encryption / Protocol Encryption (MSE/PE).

Implements the MSE/PE protocol used to obfuscate BitTorrent traffic and
prevent ISP deep packet inspection.  The spec originates from Vuze
(formerly Azureus) and is supported by all major clients.

Key exchange uses 768-bit Diffie-Hellman.  Payload encryption uses RC4
with the first 1024 bytes of keystream discarded.  All cryptographic
primitives are implemented in pure Python using only the standard
library.

References
----------
- https://wiki.vuze.com/w/Message_Stream_Encryption
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import os
import random
import struct
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Encryption policy
# ---------------------------------------------------------------------------


class EncryptionPolicy(enum.Enum):
    """Controls whether peer connections use MSE/PE."""

    DISABLED = "disabled"
    """No encryption; reject encrypted handshakes."""

    PREFERRED = "preferred"
    """Offer encryption; fall back to plaintext if the peer declines."""

    FORCED = "forced"
    """Require encryption; reject peers that don't support it."""


# ---------------------------------------------------------------------------
# DH constants (768-bit prime from the MSE spec)
# ---------------------------------------------------------------------------

_DH_P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A63A36210000000000090563",
    16,
)
_DH_G = 2
_DH_PRIVATE_BITS = 160  # 20 bytes


# ---------------------------------------------------------------------------
# Crypto provide / select flags
# ---------------------------------------------------------------------------

CRYPTO_PLAINTEXT = 0x01
CRYPTO_RC4 = 0x02

# ---------------------------------------------------------------------------
# VC (verification constant) — 8 zero bytes
# ---------------------------------------------------------------------------

_VC = b"\x00" * 8


# ---------------------------------------------------------------------------
# Pure-Python RC4
# ---------------------------------------------------------------------------


class RC4:
    """RC4 (ARC4) stream cipher — pure Python implementation.

    After construction the first *discard* bytes of keystream are thrown
    away (the MSE spec requires discarding 1024 bytes).
    """

    __slots__ = ("_S", "_i", "_j")

    def __init__(self, key: bytes, *, discard: int = 0) -> None:
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self._S = S
        self._i = 0
        self._j = 0
        if discard:
            self.process(bytes(discard))

    def process(self, data: bytes) -> bytes:
        """Encrypt or decrypt *data* (XOR with keystream)."""
        S = self._S
        i = self._i
        j = self._j
        out = bytearray(len(data))
        for k in range(len(data)):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
        self._i = i
        self._j = j
        return bytes(out)


# ---------------------------------------------------------------------------
# DH helpers
# ---------------------------------------------------------------------------


def _dh_keypair() -> tuple[int, bytes]:
    """Generate a DH private key and the corresponding public value.

    Returns ``(private_int, public_bytes_96)``.
    """
    xa = int.from_bytes(os.urandom(_DH_PRIVATE_BITS // 8))
    ya = pow(_DH_G, xa, _DH_P)
    return xa, ya.to_bytes(96)


def _dh_secret(their_public: bytes, our_private: int) -> bytes:
    """Derive the shared secret *S* from the peer's DH public value."""
    yb = int.from_bytes(their_public)
    s = pow(yb, our_private, _DH_P)
    return s.to_bytes(96)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def _derive_keys(
    secret: bytes, info_hash: bytes, *, initiator: bool
) -> tuple[RC4, RC4]:
    """Derive RC4 ciphers for encryption and decryption.

    Returns ``(encrypt_rc4, decrypt_rc4)`` with 1024 bytes already
    discarded from each stream.
    """
    key_a = hashlib.sha1(b"keyA" + secret + info_hash).digest()
    key_b = hashlib.sha1(b"keyB" + secret + info_hash).digest()
    if initiator:
        return RC4(key_a, discard=1024), RC4(key_b, discard=1024)
    return RC4(key_b, discard=1024), RC4(key_a, discard=1024)


def _raw_key_bytes(
    secret: bytes, info_hash: bytes, *, initiator: bool
) -> tuple[bytes, bytes]:
    """Return raw ``(enc_key, dec_key)`` without creating RC4 objects."""
    key_a = hashlib.sha1(b"keyA" + secret + info_hash).digest()
    key_b = hashlib.sha1(b"keyB" + secret + info_hash).digest()
    if initiator:
        return key_a, key_b
    return key_b, key_a


# ---------------------------------------------------------------------------
# Encrypted stream wrapper
# ---------------------------------------------------------------------------


class EncryptedStream:
    """Transparent encryption layer over asyncio streams.

    Wraps a :class:`~asyncio.StreamReader` / :class:`~asyncio.StreamWriter`
    pair so that :class:`~aiobt.peer.PeerConnection` can read/write
    normally while the wire bytes are RC4-encrypted.
    """

    __slots__ = ("_reader", "_writer", "_enc", "_dec", "_buffer")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        encrypt: RC4,
        decrypt: RC4,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._enc = encrypt
        self._dec = decrypt
        self._buffer = b""

    # -- reader interface used by PeerConnection ----------------------------

    async def readexactly(self, n: int) -> bytes:
        """Read exactly *n* decrypted bytes."""
        while len(self._buffer) < n:
            raw = await self._reader.read(max(n - len(self._buffer), 4096))
            if not raw:
                raise asyncio.IncompleteReadError(self._buffer, n)
            self._buffer += self._dec.process(raw)
        result = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return result

    async def read(self, n: int) -> bytes:
        """Read up to *n* decrypted bytes."""
        if self._buffer:
            result = self._buffer[:n]
            self._buffer = self._buffer[n:]
            return result
        raw = await self._reader.read(n)
        if not raw:
            return b""
        return self._dec.process(raw)

    # -- writer interface used by PeerConnection ----------------------------

    def write(self, data: bytes) -> None:
        """Encrypt and buffer *data* for sending."""
        self._writer.write(self._enc.process(data))

    async def drain(self) -> None:
        """Flush the underlying transport."""
        await self._writer.drain()

    def close(self) -> None:
        """Close the underlying writer."""
        self._writer.close()

    async def wait_closed(self) -> None:
        """Wait for the underlying writer to close."""
        await self._writer.wait_closed()

    def is_closing(self) -> bool:
        """Return *True* if the underlying writer is closing."""
        return self._writer.is_closing()

    def get_extra_info(self, name: str, default: object = None) -> object:
        """Proxy to the underlying writer's ``get_extra_info``."""
        return self._writer.get_extra_info(name, default)


# ---------------------------------------------------------------------------
# Plaintext stream wrapper (no-op, same interface)
# ---------------------------------------------------------------------------


class PlaintextStream:
    """Pass-through wrapper matching :class:`EncryptedStream` interface.

    Used when MSE negotiates plaintext (crypto_select = 0x01) so that
    the rest of the code can use a uniform stream interface.
    """

    __slots__ = ("_reader", "_writer")

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def readexactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def read(self, n: int) -> bytes:
        return await self._reader.read(n)

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    def get_extra_info(self, name: str, default: object = None) -> object:
        return self._writer.get_extra_info(name, default)


# ---------------------------------------------------------------------------
# Stream type
# ---------------------------------------------------------------------------

type MSEStream = EncryptedStream | PlaintextStream


# ---------------------------------------------------------------------------
# MSE handshake result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MSEHandshakeResult:
    """Outcome of an MSE/PE handshake."""

    stream: MSEStream
    """The negotiated stream (encrypted or plaintext)."""

    encrypted: bool
    """True if RC4 encryption was selected."""

    info_hash: bytes | None = None
    """The info_hash selected by the receiver (receiver side only)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_pad() -> bytes:
    """Return 0–512 random bytes of padding per the MSE spec."""
    return os.urandom(random.SystemRandom().randint(0, 512))


# ---------------------------------------------------------------------------
# MSE initiator handshake (outgoing connection — "A")
# ---------------------------------------------------------------------------


async def mse_initiate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    info_hash: bytes,
    *,
    policy: EncryptionPolicy = EncryptionPolicy.PREFERRED,
    timeout: float = 10.0,
) -> MSEHandshakeResult:
    """Perform the MSE/PE handshake as the initiator (side A).

    Parameters
    ----------
    reader, writer:
        The raw TCP streams.
    info_hash:
        The 20-byte info hash (used as SKEY).
    policy:
        Encryption policy governing crypto_provide flags.
    timeout:
        Seconds before the handshake is abandoned.

    Returns
    -------
    MSEHandshakeResult
        Contains the negotiated stream and whether RC4 was selected.

    Raises
    ------
    ValueError
        If the peer's crypto_select is incompatible with our policy.
    asyncio.TimeoutError
        If the handshake exceeds *timeout*.
    """
    return await asyncio.wait_for(
        _mse_initiate_inner(reader, writer, info_hash, policy),
        timeout=timeout,
    )


async def _mse_initiate_inner(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    info_hash: bytes,
    policy: EncryptionPolicy,
) -> MSEHandshakeResult:
    # ── Step 1: DH key exchange ──────────────────────────────────────
    xa, ya_bytes = _dh_keypair()
    pad_a = _random_pad()
    writer.write(ya_bytes + pad_a)
    await writer.drain()

    # Read B's DH public key (always exactly 96 bytes at the start)
    yb_bytes = await reader.readexactly(96)
    secret = _dh_secret(yb_bytes, xa)

    # ── Step 2: Build and send A's crypto block ──────────────────────
    req1 = hashlib.sha1(b"req1" + secret).digest()
    req2 = hashlib.sha1(b"req2" + info_hash).digest()
    req3 = hashlib.sha1(b"req3" + secret).digest()
    obfuscated_hash = bytes(a ^ b for a, b in zip(req2, req3))

    if policy == EncryptionPolicy.FORCED:
        crypto_provide = CRYPTO_RC4
    elif policy == EncryptionPolicy.DISABLED:
        crypto_provide = CRYPTO_PLAINTEXT
    else:
        crypto_provide = CRYPTO_PLAINTEXT | CRYPTO_RC4

    enc, dec = _derive_keys(secret, info_hash, initiator=True)

    # ENCRYPT(VC + crypto_provide + len(padC) + padC + len(IA))
    pad_c = _random_pad()
    encrypted_block = enc.process(
        _VC
        + struct.pack("!I", crypto_provide)
        + struct.pack("!H", len(pad_c))
        + pad_c
        + struct.pack("!H", 0)  # IA length = 0, BT handshake comes later
    )

    writer.write(req1 + obfuscated_hash + encrypted_block)
    await writer.drain()

    # ── Step 3: Read B's response ────────────────────────────────────
    # B sent: Yb (96, already read) + padB (0-512, plaintext)
    #         + ENCRYPT(VC + crypto_select + len(padD) + padD)
    #
    # We need to find where ENCRYPT(VC) starts.  Since padB is
    # plaintext and the RC4 stream starts at VC, we can pre-compute
    # what ENCRYPT(VC) looks like on the wire: it's the first 8 bytes
    # of the decrypt keystream XOR'd with 8 zero bytes = the keystream
    # itself.
    temp_dec = _derive_keys(secret, info_hash, initiator=True)[1]
    expected_vc_wire = temp_dec.process(_VC)  # keystream[:8]

    # Scan for the expected VC bytes in the stream after Yb.
    # padB is 0-512 bytes, so we need at most 512 + 8 bytes.
    scan_buf = b""
    vc_pos = -1
    while vc_pos < 0:
        chunk = await reader.read(1024)
        if not chunk:
            raise ConnectionError("peer closed during MSE handshake")
        scan_buf += chunk
        vc_pos = scan_buf.find(expected_vc_wire)
        if len(scan_buf) > 600 and vc_pos < 0:
            raise ConnectionError("MSE: VC not found in receiver response")

    # Everything from vc_pos onward is the encrypted block.
    # Create a fresh decrypt cipher and process from the encrypted start.
    _, dec = _derive_keys(secret, info_hash, initiator=True)
    encrypted_data = scan_buf[vc_pos:]

    # Decrypt VC (8) + crypto_select (4) + pad_d_len (2) = 14 bytes minimum
    while len(encrypted_data) < 14:
        chunk = await reader.read(14 - len(encrypted_data))
        if not chunk:
            raise ConnectionError("peer closed during MSE handshake")
        encrypted_data += chunk

    decrypted = dec.process(encrypted_data[:14])
    encrypted_data = encrypted_data[14:]

    vc_check = decrypted[:8]
    if vc_check != _VC:
        raise ConnectionError("MSE: VC verification failed")

    (crypto_select,) = struct.unpack("!I", decrypted[8:12])
    (pad_d_len,) = struct.unpack("!H", decrypted[12:14])

    # Read and discard padD
    if pad_d_len > 0:
        while len(encrypted_data) < pad_d_len:
            chunk = await reader.read(pad_d_len - len(encrypted_data))
            if not chunk:
                raise ConnectionError("peer closed during MSE handshake")
            encrypted_data += chunk
        dec.process(encrypted_data[:pad_d_len])  # consume padD
        encrypted_data = encrypted_data[pad_d_len:]

    # Any leftover bytes are the start of the encrypted payload stream
    leftover = b""
    if encrypted_data:
        leftover = dec.process(encrypted_data)

    # ── Step 4: Build result stream ──────────────────────────────────
    if crypto_select == CRYPTO_RC4:
        if policy == EncryptionPolicy.DISABLED:
            raise ValueError("peer selected RC4 but encryption is disabled")
        enc_stream = EncryptedStream(reader, writer, enc, dec)
        if leftover:
            enc_stream._buffer = leftover
        return MSEHandshakeResult(stream=enc_stream, encrypted=True)
    elif crypto_select == CRYPTO_PLAINTEXT:
        if policy == EncryptionPolicy.FORCED:
            raise ValueError("peer selected plaintext but encryption is forced")
        return MSEHandshakeResult(
            stream=PlaintextStream(reader, writer), encrypted=False
        )
    else:
        raise ValueError(f"unknown crypto_select: {crypto_select:#x}")


# ---------------------------------------------------------------------------
# MSE receiver handshake (incoming connection — "B")
# ---------------------------------------------------------------------------


async def mse_receive(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    info_hash_lookup: dict[bytes, bytes] | bytes,
    *,
    policy: EncryptionPolicy = EncryptionPolicy.PREFERRED,
    timeout: float = 10.0,
) -> MSEHandshakeResult:
    """Perform the MSE/PE handshake as the receiver (side B).

    Parameters
    ----------
    reader, writer:
        The raw TCP streams.
    info_hash_lookup:
        Either a single 20-byte info_hash (SKEY), or a dict mapping
        ``{info_hash: info_hash}`` for multi-torrent servers that need
        to identify which torrent the initiator wants.
    policy:
        Encryption policy governing crypto_select.
    timeout:
        Seconds before the handshake is abandoned.

    Returns
    -------
    MSEHandshakeResult
        Contains the negotiated stream, whether RC4 was selected,
        and the matched info_hash.
    """
    return await asyncio.wait_for(
        _mse_receive_inner(reader, writer, info_hash_lookup, policy),
        timeout=timeout,
    )


async def _mse_receive_inner(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    info_hash_lookup: dict[bytes, bytes] | bytes,
    policy: EncryptionPolicy,
) -> MSEHandshakeResult:
    # Normalize info_hash_lookup
    if isinstance(info_hash_lookup, bytes):
        hashes: dict[bytes, bytes] = {info_hash_lookup: info_hash_lookup}
    else:
        hashes = info_hash_lookup

    # ── Step 1: Read A's DH public key (96 bytes) ───────────────────
    ya_bytes = await reader.readexactly(96)

    # ── Step 2: DH key exchange — send Yb + padB ────────────────────
    xb, yb_bytes = _dh_keypair()
    pad_b = _random_pad()
    writer.write(yb_bytes + pad_b)
    await writer.drain()

    secret = _dh_secret(ya_bytes, xb)

    # ── Step 3: Find HASH('req1', S) in A's stream ──────────────────
    # A sent: Ya (96, already read) + padA (0-512, plaintext)
    #         + HASH('req1', S) (20) + obfuscated_hash (20) + encrypted_block
    req1_expected = hashlib.sha1(b"req1" + secret).digest()

    scan_buf = b""
    req1_pos = -1
    while req1_pos < 0 or len(scan_buf) < req1_pos + 40:
        chunk = await reader.read(1024)
        if not chunk:
            raise ConnectionError("peer closed during MSE handshake")
        scan_buf += chunk
        if req1_pos < 0:
            req1_pos = scan_buf.find(req1_expected)
        if len(scan_buf) > 700 and req1_pos < 0:
            raise ConnectionError("MSE: HASH('req1', S) not found")

    obfuscated_hash = scan_buf[req1_pos + 20 : req1_pos + 40]

    # ── Step 4: Identify info_hash from obfuscated hash ──────────────
    req3 = hashlib.sha1(b"req3" + secret).digest()
    matched_hash: bytes | None = None
    for ih in hashes:
        req2 = hashlib.sha1(b"req2" + ih).digest()
        expected = bytes(a ^ b for a, b in zip(req2, req3))
        if expected == obfuscated_hash:
            matched_hash = ih
            break

    if matched_hash is None:
        raise ConnectionError("MSE: no matching info_hash found")

    # ── Step 5: Derive keys and decrypt A's crypto block ─────────────
    enc, dec = _derive_keys(secret, matched_hash, initiator=False)

    encrypted_data = scan_buf[req1_pos + 40 :]

    # Need: VC(8) + crypto_provide(4) + pad_c_len(2) = 14 bytes min
    while len(encrypted_data) < 14:
        chunk = await reader.read(14 - len(encrypted_data))
        if not chunk:
            raise ConnectionError("peer closed during MSE handshake")
        encrypted_data += chunk

    decrypted = dec.process(encrypted_data[:14])
    encrypted_data = encrypted_data[14:]

    vc = decrypted[:8]
    if vc != _VC:
        raise ConnectionError("MSE: VC verification failed")

    (crypto_provide,) = struct.unpack("!I", decrypted[8:12])
    (pad_c_len,) = struct.unpack("!H", decrypted[12:14])

    # Read padC + IA_len(2)
    need = pad_c_len + 2
    while len(encrypted_data) < need:
        chunk = await reader.read(need - len(encrypted_data))
        if not chunk:
            raise ConnectionError("peer closed during MSE handshake")
        encrypted_data += chunk

    rest = dec.process(encrypted_data[: pad_c_len + 2])
    encrypted_data = encrypted_data[pad_c_len + 2 :]

    (ia_len,) = struct.unpack("!H", rest[pad_c_len : pad_c_len + 2])

    # Read IA (initial payload from A)
    if ia_len > 0:
        while len(encrypted_data) < ia_len:
            chunk = await reader.read(ia_len - len(encrypted_data))
            if not chunk:
                raise ConnectionError("peer closed during MSE handshake")
            encrypted_data += chunk
        dec.process(encrypted_data[:ia_len])  # consume IA
        encrypted_data = encrypted_data[ia_len:]

    leftover = b""
    if encrypted_data:
        leftover = dec.process(encrypted_data)

    # ── Step 6: Select crypto method and respond ─────────────────────
    if policy == EncryptionPolicy.FORCED:
        if not (crypto_provide & CRYPTO_RC4):
            raise ValueError("peer only offers plaintext but encryption is forced")
        crypto_select = CRYPTO_RC4
    elif policy == EncryptionPolicy.DISABLED:
        if not (crypto_provide & CRYPTO_PLAINTEXT):
            raise ValueError("peer only offers RC4 but encryption is disabled")
        crypto_select = CRYPTO_PLAINTEXT
    else:
        # PREFERRED: pick RC4 if available, else plaintext
        if crypto_provide & CRYPTO_RC4:
            crypto_select = CRYPTO_RC4
        elif crypto_provide & CRYPTO_PLAINTEXT:
            crypto_select = CRYPTO_PLAINTEXT
        else:
            raise ValueError(f"no acceptable crypto method: {crypto_provide:#x}")

    # Send: ENCRYPT(VC + crypto_select + len(padD) + padD)
    pad_d = _random_pad()
    response = enc.process(
        _VC + struct.pack("!I", crypto_select) + struct.pack("!H", len(pad_d)) + pad_d
    )
    writer.write(response)
    await writer.drain()

    # ── Step 7: Build result stream ──────────────────────────────────
    if crypto_select == CRYPTO_RC4:
        enc_stream = EncryptedStream(reader, writer, enc, dec)
        if leftover:
            enc_stream._buffer = leftover
        return MSEHandshakeResult(
            stream=enc_stream, encrypted=True, info_hash=matched_hash
        )
    else:
        return MSEHandshakeResult(
            stream=PlaintextStream(reader, writer),
            encrypted=False,
            info_hash=matched_hash,
        )
