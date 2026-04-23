#!/usr/bin/env python3
"""
Dry-run test: verify all dev tooling works without a live Discord connection.

Tests:
1. aspectlib weave/rollback
2. structlog structured logging
3. rich output formatting
4. IPC debug server
5. RTP contract validation
6. Hypothesis property-based tests
7. Import all discord_video_stream modules

Run: python3 dev/dry_run.py
"""

import sys
import os
import json
import time
import struct
import asyncio
import threading

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("DRY RUN: Dev Tooling Validation")
print("=" * 60)

errors = []
passed = []

def check(name, fn):
    try:
        fn()
        passed.append(name)
        print(f"  PASS  {name}")
    except Exception as e:
        errors.append((name, str(e)))
        print(f"  FAIL  {name}: {e}")

# ─────────────────────────────────────────
# 1. Import checks
# ─────────────────────────────────────────
print("\n1. Import Checks")

def test_import_aspectlib():
    import aspectlib
    assert hasattr(aspectlib, 'weave')
    assert hasattr(aspectlib, 'aspect')

def test_import_structlog():
    import structlog
    log = structlog.get_logger()
    assert log is not None

def test_import_rich():
    from rich.console import Console
    c = Console()
    assert c is not None

def test_import_hypothesis():
    from hypothesis import given, strategies as st
    assert st is not None

def test_import_discord_video_stream():
    import discord_video_stream as dvs
    assert hasattr(dvs, 'VideoStreamer')
    assert hasattr(dvs, 'StreamConnection')
    assert hasattr(dvs, 'VideoSender')
    assert hasattr(dvs, 'build_rtp_header')
    assert hasattr(dvs, 'TransportEncryptor')
    assert hasattr(dvs, 'H264Packetizer')
    assert hasattr(dvs, 'split_nalu')
    assert hasattr(dvs, 'rewrite_sps_vui')
    assert hasattr(dvs, 'FramePacer')
    assert hasattr(dvs, 'FFmpegProcess')
    assert hasattr(dvs, 'Demuxer')

def test_import_protocol():
    from discord_video_stream.protocol.types import (
        CodecConfig, CODEC_OPUS, CODEC_H264, ALL_CODECS,
        STREAMS_SIMULCAST, SUPPORTED_ENCRYPTION_MODES,
        GatewayOpCodes, VideoAttributes,
        generate_stream_key, parse_stream_key,
        build_video_payload, build_video_off_payload,
    )
    assert len(ALL_CODECS) == 6
    assert CODEC_OPUS.payload_type == 120
    assert CODEC_H264.payload_type == 101

def test_import_rtp():
    from discord_video_stream.rtp.serialize import build_rtp_header, build_rtp_packet, set_marker, build_rtcp_sr
    from discord_video_stream.rtp.h264 import split_nalu, get_nalu_type, H264Packetizer, H264NalUnitTypes
    from discord_video_stream.rtp.crypto import TransportEncryptor

def test_import_media():
    from discord_video_stream.media.ffmpeg import StreamOptions, FFmpegProcess
    from discord_video_stream.media.demux import Demuxer, FrameType, parse_opus_duration
    from discord_video_stream.media.pacer import FramePacer

def test_import_compat():
    from discord_video_stream.compat.voice_recv import VoiceSendRecvClient, HAS_VOICE_RECV
    assert isinstance(HAS_VOICE_RECV, bool)

check("import aspectlib", test_import_aspectlib)
check("import structlog", test_import_structlog)
check("import rich", test_import_rich)
check("import hypothesis", test_import_hypothesis)
check("import discord_video_stream", test_import_discord_video_stream)
check("import protocol", test_import_protocol)
check("import rtp", test_import_rtp)
check("import media", test_import_media)
check("import compat", test_import_compat)

# ─────────────────────────────────────────
# 2. aspectlib Weave / Rollback
# ─────────────────────────────────────────
print("\n2. aspectlib Weave / Rollback")

def test_aspectlib_weave():
    import aspectlib
    from discord_video_stream.rtp.serialize import build_rtp_header as orig_build

    called = []
    @aspectlib.Aspect(bind=True)
    def trace(cutpoint, *args, **kwargs):
        called.append(cutpoint.__name__)
        result = yield aspectlib.Proceed
        yield aspectlib.Return(result)

    # Test weave on a copy, don't break the original
    import types
    test_build = types.FunctionType(
        orig_build.__code__, orig_build.__globals__, 'test_build',
        orig_build.__defaults__, orig_build.__closure__
    )
    # Can't easily weave a standalone function, so test with a class instead
    from discord_video_stream.media.pacer import FramePacer
    aspectlib.weave(FramePacer, trace)
    p = FramePacer(clock_rate=90000)
    p.reset_timing()
    assert "reset_timing" in called
    aspectlib.weave(FramePacer, aspectlib.Rollback)

check("aspectlib weave/rollback", test_aspectlib_weave)

def test_aspectlib_class_weave():
    import aspectlib
    from discord_video_stream.media.pacer import FramePacer

    called = []
    @aspectlib.Aspect(bind=True)
    def trace(cutpoint, *args, **kwargs):
        called.append(cutpoint.__name__)
        result = yield aspectlib.Proceed
        yield aspectlib.Return(result)

    aspectlib.weave(FramePacer, trace)
    p = FramePacer(clock_rate=90000)
    p.reset_timing()
    assert "reset_timing" in called

    aspectlib.weave(FramePacer, aspectlib.Rollback)

check("aspectlib class weave", test_aspectlib_class_weave)

# ─────────────────────────────────────────
# 3. structlog Output
# ─────────────────────────────────────────
print("\n3. structlog Output")

def test_structlog_bind():
    import structlog
    log = structlog.get_logger().bind(ssrc=2000, opcode=12)
    log.info("test_event", packets=5, pts_ms=100.0)
    # If it doesn't raise, it works

check("structlog bind", test_structlog_bind)

# ─────────────────────────────────────────
# 4. Rich Console
# ─────────────────────────────────────────
print("\n4. Rich Console")

def test_rich_table():
    from rich.console import Console
    from rich.table import Table
    c = Console(file=open(os.devnull, 'w'))
    t = Table(title="Test")
    t.add_column("Col1")
    t.add_row("Value1")
    c.print(t)

check("rich table", test_rich_table)

# ─────────────────────────────────────────
# 5. RTP Contract Validation
# ─────────────────────────────────────────
print("\n5. RTP Contract Validation")

def test_rtp_header_version():
    from discord_video_stream.rtp.serialize import build_rtp_header
    header = build_rtp_header(1, 960, 12345, 120)
    assert header[0] >> 6 == 2, "RTP version must be 2"

check("RTP version check", test_rtp_header_version)

def test_rtp_seq_range():
    from discord_video_stream.rtp.serialize import build_rtp_header
    for seq in [0, 1, 32768, 65535]:
        header = build_rtp_header(seq, 0, 0, 120)
        parsed = struct.unpack('>H', header[2:4])[0]
        assert parsed == seq, f"seq {seq} != parsed {parsed}"

check("RTP seq range", test_rtp_seq_range)

def test_rtp_marker_bit():
    from discord_video_stream.rtp.serialize import build_rtp_header
    header = build_rtp_header(0, 0, 0, 101, marker=True)
    assert header[1] & 0x80, "marker bit should be set"
    header = build_rtp_header(0, 0, 0, 101, marker=False)
    assert not (header[1] & 0x80), "marker bit should be clear"

check("RTP marker bit", test_rtp_marker_bit)

# ─────────────────────────────────────────
# 6. H.264 NALU Splitting
# ─────────────────────────────────────────
print("\n6. H.264 NALU Processing")

def test_nalu_split():
    from discord_video_stream.rtp.h264 import split_nalu, get_nalu_type, H264NalUnitTypes
    frame = (
        b'\x00\x00\x00\x01\x67' + b'\x42' * 10  # SPS
        + b'\x00\x00\x00\x01\x68' + b'\xce' * 5   # PPS
        + b'\x00\x00\x00\x01\x65' + b'\x88' * 20  # IDR
    )
    nalus = split_nalu(frame)
    assert len(nalus) == 3
    assert get_nalu_type(nalus[0]) == H264NalUnitTypes.SPS
    assert get_nalu_type(nalus[1]) == H264NalUnitTypes.PPS
    assert get_nalu_type(nalus[2]) == H264NalUnitTypes.IDR

check("NALU split", test_nalu_split)

def test_h264_packetizer():
    from discord_video_stream.rtp.h264 import H264Packetizer
    pkt = H264Packetizer(ssrc=12345, payload_type=101)
    pkt.set_timestamp(90000)
    frame = b'\x00\x00\x00\x01\x65' + b'\x88' * 20
    packets = pkt.packetize_frame(frame)
    assert len(packets) >= 1
    assert packets[-1][1] & 0x80, "last packet should have marker"

check("H264 packetizer", test_h264_packetizer)

# ─────────────────────────────────────────
# 7. SPS VUI Rewriter
# ─────────────────────────────────────────
print("\n7. SPS VUI Rewriter")

def test_sps_vui_rewrite():
    from discord_video_stream.protocol.vui import rewrite_sps_vui, BitstreamWriter
    w = BitstreamWriter()
    w.write_unsigned(66, 8); w.write_unsigned(0, 8); w.write_unsigned(30, 8)
    w.write_ue(0); w.write_ue(0); w.write_ue(0); w.write_ue(0)
    w.write_ue(4); w.write_bits(0, 1)
    w.write_ue(21); w.write_ue(17); w.write_bits(1, 1)
    w.write_bits(0, 1); w.write_bits(0, 1)
    w.write_bits(0, 1)  # no VUI
    w.write_bits(1, 1)  # RBSP stop
    w.flush()
    sps = bytes([0x67]) + w.to_bytes()
    result = rewrite_sps_vui(sps)
    assert result[0] == 0x67, "NAL header preserved"
    assert len(result) > len(sps), "VUI should be injected"

check("SPS VUI rewrite", test_sps_vui_rewrite)

# ─────────────────────────────────────────
# 8. Transport Encryption
# ─────────────────────────────────────────
print("\n8. Transport Encryption")

def test_transport_encrypt():
    try:
        import nacl.secret
        from discord_video_stream.rtp.crypto import TransportEncryptor
        from discord_video_stream.rtp.serialize import build_rtp_header

        key = nacl.utils.random(32)
        enc = TransportEncryptor(key, 'aead_xchacha20_poly1305_rtpsize')
        header = build_rtp_header(1, 960, 12345, 101)
        encrypted = enc.encrypt_rtp(bytes(header), b'\x00\x00\x01\x65')
        assert len(encrypted) > 4, "encrypted should be larger than nonce"

        # Decrypt with nacl directly
        nonce_bytes = encrypted[-4:]
        ciphertext = encrypted[:-4]
        nonce = bytearray(24)
        nonce[:4] = nonce_bytes
        box = nacl.secret.Aead(key)
        decrypted = box.decrypt(bytes(ciphertext), bytes(header), bytes(nonce))
        assert decrypted == b'\x00\x00\x01\x65'
    except ImportError:
        raise Exception("pynacl not installed")

check("transport encrypt/decrypt", test_transport_encrypt)

# ─────────────────────────────────────────
# 9. Protocol Types
# ─────────────────────────────────────────
print("\n9. Protocol Types")

def test_stream_key():
    from discord_video_stream.protocol.types import generate_stream_key, parse_stream_key
    key = generate_stream_key('guild', '123', '456', '789')
    assert key == 'guild:123:456:789'
    parsed = parse_stream_key(key)
    assert parsed['guild_id'] == '123'
    assert parsed['channel_id'] == '456'
    assert parsed['user_id'] == '789'

check("stream key roundtrip", test_stream_key)

def test_video_payload():
    from discord_video_stream.protocol.types import build_video_payload, VideoAttributes
    attrs = VideoAttributes(width=1920, height=1080, fps=30)
    payload = build_video_payload(1000, 2000, 2001, attrs)
    assert payload['audio_ssrc'] == 1000
    assert payload['streams'][0]['max_resolution']['width'] == 1920

check("video payload", test_video_payload)

# ─────────────────────────────────────────
# 10. Opus Duration Parsing
# ─────────────────────────────────────────
print("\n10. Opus Duration")

def test_opus_duration():
    from discord_video_stream.media.demux import parse_opus_duration
    # Config 1 (SILK narrowband 20ms): toc = (1 << 3) | 0 = 0x08
    frame = bytes([0x08]) + b'\x00' * 10
    dur = parse_opus_duration(frame)
    assert dur == 960, f"expected 960, got {dur}"

check("opus duration", test_opus_duration)

# ─────────────────────────────────────────
# 11. IPC Debug Server
# ─────────────────────────────────────────
print("\n11. IPC Debug Server")

def test_debug_server():
    from dev.debug_server import DebugServer
    server = DebugServer(path="/tmp/test-dry-debug.sock")
    assert server._path == "/tmp/test-dry-debug.sock"
    assert server._streamer is None
    # Don't actually start the server in dry run

check("debug server init", test_debug_server)

# ─────────────────────────────────────────
# 12. Aspects Module
# ─────────────────────────────────────────
print("\n12. Aspects Module")

def test_aspects_import():
    from dev.aspects import (
        protocol_trace, timing, packet_trace,
        error_enrich, rtp_contract,
        apply_debug_aspects, remove_debug_aspects,
    )
    assert protocol_trace is not None
    assert timing is not None

check("aspects import", test_aspects_import)

def test_aspects_apply_rollback():
    from dev.aspects import apply_debug_aspects, remove_debug_aspects, _woven_targets
    apply_debug_aspects()
    assert len(_woven_targets) > 0, "should have woven at least one target"
    remove_debug_aspects()
    assert len(_woven_targets) == 0, "should have rolled back all"

check("aspects apply/rollback", test_aspects_apply_rollback)

# ─────────────────────────────────────────
# 13. Frame Pacer
# ─────────────────────────────────────────
print("\n13. Frame Pacer")

def test_pacer_init():
    from discord_video_stream.media.pacer import FramePacer
    p = FramePacer(clock_rate=90000)
    assert p.clock_rate == 90000
    assert p.pts is None

check("pacer init", test_pacer_init)

def test_pacer_sync_reject():
    from discord_video_stream.media.pacer import FramePacer
    a = FramePacer(clock_rate=90000)
    b = FramePacer(clock_rate=48000)
    a.sync_partner = b
    try:
        b.sync_partner = a
        assert False, "should have raised ValueError"
    except ValueError:
        pass

check("pacer sync reject circular", test_pacer_sync_reject)

# ─────────────────────────────────────────
# 14. Hypothesis Property Test
# ─────────────────────────────────────────
print("\n14. Hypothesis Property Tests")

def test_hypothesis_rtp():
    from hypothesis import given, strategies as st, settings
    from discord_video_stream.rtp.serialize import build_rtp_header

    @given(
        seq=st.integers(min_value=0, max_value=65535),
        ts=st.integers(min_value=0, max_value=2**32 - 1),
    )
    @settings(max_examples=50)
    def inner(seq, ts):
        header = build_rtp_header(seq, ts, 0, 120)
        parsed_seq = struct.unpack('>H', header[2:4])[0]
        parsed_ts = struct.unpack('>I', header[4:8])[0]
        assert parsed_seq == seq
        assert parsed_ts == ts

    inner()

check("hypothesis RTP roundtrip", test_hypothesis_rtp)

# ─────────────────────────────────────────
# Summary
# ─────────────────────────────────────────
print("\n" + "=" * 60)
print(f"RESULTS: {len(passed)} passed, {len(errors)} failed")
print("=" * 60)

if errors:
    print("\nFailed:")
    for name, err in errors:
        print(f"  - {name}: {err}")
    sys.exit(1)
else:
    print("\nAll checks passed.")
