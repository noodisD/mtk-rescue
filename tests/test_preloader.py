"""Tests for preloader magic-byte sniffing.

Ground truth from the user's actual on-disk file:
  /home/noodis/android-flash/downloads/.../preloader_begonia.bin starts with
  4d4d 4d01 3800 0000 4649 4c45 5f49 4e46  (MMM.8...FILE_INF)
This is the GFH FILE_INFO struct — the standalone Xiaomi ROM form.
"""

from pathlib import Path

from mtk_rescue.core.preloader import identify_preloader


def test_gfh_form_from_xiaomi_extract(tmp_path):
    # First 16 bytes verbatim from the user's preloader_begonia.bin
    p = tmp_path / "preloader_begonia.bin"
    p.write_bytes(b"\x4d\x4d\x4d\x01\x38\x00\x00\x00FILE_INF" + b"\x00" * 512)
    assert identify_preloader(p) == "GFH (raw, from ROM extract)"


def test_ufs_boot_form_from_lu_dump(tmp_path):
    p = tmp_path / "boot1_content.bin"
    p.write_bytes(b"UFS_BOOT" + b"\x00" * 512)
    assert identify_preloader(p) == "UFS_BOOT (from on-device LU dump)"


def test_emmc_boot_form(tmp_path):
    p = tmp_path / "preloader_emmc.bin"
    p.write_bytes(b"EMMC_BOOT" + b"\x00" * 512)
    assert identify_preloader(p) == "EMMC_BOOT (from on-device boot partition)"


def test_unknown_magic_returns_none(tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(b"random garbage data" + b"\x00" * 512)
    assert identify_preloader(p) is None


def test_missing_file_returns_none(tmp_path):
    assert identify_preloader(tmp_path / "does-not-exist.bin") is None
