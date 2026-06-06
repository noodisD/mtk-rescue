"""Tests for the seccfg V3 parser. Synthesises blobs since we can't ship real ones."""

import struct

from mtk_rescue.core.seccfg import (
    ENDFLAG,
    INFO_HEADER_PREFIX,
    LOCK_FLAG_LOCKED,
    LOCK_FLAG_UNLOCKED,
    MAGIC,
    diff_summary,
    parse_seccfg,
)


def _make_blob(enc_len: int, *, size: int = 0x1860, ver: int = 3) -> bytes:
    info_header = INFO_HEADER_PREFIX + b"\x00" * (16 - len(INFO_HEADER_PREFIX))
    head = (
        info_header
        + MAGIC
        + struct.pack("<IIII", ver, size, 0x28, enc_len)
        + b"\x00" * (size - 0x24 - 4)
        + ENDFLAG
    )
    assert len(head) == size, (len(head), size)
    return head


def test_locked_blob_parses():
    v = parse_seccfg(_make_blob(LOCK_FLAG_LOCKED))
    assert v.state == "VALID"
    assert v.magic_ok and v.endflag_ok
    assert v.version == 3
    assert v.lock_state == "LOCKED"


def test_unlocked_blob_parses():
    v = parse_seccfg(_make_blob(LOCK_FLAG_UNLOCKED))
    assert v.state == "VALID"
    assert v.lock_state == "UNLOCKED"


def test_wiped_blob_detected():
    v = parse_seccfg(b"\x00" * 0x200)
    assert v.state == "WIPED"


def test_bad_magic_is_invalid():
    blob = bytearray(_make_blob(LOCK_FLAG_LOCKED))
    blob[16:20] = b"XXXX"
    v = parse_seccfg(bytes(blob))
    assert v.state == "INVALID"
    assert "bad magic" in v.reason


def test_too_short_is_invalid():
    v = parse_seccfg(b"AND_SECCFG")
    assert v.state == "INVALID"
    assert "too short" in v.reason


def test_diff_detects_lock_flip():
    before = parse_seccfg(_make_blob(LOCK_FLAG_UNLOCKED))
    after = parse_seccfg(_make_blob(LOCK_FLAG_LOCKED))
    diff = diff_summary(before, after)
    assert any("lock_state" in line and "UNLOCKED" in line and "LOCKED" in line for line in diff)


def test_diff_state_transition():
    before = parse_seccfg(_make_blob(LOCK_FLAG_LOCKED))
    after = parse_seccfg(b"\x00" * 0x200)
    diff = diff_summary(before, after)
    assert any("VALID" in line and "WIPED" in line for line in diff)
