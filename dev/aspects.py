"""
AOP aspects for discord_video_stream development.

Import and call apply_debug_aspects() to weave all debugging
instrumentation into the codebase. Call remove_debug_aspects()
to roll back all instrumentation.

Usage:
    from dev.aspects import apply_debug_aspects, remove_debug_aspects
    apply_debug_aspects()   # weave all aspects
    # ... debug ...
    remove_debug_aspects()  # roll back everything
"""

from __future__ import annotations

import time
import struct
import logging
import aspectlib
import structlog

log = structlog.get_logger()

# Track all woven targets for rollback
_woven_targets: list = []

# ──────────────────────────────────────────────
# 1. Protocol Call Tracing
# ──────────────────────────────────────────────

@aspectlib.Aspect(bind=True)
def protocol_trace(cutpoint, *args, **kwargs):
    """Trace all voice WebSocket protocol method calls."""
    method = cutpoint.__name__ if cutpoint else "unknown"
    log.debug("protocol_call", method=method, arg_count=len(args))
    result = yield aspectlib.Proceed
    log.debug("protocol_done", method=method)
    yield aspectlib.Return(result)


# ──────────────────────────────────────────────
# 2. Timing / Slow Call Detection
# ──────────────────────────────────────────────

@aspectlib.Aspect(bind=True)
def timing(cutpoint, *args, **kwargs):
    """Measure execution time. Logs warning for calls >5ms."""
    method = cutpoint.__name__ if cutpoint else "unknown"
    start = time.perf_counter()
    result = yield aspectlib.Proceed
    elapsed_ms = (time.perf_counter() - start) * 1000
    if elapsed_ms > 5.0:
        log.warning("slow_call", method=method, elapsed_ms=round(elapsed_ms, 2))
    else:
        log.debug("timing", method=method, elapsed_ms=round(elapsed_ms, 2))
    yield aspectlib.Return(result)


# ──────────────────────────────────────────────
# 3. Packet Lifecycle Tracing
# ──────────────────────────────────────────────

@aspectlib.Aspect(bind=True)
def packet_trace(cutpoint, *args, **kwargs):
    """Trace RTP packet build -> encrypt -> send pipeline."""
    method = cutpoint.__name__ if cutpoint else "unknown"
    if method == "build_rtp_header" and len(args) >= 3:
        seq, ts, ssrc = args[0], args[1], args[2]
        log.debug("rtp_header_built", seq=seq, ts=ts, ssrc=ssrc)
    elif method == "encrypt_rtp" and len(args) >= 2:
        payload_size = len(args[1]) if args[1] else 0
        log.debug("rtp_encrypting", payload_size=payload_size)
    result = yield aspectlib.Proceed
    if method == "send_frame":
        log.debug("frame_sent", packet_count=result)
    elif method == "encrypt_rtp":
        log.debug("rtp_encrypted", output_size=len(result) if result else 0)
    yield aspectlib.Return(result)


# ──────────────────────────────────────────────
# 4. Error Enrichment
# ──────────────────────────────────────────────

@aspectlib.Aspect(bind=True)
def error_enrich(cutpoint, *args, **kwargs):
    """Catch and enrich errors with context."""
    method = cutpoint.__name__ if cutpoint else "unknown"
    try:
        result = yield aspectlib.Proceed
        yield aspectlib.Return(result)
    except Exception as e:
        log.error("pipeline_error",
            method=method,
            error_type=type(e).__name__,
            error_msg=str(e),
        )
        raise


# ──────────────────────────────────────────────
# 5. RTP Contract Validator (for tests)
# ──────────────────────────────────────────────

@aspectlib.Aspect(bind=True)
def rtp_contract(cutpoint, *args, **kwargs):
    """Verify RTP packet invariants. Use in tests."""
    result = yield aspectlib.Proceed
    method = cutpoint.__name__ if cutpoint else "unknown"
    if method == "build_rtp_header" and result:
        assert result[0] >> 6 == 2, "RTP version must be 2"
        seq = struct.unpack('>H', result[2:4])[0]
        assert 0 <= seq <= 65535, f"Sequence out of range: {seq}"
    yield aspectlib.Return(result)


# ──────────────────────────────────────────────
# Apply / Remove
# ──────────────────────────────────────────────

def apply_debug_aspects():
    """Weave all debug aspects into the codebase. Safe to call multiple times."""
    try:
        from discord_video_stream.stream_connection import StreamConnection
        aspectlib.weave(StreamConnection, protocol_trace)
        _woven_targets.append((StreamConnection, protocol_trace))
        log.info("woven", target="StreamConnection", aspect="protocol_trace")
    except ImportError as e:
        log.warning("skip_weave", target="StreamConnection", reason=str(e))

    try:
        from discord_video_stream.media import pacer
        aspectlib.weave(pacer.FramePacer, timing)
        _woven_targets.append((pacer.FramePacer, timing))
        log.info("woven", target="FramePacer", aspect="timing")
    except ImportError as e:
        log.warning("skip_weave", target="FramePacer", reason=str(e))

    try:
        from discord_video_stream.rtp import serialize
        aspectlib.weave(serialize, packet_trace)
        _woven_targets.append((serialize, packet_trace))
        log.info("woven", target="rtp.serialize", aspect="packet_trace")
    except ImportError as e:
        log.warning("skip_weave", target="rtp.serialize", reason=str(e))

    try:
        from discord_video_stream.rtp import crypto
        aspectlib.weave(crypto.TransportEncryptor, packet_trace)
        _woven_targets.append((crypto.TransportEncryptor, packet_trace))
        log.info("woven", target="TransportEncryptor", aspect="packet_trace")
    except ImportError as e:
        log.warning("skip_weave", target="TransportEncryptor", reason=str(e))

    try:
        from discord_video_stream.voice_send import VideoSender, AudioSender
        aspectlib.weave(VideoSender, error_enrich)
        aspectlib.weave(AudioSender, error_enrich)
        _woven_targets.extend([
            (VideoSender, error_enrich),
            (AudioSender, error_enrich),
        ])
        log.info("woven", target="VideoSender+AudioSender", aspect="error_enrich")
    except ImportError as e:
        log.warning("skip_weave", target="VideoSender", reason=str(e))

    log.info("aspects_applied", count=len(_woven_targets))


def remove_debug_aspects():
    """Roll back all woven aspects."""
    for target, aspect in _woven_targets:
        try:
            aspectlib.weave(target, aspectlib.Rollback)
        except Exception:
            pass
    _woven_targets.clear()
    log.info("aspects_removed")
