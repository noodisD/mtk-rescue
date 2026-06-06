from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .mtk import MtkClient
from .seccfg import diff_summary, parse_seccfg


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


# ---- seccfg ops for begonia -------------------------------------------------
#
# On this device the seccfg partition lives at LU0 offset 0x13800000 (measured;
# device-specific). It holds the V3 hash that LK verifies at boot. Our current
# brick state: the partition is still populated, but it was sealed by mtkclient's
# SW-encryption path during the original unlock — while LK expects the HW-SEJ
# form. LK silent-halts.
#
# mtkclient's `da seccfg lock` auto-detects the existing encryption type during
# its parse step and re-uses the same type when writing. So locking the current
# SW-encrypted blob produces another SW-encrypted blob → LK still rejects. We
# offer it anyway because (a) on a *fresh* device the type would be SEJ and lock
# would do the right thing; (b) sometimes the v3 plan's "try lock first" is the
# right ordering even if it likely no-ops here.
#
# The fallback that sidesteps the encryption-type question entirely: wipe the
# header. When the preloader can't find the seccfg magic, it falls back to
# factory-default-locked, treating the device as fresh. No hash to mismatch.

BEGONIA_SECCFG_OFFSET = 0x13800000
SECCFG_DUMP_LEN = 0x2000  # comfortably covers V2/V3/V4 (V3 = 0x1860)
SECCFG_WIPE_LEN = 0x200  # zeroing the header alone is sufficient
SECCFG_BLANK_PATH = Path("/tmp/seccfg_blank.bin")
SECCFG_BEFORE_PATH = Path("/tmp/seccfg_before.bin")
SECCFG_AFTER_PATH = Path("/tmp/seccfg_after.bin")


def _emit_seccfg_view(label: str, path: Path) -> Iterator[str]:
    """Read a dumped seccfg file from disk and yield human-readable parse output."""
    if not path.exists():
        yield f"[seccfg] {label}: dump file missing ({path})"
        return
    view = parse_seccfg(path.read_bytes())
    yield f"[seccfg] {label}:"
    for line in view.summary_lines():
        yield f"[seccfg]   {line}"


def _seccfg_dump(mtk: MtkClient) -> Iterator[str]:
    if SECCFG_BEFORE_PATH.exists():
        SECCFG_BEFORE_PATH.unlink()
    yield f"[seccfg] dumping {SECCFG_DUMP_LEN:#x} B from {BEGONIA_SECCFG_OFFSET:#x}"
    yield from mtk.stream(
        "ro",
        f"{BEGONIA_SECCFG_OFFSET:#x}",
        f"{SECCFG_DUMP_LEN:#x}",
        str(SECCFG_BEFORE_PATH),
    )
    yield from _emit_seccfg_view("current", SECCFG_BEFORE_PATH)


def _seccfg_lock(mtk: MtkClient) -> Iterator[str]:
    """Try mtkclient `da seccfg lock`. Dump before AND after so we can see what changed.

    Single DA session via `multi`. We don't trust the lock to actually fix the
    SEJ-vs-SW mismatch — we run it because it's the v3 plan's "try first" step
    and because the diff tells us concretely whether anything moved.
    """
    for p in (SECCFG_BEFORE_PATH, SECCFG_AFTER_PATH):
        if p.exists():
            p.unlink()

    cmds = ";".join(
        [
            f"ro {BEGONIA_SECCFG_OFFSET:#x} {SECCFG_DUMP_LEN:#x} {SECCFG_BEFORE_PATH}",
            "da seccfg lock",
            f"ro {BEGONIA_SECCFG_OFFSET:#x} {SECCFG_DUMP_LEN:#x} {SECCFG_AFTER_PATH}",
        ]
    )
    yield f"[seccfg-lock] mtkclient multi: {cmds}"
    yield from mtk.stream("multi", cmds)

    yield from _emit_seccfg_view("before", SECCFG_BEFORE_PATH)
    yield from _emit_seccfg_view("after", SECCFG_AFTER_PATH)

    if SECCFG_BEFORE_PATH.exists() and SECCFG_AFTER_PATH.exists():
        before = parse_seccfg(SECCFG_BEFORE_PATH.read_bytes())
        after = parse_seccfg(SECCFG_AFTER_PATH.read_bytes())
        yield "[seccfg-lock] diff:"
        for line in diff_summary(before, after):
            yield f"[seccfg-lock]   {line}"
        yield (
            "[seccfg-lock] NOTE: a successful lock here does NOT prove the LK will accept it. "
            "If the boot test fails, run repair.seccfg_wipe next."
        )


def _seccfg_wipe(mtk: MtkClient) -> Iterator[str]:
    """Overwrite the seccfg header with zeros. Preloader then falls back to factory-default.

    This is the fallback when lock didn't fix the brick. It bypasses the
    SW-vs-SEJ encryption question entirely: no magic, no validation, factory
    defaults apply.
    """
    # Generate the blank locally — no point sending zeros over USB by hand.
    SECCFG_BLANK_PATH.write_bytes(b"\x00" * SECCFG_WIPE_LEN)
    yield f"[seccfg-wipe] prepared blank: {SECCFG_BLANK_PATH} ({SECCFG_WIPE_LEN:#x} B of zeros)"

    for p in (SECCFG_BEFORE_PATH, SECCFG_AFTER_PATH):
        if p.exists():
            p.unlink()

    cmds = ";".join(
        [
            f"ro {BEGONIA_SECCFG_OFFSET:#x} {SECCFG_DUMP_LEN:#x} {SECCFG_BEFORE_PATH}",
            f"wo {BEGONIA_SECCFG_OFFSET:#x} {SECCFG_WIPE_LEN:#x} {SECCFG_BLANK_PATH}",
            f"ro {BEGONIA_SECCFG_OFFSET:#x} {SECCFG_DUMP_LEN:#x} {SECCFG_AFTER_PATH}",
        ]
    )
    yield f"[seccfg-wipe] mtkclient multi: {cmds}"
    yield from mtk.stream("multi", cmds)

    yield from _emit_seccfg_view("before", SECCFG_BEFORE_PATH)
    yield from _emit_seccfg_view("after", SECCFG_AFTER_PATH)

    if SECCFG_AFTER_PATH.exists():
        after = parse_seccfg(SECCFG_AFTER_PATH.read_bytes())
        if after.state == "WIPED":
            yield "[seccfg-wipe] SUCCESS: seccfg header is now zeroed."
            yield "[seccfg-wipe] preloader will treat device as factory-default-locked. Boot test now."
        else:
            yield f"[seccfg-wipe] FAIL: post-wipe state is {after.state}; write did not land."


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
    "diag.seccfg_dump": Recipe(
        id="diag.seccfg_dump",
        title="Dump and parse seccfg (begonia)",
        description=(
            f"Raw read {SECCFG_DUMP_LEN:#x} B at {BEGONIA_SECCFG_OFFSET:#x} on LU0 and "
            "parse the V3 struct (magic, version, lock_state, endflag). Read-only."
        ),
        writes_device=False,
        runner=_seccfg_dump,
    ),
    "repair.seccfg_lock": Recipe(
        id="repair.seccfg_lock",
        title="Lock seccfg (try first; may not fix SEJ mismatch)",
        description=(
            "Dump → `da seccfg lock` → re-dump → diff. Single DA session. WRITES. "
            "Honest caveat: mtkclient lock reuses the existing encryption type, so "
            "if seccfg is currently SW-encrypted, the relock stays SW. Run wipe if "
            "boot test still fails."
        ),
        writes_device=True,
        runner=_seccfg_lock,
    ),
    "repair.seccfg_wipe": Recipe(
        id="repair.seccfg_wipe",
        title="Wipe seccfg header → factory-default-locked (fallback)",
        description=(
            f"Overwrite {SECCFG_WIPE_LEN:#x} B at {BEGONIA_SECCFG_OFFSET:#x} with zeros, "
            "then re-dump to confirm. Preloader sees no seccfg magic and falls "
            "back to factory-default-locked, sidestepping the SW-vs-SEJ encryption "
            "question. WRITES."
        ),
        writes_device=True,
        runner=_seccfg_wipe,
    ),
}
