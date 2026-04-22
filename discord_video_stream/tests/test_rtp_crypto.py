"""
Tests for RTP serialization and transport encryption.

Validates:
- RTP header byte layout matches voice-recv's parsing expectations
- Encrypt/decrypt round-trip with pynacl
- Sequence/timestamp wrap-around behavior
- Marker bit manipulation
- RTCP SR construction
"""

import sys
import os
import struct
import unittest

# Add parent dir for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from rtp.serialize import build_rtp_header, build_rtp_packet, set_marker, build_rtcp_sr
from rtp.crypto import TransportEncryptor

try:
    import nacl.secret
    HAS_NACL = True
except ImportError:
    HAS_NACL = False


class TestRTPHeader(unittest.TestCase):
    """Test RTP header construction against known byte patterns."""

    def test_basic_header_layout(self):
        """Verify the 12-byte header matches the expected wire format."""
        header = build_rtp_header(
            sequence=1,
            timestamp=960,
            ssrc=12345,
            payload_type=120,
        )
        self.assertEqual(len(header), 12)

        # byte 0: version=2 (bits 7-6), no padding, no extension, cc=0
        self.assertEqual(header[0], 0x80)  # 10000000

        # byte 1: marker=0, payload_type=120
        self.assertEqual(header[1], 120)

        # bytes 2-3: sequence=1 (big-endian)
        seq = struct.unpack('>H', header[2:4])[0]
        self.assertEqual(seq, 1)

        # bytes 4-7: timestamp=960
        ts = struct.unpack('>I', header[4:8])[0]
        self.assertEqual(ts, 960)

        # bytes 8-11: ssrc=12345
        ssrc = struct.unpack('>I', header[8:12])[0]
        self.assertEqual(ssrc, 12345)

    def test_marker_bit(self):
        """Marker bit should be bit 7 of byte 1."""
        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0,
            payload_type=101, marker=True,
        )
        self.assertEqual(header[1] & 0x80, 0x80)  # marker set
        self.assertEqual(header[1] & 0x7F, 101)    # payload type preserved

    def test_extension_bit(self):
        """Extension bit should be bit 4 of byte 0."""
        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0,
            payload_type=101, extension=True,
        )
        self.assertEqual(header[0] & 0x10, 0x10)

    def test_sequence_wrap(self):
        """Sequence wraps at 65536."""
        header = build_rtp_header(
            sequence=65536, timestamp=0, ssrc=0, payload_type=120,
        )
        seq = struct.unpack('>H', header[2:4])[0]
        self.assertEqual(seq, 0)  # 65536 % 65536 = 0

    def test_timestamp_wrap(self):
        """Timestamp wraps at 2^32."""
        header = build_rtp_header(
            sequence=0, timestamp=4294967296, ssrc=0, payload_type=120,
        )
        ts = struct.unpack('>I', header[4:8])[0]
        self.assertEqual(ts, 0)

    def test_compatible_with_voice_recv_parsing(self):
        """Verify header can be parsed by voice-recv's RTPPacket._hstruct format.

        voice-recv uses struct.Struct('>xxHII') which skips first 2 bytes then
        reads sequence (H), timestamp (I), ssrc (I) from bytes 2-11.
        """
        header = build_rtp_header(
            sequence=42, timestamp=1440, ssrc=99999, payload_type=101,
            marker=True,
        )
        # Parse using voice-recv's struct format
        hstruct = struct.Struct('>xxHII')
        seq, ts, ssrc = hstruct.unpack_from(header)
        self.assertEqual(seq, 42)
        self.assertEqual(ts, 1440)
        self.assertEqual(ssrc, 99999)


class TestRTPPacket(unittest.TestCase):
    """Test complete RTP packet assembly."""

    def test_build_packet(self):
        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0, payload_type=120,
        )
        payload = b'\xf8\xff\xfe'  # opus silence
        packet = build_rtp_packet(header, payload)
        self.assertEqual(len(packet), 12 + 3)
        self.assertEqual(packet[:12], header)
        self.assertEqual(packet[12:], payload)

    def test_set_marker(self):
        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0, payload_type=101,
        )
        packet = build_rtp_packet(header, b'\x00')

        # Clear marker
        set_marker(packet, False)
        self.assertEqual(packet[1] & 0x80, 0)

        # Set marker
        set_marker(packet, True)
        self.assertEqual(packet[1] & 0x80, 0x80)


class TestRTCPSR(unittest.TestCase):
    """Test RTCP Sender Report construction."""

    def test_sr_size(self):
        sr = build_rtcp_sr(
            ssrc=1, ntp_timestamp=0, rtp_timestamp=0,
            packet_count=0, octet_count=0,
        )
        # Header (8 bytes) + sender info (20 bytes) = 28 bytes
        self.assertEqual(len(sr), 28)

    def test_sr_type(self):
        sr = build_rtcp_sr(
            ssrc=1, ntp_timestamp=0, rtp_timestamp=0,
            packet_count=0, octet_count=0,
        )
        # Byte 1 should be 200 (SR type)
        self.assertEqual(sr[1], 200)

    def test_sr_version(self):
        sr = build_rtcp_sr(
            ssrc=1, ntp_timestamp=0, rtp_timestamp=0,
            packet_count=0, octet_count=0,
        )
        # Byte 0 upper 2 bits should be 2 (version)
        self.assertEqual(sr[0] >> 6, 2)


@unittest.skipUnless(HAS_NACL, "pynacl not installed")
class TestTransportEncryption(unittest.TestCase):
    """Test transport encryption with encrypt/decrypt round-trip."""

    def test_round_trip_xchacha20(self):
        """Encrypt with our TransportEncryptor, decrypt with nacl directly."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')

        header = build_rtp_header(
            sequence=1, timestamp=960, ssrc=12345, payload_type=101,
        )
        payload = b'\x00\x00\x01\x65'  # fake IDR NALU

        encrypted = encryptor.encrypt_rtp(bytes(header), payload)
        self.assertNotEqual(encrypted, payload)

        # Decrypt manually using nacl
        # The encrypted format is: [nacl_ciphertext (includes auth tag)][4-byte nonce counter]
        nonce_counter_bytes = encrypted[-4:]
        nacl_ciphertext = encrypted[:-4]

        # Reconstruct the nonce from the counter
        nonce = bytearray(24)
        nonce[:4] = nonce_counter_bytes

        # nacl's encrypt returns ciphertext+tag, which is what we stored
        box = nacl.secret.Aead(secret_key)
        decrypted = box.decrypt(bytes(nacl_ciphertext), bytes(header), bytes(nonce))
        self.assertEqual(decrypted, payload)

    def test_sequential_nonces(self):
        """Each packet should get a unique incrementing nonce."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')

        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0, payload_type=120,
        )
        payload = b'\xf8\xff\xfe'

        # Encrypt two packets
        enc1 = encryptor.encrypt_rtp(bytes(header), payload)
        enc2 = encryptor.encrypt_rtp(bytes(header), payload)

        # The nonce counters at the end should differ
        nc1 = struct.unpack('>I', enc1[-4:])[0]
        nc2 = struct.unpack('>I', enc2[-4:])[0]
        self.assertEqual(nc2, nc1 + 1)

    def test_nonce_counter_wraps(self):
        """Nonce counter should wrap at 2^32."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')
        encryptor._nonce_counter = 0xFFFFFFFF  # one before wrap

        header = build_rtp_header(
            sequence=0, timestamp=0, ssrc=0, payload_type=120,
        )
        enc = encryptor.encrypt_rtp(bytes(header), b'\x00')
        nc = struct.unpack('>I', enc[-4:])[0]
        self.assertEqual(nc, 0xFFFFFFFF)

        # Next one should wrap to 0
        enc2 = encryptor.encrypt_rtp(bytes(header), b'\x00')
        nc2 = struct.unpack('>I', enc2[-4:])[0]
        self.assertEqual(nc2, 0)

    def test_aad_authenticates_header(self):
        """Different headers should produce different ciphertext (AAD matters)."""
        secret_key = nacl.utils.random(32)
        encryptor1 = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')
        encryptor2 = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')

        header1 = build_rtp_header(sequence=1, timestamp=0, ssrc=0, payload_type=120)
        header2 = build_rtp_header(sequence=2, timestamp=0, ssrc=0, payload_type=120)
        payload = b'\x00\x01\x02'

        # Both start at nonce_counter=0, so they'd encrypt identically
        # if AAD didn't matter. But AAD (header) differs, so output differs.
        enc1 = encryptor1.encrypt_rtp(bytes(header1), payload)
        enc2 = encryptor2.encrypt_rtp(bytes(header2), payload)

        # Ciphertext portions (excluding nonce counter) should differ
        self.assertNotEqual(enc1[:-4], enc2[:-4])

    def test_update_secret_key(self):
        """After key update, old key should fail to decrypt."""
        key1 = nacl.utils.random(32)
        key2 = nacl.utils.random(32)
        encryptor = TransportEncryptor(key1, 'aead_xchacha20_poly1305_rtpsize')

        header = build_rtp_header(sequence=0, timestamp=0, ssrc=0, payload_type=120)
        enc = encryptor.encrypt_rtp(bytes(header), b'\x01\x02\x03')

        # Update key
        encryptor.update_secret_key(key2)

        # Encrypt another packet with new key
        enc2 = encryptor.encrypt_rtp(bytes(header), b'\x04\x05\x06')

        # Decryption with key1 should fail for enc2
        from nacl.exceptions import CryptoError
        box = nacl.secret.Aead(key1)
        nc_bytes = enc2[-4:]
        nonce = bytearray(24)
        nonce[:4] = nc_bytes
        with self.assertRaises(CryptoError):
            box.decrypt(bytes(enc2[:-4]), bytes(header), bytes(nonce))

    def test_unsupported_mode_raises(self):
        """Should raise NotImplementedError for unsupported modes."""
        with self.assertRaises(NotImplementedError):
            TransportEncryptor(b'\x00' * 32, 'xsalsa20_poly1305')


class TestFullPacketBuild(unittest.TestCase):
    """Integration test: build packet + encrypt, verify structure."""

    @unittest.skipUnless(HAS_NACL, "pynacl not installed")
    def test_video_packet_structure(self):
        """Simulate building a video RTP packet as the send pipeline would."""
        secret_key = nacl.utils.random(32)
        encryptor = TransportEncryptor(secret_key, 'aead_xchacha20_poly1305_rtpsize')

        # Build a fake H.264 single-NALU packet
        nalu = bytes([0x65, 0x88, 0x84, 0x00])  # IDR NALU, tiny
        header = build_rtp_header(
            sequence=1, timestamp=3000, ssrc=55555,
            payload_type=101, marker=True,
        )
        packet = build_rtp_packet(header, nalu)

        # The packet on the wire before encryption: header + NALU
        self.assertEqual(len(packet), 16)  # 12 + 4

        # Encrypt just the payload (standard flow)
        encrypted = encryptor.encrypt_rtp(bytes(header), nalu)

        # Final wire packet: header (plaintext) + encrypted payload + nonce
        wire_packet = bytes(header) + encrypted
        self.assertGreater(len(wire_packet), 16)  # encrypted is larger


if __name__ == '__main__':
    unittest.main()
