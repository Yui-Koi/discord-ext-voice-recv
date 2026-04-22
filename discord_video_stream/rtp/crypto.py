"""
Transport encryption for Discord voice/video RTP packets.

Mirrors discord-ext-voice-recv's reader.py PacketDecryptor (reverse direction)
and discord.py's VoiceClient._encrypt_aead_xchacha20_poly1305_rtpsize.

The encryption operates on the RTP payload with the header as AAD:
    output = [header][encrypted_payload][4-byte nonce_counter]

For aead_xchacha20_poly1305_rtpsize (the current standard mode):
    - AEAD: nacl.secret.Aead(secret_key)
    - Nonce: 4-byte BE counter + 20 zero bytes (24 total for xchacha20)
    - AAD: RTP header bytes
    - Output append: first 4 bytes of nonce (the counter value)
"""

from __future__ import annotations

import struct

try:
    import nacl.secret
    from nacl.exceptions import CryptoError
except ImportError as e:
    raise RuntimeError("pynacl is required") from e

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Union

__all__ = [
    'TransportEncryptor',
]


class TransportEncryptor:
    """Encrypts RTP packets for UDP transport to Discord's voice server.

    Currently supports aead_xchacha20_poly1305_rtpsize (the preferred and
    required mode since DAVE became mandatory).

    Usage:
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')
        encrypted = encryptor.encrypt_rtp(header_bytes, payload_bytes)
        # sendto: header + encrypted
    """

    SUPPORTED_MODES = [
        'aead_xchacha20_poly1305_rtpsize',
        'aead_aes256_gcm_rtpsize',
    ]

    def __init__(self, secret_key: bytes, mode: str) -> None:
        if mode not in self.SUPPORTED_MODES:
            raise NotImplementedError(f"Unsupported encryption mode: {mode}")

        self.mode = mode
        self._nonce_counter = 0
        self._box: Union[nacl.secret.Aead, None] = None

        if mode == 'aead_xchacha20_poly1305_rtpsize':
            self._box = nacl.secret.Aead(secret_key)
        elif mode == 'aead_aes256_gcm_rtpsize':
            # AES-256-GCM uses 12-byte nonce (4 counter + 8 zero padding)
            # Falls back to nacl Aead if available, otherwise needs cryptography lib
            self._box = nacl.secret.Aead(secret_key)

    def update_secret_key(self, secret_key: bytes) -> None:
        """Update the encryption key (e.g. after DAVE rekey)."""
        self._box = nacl.secret.Aead(secret_key)

    def encrypt_rtp(self, header: bytes, payload: bytes) -> bytes:
        """Encrypt an RTP payload using AEAD with the header as AAD.

        Parameters
        ----------
        header : bytes
            The 12-byte RTP header (used as Additional Authenticated Data).
        payload : bytes
            The RTP payload to encrypt.

        Returns
        -------
        bytes
            Encrypted payload + 4-byte nonce counter appended.
        """
        # Save current counter value BEFORE incrementing
        used_counter = self._nonce_counter
        nonce = self._make_nonce()
        assert isinstance(self._box, nacl.secret.Aead)
        # Aead.encrypt() returns EncryptedMessage = [nonce][ciphertext+tag]
        # We only want the ciphertext+tag portion (the nonce is redundant since
        # we derive it from the appended counter)
        encrypted = self._box.encrypt(bytes(payload), bytes(header), bytes(nonce))
        return encrypted.ciphertext + struct.pack('>I', used_counter)

    def _make_nonce(self) -> bytes:
        """Generate a 24-byte nonce for xchacha20-poly1305.

        First 4 bytes = incremental counter (BE uint32).
        Remaining 20 bytes = zero padding.
        """
        nonce = struct.pack('>I', self._nonce_counter)
        self._nonce_counter = (self._nonce_counter + 1) & 0xFFFFFFFF
        return nonce + b'\x00' * 20

    @property
    def nonce_counter(self) -> int:
        """Current nonce counter value."""
        return self._nonce_counter
