"""Streaming parser for mtkclient stdout.

Feed each line through LogParser.feed(); it emits structured LogEvents that the UI
consumes to populate the Device Info panel, surface errors as Findings with
suggested fixes, and track session state (watchdog disabled, DRAM passed, etc.).

Adding a new pattern: append a Pattern to PATTERNS. Each pattern describes one
regex and what to do with the captured groups.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class EventKind(str, Enum):
    DEVICE_INFO = "device_info"  # extracted fact about the device
    STATE = "state"  # session-state transition (watchdog disabled, DA uploaded, …)
    ERROR = "error"  # something went wrong; may include suggested_fix
    PROGRESS = "progress"  # progress markers (sector counts, percentages)


@dataclass(frozen=True)
class LogEvent:
    kind: EventKind
    key: str
    value: str = ""
    suggested_fix: str | None = None  # opaque code for the UI to dispatch on
    message: str = ""  # human-readable summary for findings


@dataclass(frozen=True)
class Pattern:
    regex: re.Pattern[str]
    kind: EventKind
    key: str
    # group_extractor returns (value, message); takes the regex Match.
    extract: Callable[[re.Match[str]], tuple[str, str]] = field(
        default=lambda m: (m.group(1) if m.groups() else "", "")
    )
    suggested_fix: str | None = None


def _val(group: int = 1) -> Callable[[re.Match[str]], tuple[str, str]]:
    return lambda m: (m.group(group).strip(), "")


def _val_msg(group: int, message: str) -> Callable[[re.Match[str]], tuple[str, str]]:
    return lambda m: (m.group(group).strip(), message)


def _just_msg(message: str) -> Callable[[re.Match[str]], tuple[str, str]]:
    return lambda m: ("", message)


# Order matters — first match wins per line, so place more specific patterns first.
PATTERNS: tuple[Pattern, ...] = (
    # ---- device info: from BROM/Preloader handshake ----
    Pattern(re.compile(r"Preloader -\s+CPU:\s+(.+)$"), EventKind.DEVICE_INFO, "cpu", _val()),
    Pattern(re.compile(r"Preloader - HW code:\s+(\S+)"), EventKind.DEVICE_INFO, "hw_code", _val()),
    Pattern(
        re.compile(r"Preloader - Target config:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "target_config",
        _val(),
    ),
    Pattern(
        re.compile(r"Preloader -\s+SBC enabled:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "sbc_enabled",
        _val(),
    ),
    Pattern(
        re.compile(r"Preloader -\s+SLA enabled:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "sla_enabled",
        _val(),
    ),
    Pattern(re.compile(r"Preloader - ME_ID:\s+([0-9A-F]+)"), EventKind.DEVICE_INFO, "me_id", _val()),
    Pattern(
        re.compile(r"Preloader - SOC_ID:\s+([0-9A-F]+)"), EventKind.DEVICE_INFO, "soc_id", _val()
    ),
    # ---- UFS storage info from DA ----
    Pattern(
        re.compile(r"DAXFlash - UFS Blocksize:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "ufs_blocksize",
        _val(),
    ),
    Pattern(re.compile(r"DAXFlash - UFS ID:\s+(\S+)"), EventKind.DEVICE_INFO, "ufs_id", _val()),
    Pattern(re.compile(r"DAXFlash - UFS MID:\s+(\S+)"), EventKind.DEVICE_INFO, "ufs_mid", _val()),
    Pattern(
        re.compile(r"DAXFlash - UFS FWVer:\s+(\S+)"), EventKind.DEVICE_INFO, "ufs_fwver", _val()
    ),
    Pattern(
        re.compile(r"DAXFlash - UFS Serial:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "ufs_serial",
        _val(),
    ),
    Pattern(
        re.compile(r"DAXFlash - UFS LU0 Size:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "ufs_lu0_size",
        _val(),
    ),
    Pattern(
        re.compile(r"DAXFlash - UFS LU1 Size:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "ufs_lu1_size",
        _val(),
    ),
    Pattern(
        re.compile(r"DAXFlash - UFS LU2 Size:\s+(\S+)"),
        EventKind.DEVICE_INFO,
        "ufs_lu2_size",
        _val(),
    ),
    # ---- session state transitions ----
    Pattern(
        re.compile(r"Preloader - Disabling Watchdog"),
        EventKind.STATE,
        "watchdog_disabled",
        _just_msg("BROM watchdog disabled — session is now stable."),
    ),
    Pattern(
        re.compile(r"Exploitation - Kamakiri Run"),
        EventKind.STATE,
        "kamakiri",
        _just_msg("Kamakiri BROM exploit fired."),
    ),
    Pattern(
        re.compile(r"PLTools - Successfully sent payload"),
        EventKind.STATE,
        "payload_sent",
        _just_msg("BROM payload accepted."),
    ),
    Pattern(
        re.compile(r"DAXFlash - Successfully uploaded stage 1"),
        EventKind.STATE,
        "da_stage1",
        _just_msg("DA stage 1 uploaded."),
    ),
    Pattern(
        re.compile(r"DAXFlash - Successfully received DA sync"),
        EventKind.STATE,
        "da_sync",
        _just_msg("DA sync received."),
    ),
    Pattern(
        re.compile(r"DAXFlash - DRAM setup passed"),
        EventKind.STATE,
        "dram_ok",
        _just_msg("DRAM init OK."),
    ),
    Pattern(
        re.compile(r"DAXFlash - Successfully uploaded stage 2"),
        EventKind.STATE,
        "da_stage2",
        _just_msg("DA stage 2 uploaded."),
    ),
    Pattern(
        re.compile(r"Main - Handling da commands"),
        EventKind.STATE,
        "da_ready",
        _just_msg("DA ready — partition operations now possible."),
    ),
    Pattern(
        re.compile(r"XFlash - Done"),
        EventKind.STATE,
        "xflash_done",
        _just_msg("XFlash operation completed."),
    ),
    # ---- known errors with auto-fix suggestions ----
    Pattern(
        re.compile(r"DRAM setup failed.*--preloader"),
        EventKind.ERROR,
        "dram_needs_preloader",
        _just_msg(
            "DRAM init failed because mtkclient couldn't extract EMI from RAM. "
            "Provide a stock preloader file via MTK_RESCUE_PRELOADER."
        ),
        suggested_fix="set_preloader",
    ),
    Pattern(
        re.compile(r"Auth file is required.*--auth"),
        EventKind.ERROR,
        "auth_required",
        _just_msg(
            "Device is auth-protected. If you see 'Bypassing security' next, "
            "Kamakiri handled it and this can be ignored. Only supply MTK_RESCUE_AUTH "
            "if the bypass fails."
        ),
        suggested_fix="set_auth",
    ),
    Pattern(
        re.compile(r"Device is in BROM-Mode\. Bypassing security"),
        EventKind.STATE,
        "security_bypassed",
        _just_msg("BROM-Mode security bypass active — auth_required can be ignored."),
    ),
    Pattern(
        re.compile(r"Couldn't detect partition:\s*(\S+)"),
        EventKind.ERROR,
        "partition_not_found",
        _val_msg(
            1,
            "Partition not found in GPT. For preloader on UFS devices, retry with "
            "--parttype=boot1 / boot2. Otherwise the GPT may be corrupted.",
        ),
        suggested_fix=None,
    ),
    Pattern(
        re.compile(r"Status: Handshake failed, retrying"),
        EventKind.ERROR,
        "handshake_failed",
        _just_msg(
            "BROM handshake failed — likely because the device is in Preloader mode, "
            "not BROM. Re-enter BROM (hold Vol Up + Vol Down before plugging USB)."
        ),
    ),
    Pattern(
        re.compile(r"Status: Waiting for PreLoader VCOM"),
        EventKind.STATE,
        "waiting_for_device",
        _just_msg("mtkclient is waiting for the device. Enter BROM mode now."),
    ),
    Pattern(
        re.compile(r"USB disconnect"),
        EventKind.STATE,
        "usb_disconnect",
        _just_msg("Device disconnected from USB."),
    ),
    Pattern(
        re.compile(r"Failed to upload da"),
        EventKind.ERROR,
        "da_upload_failed",
        _just_msg("DA upload failed. Check earlier errors for the root cause."),
    ),
)


class LogParser:
    """Stateful streaming parser. Emits events for each new line."""

    def __init__(self) -> None:
        self._seen_keys: set[str] = set()

    def feed(self, line: str) -> Iterator[LogEvent]:
        for pat in PATTERNS:
            m = pat.regex.search(line)
            if not m:
                continue
            value, message = pat.extract(m)
            # De-dupe device_info and state events by key; errors and progress can repeat.
            if pat.kind in (EventKind.DEVICE_INFO, EventKind.STATE):
                if pat.key in self._seen_keys:
                    return
                self._seen_keys.add(pat.key)
            yield LogEvent(
                kind=pat.kind,
                key=pat.key,
                value=value,
                suggested_fix=pat.suggested_fix,
                message=message or value,
            )
            return  # first match wins per line

    def feed_lines(self, lines: Iterable[str]) -> Iterator[LogEvent]:
        for line in lines:
            yield from self.feed(line)

    def reset(self) -> None:
        self._seen_keys.clear()
