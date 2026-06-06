"""Tests for the GPT-restore recipe's pre-flight validators.

The recipe itself talks to a phone and can't be unit-tested end to end, but
the file checks (existence, size, magic on the secondary blob) are pure logic
that can — and should — fail-fast before we burn a BROM window on bad inputs.
"""

import pytest

from mtk_rescue.core.recipes import _validate_gpt_blob


def test_validate_gpt_blob_accepts_reasonable_file(tmp_path):
    p = tmp_path / "pgpt.bin"
    p.write_bytes(b"\x00" * 4096)
    assert _validate_gpt_blob(p, role="primary") == 4096


def test_validate_gpt_blob_rejects_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="primary"):
        _validate_gpt_blob(tmp_path / "nope.bin", role="primary")


def test_validate_gpt_blob_rejects_too_small(tmp_path):
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"too small")
    with pytest.raises(ValueError, match="too small"):
        _validate_gpt_blob(p, role="secondary")
