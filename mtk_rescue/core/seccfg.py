"""Parser for MediaTek seccfg V3 blobs.

Reference: mtkclient/Library/Hardware/seccfg.py (V3 struct).

Layout (little-endian):

    offset  size  field
    ------  ----  -----
    0x00    16    info_header  = b"AND_SECCFG_v\\x00\\x00\\x00\\x00"
    0x10     4    magic        = 0x4D4D4D4D  (b"MMMM")
    0x14     4    seccfg_ver   (V3 = 3)
    0x18     4    seccfg_size  (V3 = 0x1860)
    0x1C     4    seccfg_enc_offset
    0x20     4    seccfg_enc_len     ← lock indicator: 0x1 = LOCKED, 0x07F20000 = UNLOCKED
    0x24     1    sw_sec_lock_try
    0x25     1    sw_sec_lock_done
    0x26     2    page_size
    0x28     4    page_count
    ...     ...   encrypted payload (SW/V2/V3/V4)
    size-4   4    endflag      = 0x45454545  (b"EEEE")

We can't tell from outside whether the encrypted payload was sealed with the
SW key (mtkclient knows it) or the SEJ HW key (only the SoC knows it). That's
the brick distinguisher. But we *can* surface every field above so the user
sees concretely what changed across a lock/wipe operation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


INFO_HEADER_PREFIX = b"AND_SECCFG_v"
MAGIC = b"MMMM"  # = 0x4D4D4D4D little-endian
ENDFLAG = b"EEEE"  # = 0x45454545 little-endian

LOCK_FLAG_LOCKED = 0x1
LOCK_FLAG_UNLOCKED = 0x07F20000

# Minimum bytes to read before we can decide anything useful.
HEADER_BYTES = 0x24


@dataclass(frozen=True)
class SecCfgView:
    state: str  # "VALID" | "WIPED" | "INVALID"
    reason: str
    info_header: bytes
    magic_ok: bool
    endflag_ok: bool
    version: int | None
    size: int | None
    enc_offset: int | None
    enc_len: int | None
    lock_state: str  # "LOCKED" | "UNLOCKED" | "UNKNOWN(<hex>)" | "n/a"

    def summary_lines(self) -> list[str]:
        if self.state == "WIPED":
            return [
                "seccfg state: WIPED (all zeros)",
                "  preloader will fall back to factory-default-locked",
            ]
        if self.state == "INVALID":
            return [f"seccfg state: INVALID — {self.reason}"]
        return [
            f"seccfg state: VALID  version={self.version}  size={self.size:#x}",
            f"  info_header: {self.info_header!r}",
            f"  magic_ok={self.magic_ok}  endflag_ok={self.endflag_ok}",
            f"  enc_offset={self.enc_offset:#x}  enc_len={self.enc_len:#x}",
            f"  lock_state: {self.lock_state}",
        ]


def _lock_label(enc_len: int) -> str:
    if enc_len == LOCK_FLAG_LOCKED:
        return "LOCKED"
    if enc_len == LOCK_FLAG_UNLOCKED:
        return "UNLOCKED"
    return f"UNKNOWN({enc_len:#x})"


def parse_seccfg(data: bytes) -> SecCfgView:
    if len(data) < HEADER_BYTES:
        return SecCfgView(
            state="INVALID",
            reason=f"too short ({len(data)} B, need {HEADER_BYTES})",
            info_header=b"",
            magic_ok=False,
            endflag_ok=False,
            version=None,
            size=None,
            enc_offset=None,
            enc_len=None,
            lock_state="n/a",
        )

    head = data[:HEADER_BYTES]
    if head == b"\x00" * HEADER_BYTES:
        return SecCfgView(
            state="WIPED",
            reason="header region is all zeros",
            info_header=b"",
            magic_ok=False,
            endflag_ok=False,
            version=None,
            size=None,
            enc_offset=None,
            enc_len=None,
            lock_state="n/a",
        )

    info_header = data[0:16]
    magic = data[16:20]
    seccfg_ver, seccfg_size, seccfg_enc_offset, seccfg_enc_len = struct.unpack_from(
        "<IIII", data, 0x14
    )

    magic_ok = magic == MAGIC

    endflag_ok = False
    if 0 < seccfg_size <= len(data):
        endflag_ok = data[seccfg_size - 4 : seccfg_size] == ENDFLAG

    if not magic_ok:
        return SecCfgView(
            state="INVALID",
            reason=f"bad magic {magic!r} (expected b'MMMM')",
            info_header=info_header,
            magic_ok=False,
            endflag_ok=endflag_ok,
            version=seccfg_ver,
            size=seccfg_size,
            enc_offset=seccfg_enc_offset,
            enc_len=seccfg_enc_len,
            lock_state=_lock_label(seccfg_enc_len),
        )

    return SecCfgView(
        state="VALID",
        reason="",
        info_header=info_header,
        magic_ok=True,
        endflag_ok=endflag_ok,
        version=seccfg_ver,
        size=seccfg_size,
        enc_offset=seccfg_enc_offset,
        enc_len=seccfg_enc_len,
        lock_state=_lock_label(seccfg_enc_len),
    )


def diff_summary(before: SecCfgView, after: SecCfgView) -> list[str]:
    """Human-readable diff between two parsed seccfg views."""
    if before.state != after.state:
        return [f"state: {before.state} → {after.state}"]
    if before.state == "VALID":
        out: list[str] = []
        if before.enc_len != after.enc_len:
            out.append(f"enc_len: {before.enc_len:#x} → {after.enc_len:#x}")
        if before.lock_state != after.lock_state:
            out.append(f"lock_state: {before.lock_state} → {after.lock_state}")
        if before.size != after.size:
            out.append(f"size: {before.size:#x} → {after.size:#x}")
        if not out:
            out.append("no field-level differences (encrypted payload may still differ)")
        return out
    return ["(no fields to compare in non-VALID states)"]
