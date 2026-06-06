"""Sniff the type of a MediaTek preloader binary by its leading magic bytes.

Three forms in the wild:

  GFH (Generic Flash Header) — what you get from a standalone Xiaomi/OEM ROM
    extract (`images/preloader_<codename>.bin`). Raw preloader payload with
    an MMM\\x01 header. mtkclient's `--preloader` flag reads this form.

  UFS_BOOT — what you get when you dump LU1/LU2 from a UFS device (e.g. via
    mtkclient `r preloader ... --parttype=boot1`). The BROM flash flow wraps
    the GFH payload with a UFS_BOOT header + BRLYT table + 0x1000 padding.

  EMMC_BOOT — eMMC equivalent of UFS_BOOT, found on eMMC-based devices.

mtkclient accepts all three for `--preloader`; it sniffs the magic and locates
the EMI/DRAM config inside the GFH payload regardless of wrapper. This module
just gives us a friendly name to surface in the UI so users see what they
selected.
"""

from __future__ import annotations

from pathlib import Path


# 4d4d 4d01 — start of the GFH FILE_INFO struct
GFH_MAGIC = b"MMM\x01"
UFS_BOOT_MAGIC = b"UFS_BOOT"
EMMC_BOOT_MAGIC = b"EMMC_BOOT"
COMBO_BOOT_MAGIC = b"COMBO_BOOT"


def identify_preloader(path: Path) -> str | None:
    """Return a short tag for the preloader form, or None if unrecognised."""
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return None
    if head.startswith(GFH_MAGIC):
        return "GFH (raw, from ROM extract)"
    if head.startswith(UFS_BOOT_MAGIC):
        return "UFS_BOOT (from on-device LU dump)"
    if head.startswith(EMMC_BOOT_MAGIC):
        return "EMMC_BOOT (from on-device boot partition)"
    if head.startswith(COMBO_BOOT_MAGIC):
        return "COMBO_BOOT"
    return None
