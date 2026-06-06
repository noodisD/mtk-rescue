from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .mtk import MtkClient


RecipeRunner = Callable[[MtkClient], Iterator[str]]


@dataclass(frozen=True)
class Recipe:
    id: str
    title: str
    description: str
    writes_device: bool
    runner: RecipeRunner


def _printgpt(mtk: MtkClient) -> Iterator[str]:
    yield from mtk.stream("printgpt")


# ---- GPT restore for begonia (Redmi Note 8 Pro, UFS, 4K sectors) ------------
#
# Context: this device's brick state is "GPT wiped on LU0 sector 1" + seccfg
# encryption-format mismatch. Partition contents are already stock (rewritten
# in Phase 6); reflashing them is a no-op. The blockers are:
#   1) restoring the GPT so the preloader can locate `lk` again, and
#   2) re-locking seccfg with the SEJ-encrypted hash LK actually verifies.
#
# This recipe addresses #1. It expects two prepared GPT blob files in /tmp
# (generated in a prior session). The secondary-GPT offset is device-specific
# and lives 33 sectors before the end of LU0; for begonia's LU0 size that
# offset is 0xEE57F8000 (confirmed in the recovery plan; do not generalise
# without measuring per-device).
#
# Uses mtkclient's `multi` to chain three operations in a single DA session
# (two writes + a verification read), so the user only has to win one BROM
# window instead of three.

BEGONIA_PGPT_PATH = Path("/tmp/pgpt.bin")
BEGONIA_SGPT_PATH = Path("/tmp/sgpt.bin")
BEGONIA_SGPT_OFFSET = 0xEE57F8000
# Verification: 4K-sector UFS → primary GPT header sits at offset 0x1000
# (= sector 1). Read 0x200 bytes and check for EFI PART magic.
BEGONIA_GPT_HEADER_OFFSET = 0x1000
BEGONIA_VERIFY_PATH = Path("/tmp/gpt_verify_sector1.bin")


def _validate_gpt_blob(path: Path, *, role: str) -> int:
    """Raise on missing/empty/obviously-wrong file; return its size in bytes.

    The primary blob (`pgpt.bin`) holds protective MBR + GPT header + entries
    starting from LBA 0; the secondary (`sgpt.bin`) is just the header + entries
    starting with EFI PART magic. We accept both because mtkclient generates them
    in those forms.
    """
    if not path.exists():
        raise FileNotFoundError(f"GPT {role} blob missing: {path}")
    size = path.stat().st_size
    if size < 1024:
        raise ValueError(f"GPT {role} blob too small ({size} B): {path}")
    return size


def _restore_gpt_begonia(mtk: MtkClient) -> Iterator[str]:
    pgpt_size = _validate_gpt_blob(BEGONIA_PGPT_PATH, role="primary")
    sgpt_size = _validate_gpt_blob(BEGONIA_SGPT_PATH, role="secondary")

    # Cheap structural sanity check on the secondary blob — it should literally
    # begin with EFI PART. The primary blob's EFI PART sits one sector in
    # (variable by sector size), so we don't validate it here; the post-write
    # verification read of device sector 1 is the real check.
    sgpt_magic = BEGONIA_SGPT_PATH.read_bytes()[:8]
    if sgpt_magic != b"EFI PART":
        raise ValueError(
            f"{BEGONIA_SGPT_PATH} does not start with EFI PART magic "
            f"(got {sgpt_magic!r}). Refusing to write a bad secondary GPT."
        )

    yield f"[gpt-restore] pre-flight OK: pgpt={pgpt_size} B, sgpt={sgpt_size} B"
    yield f"[gpt-restore] target offsets: pgpt→0x0, sgpt→{BEGONIA_SGPT_OFFSET:#x}"
    yield f"[gpt-restore] verify: read 0x200 B from {BEGONIA_GPT_HEADER_OFFSET:#x}"

    if BEGONIA_VERIFY_PATH.exists():
        BEGONIA_VERIFY_PATH.unlink()

    # Single DA session via mtkclient's `multi` — semicolon-separated commands.
    cmds = ";".join(
        [
            f"wo 0x0 {pgpt_size:#x} {BEGONIA_PGPT_PATH}",
            f"wo {BEGONIA_SGPT_OFFSET:#x} {sgpt_size:#x} {BEGONIA_SGPT_PATH}",
            f"ro {BEGONIA_GPT_HEADER_OFFSET:#x} 0x200 {BEGONIA_VERIFY_PATH}",
        ]
    )
    yield f"[gpt-restore] mtkclient multi: {cmds}"
    yield from mtk.stream("multi", cmds)

    # Local-side verification: did the device's sector 1 actually come back
    # as a GPT header? This is the difference between "mtkclient said the
    # write succeeded" and "the bytes are physically on the platter."
    if not BEGONIA_VERIFY_PATH.exists():
        yield "[gpt-restore] FAIL: verification read produced no file"
        return
    sector1 = BEGONIA_VERIFY_PATH.read_bytes()
    if len(sector1) < 8:
        yield f"[gpt-restore] FAIL: verification file too short ({len(sector1)} B)"
        return
    if sector1[:8] == b"EFI PART":
        yield "[gpt-restore] SUCCESS: device sector 1 now contains EFI PART"
        yield "[gpt-restore] next step: re-lock seccfg now that GPT exists"
    else:
        yield f"[gpt-restore] FAIL: sector 1 magic is {sector1[:8]!r}, expected b'EFI PART'"
        yield "[gpt-restore] writes may not have landed; check earlier mtkclient errors"


def _verify_gpt(mtk: MtkClient) -> Iterator[str]:
    """Read-only: dump sector 1 of LU0 and check for EFI PART magic.

    Cheap pre-flight to answer "did our earlier GPT write actually land?"
    without doing the full printgpt parse.
    """
    if BEGONIA_VERIFY_PATH.exists():
        BEGONIA_VERIFY_PATH.unlink()
    yield f"[gpt-verify] reading device offset {BEGONIA_GPT_HEADER_OFFSET:#x} (0x200 B)"
    yield from mtk.stream(
        "ro",
        f"{BEGONIA_GPT_HEADER_OFFSET:#x}",
        "0x200",
        str(BEGONIA_VERIFY_PATH),
    )
    if not BEGONIA_VERIFY_PATH.exists():
        yield "[gpt-verify] FAIL: read produced no file"
        return
    head = BEGONIA_VERIFY_PATH.read_bytes()[:8]
    if head == b"EFI PART":
        yield "[gpt-verify] GPT is present (sector 1 contains EFI PART)"
    elif head == b"\x00" * 8:
        yield "[gpt-verify] GPT is WIPED (sector 1 is all zeros)"
    else:
        yield f"[gpt-verify] GPT is UNKNOWN: sector 1 magic = {head!r}"


RECIPES: dict[str, Recipe] = {
    "diag.printgpt": Recipe(
        id="diag.printgpt",
        title="List GPT partitions",
        description="Read the GPT from device and display every partition entry. Read-only.",
        writes_device=False,
        runner=_printgpt,
    ),
    "diag.verifygpt": Recipe(
        id="diag.verifygpt",
        title="Verify GPT header (sector 1)",
        description=(
            "Raw read of LU0 sector 1 to confirm whether a GPT exists. "
            "Use before/after a GPT restore. Read-only."
        ),
        writes_device=False,
        runner=_verify_gpt,
    ),
    "repair.gpt_begonia": Recipe(
        id="repair.gpt_begonia",
        title="Restore wiped GPT (begonia, from /tmp/pgpt.bin + sgpt.bin)",
        description=(
            "Write the primary GPT to offset 0x0 and the secondary GPT to "
            f"{BEGONIA_SGPT_OFFSET:#x} of LU0, then verify sector 1. Single DA "
            "session via mtkclient `multi`. WRITES the device."
        ),
        writes_device=True,
        runner=_restore_gpt_begonia,
    ),
}
