from collections.abc import Callable

from .findings import Finding, Severity
from .usb import DeviceMode, detect_mode, usb_id


def check_usb_state() -> Finding:
    mode = detect_mode()
    evidence = f"USB ID: {usb_id(mode)}"
    if mode == DeviceMode.BROM:
        return Finding(
            check_id="usb.state",
            title="USB connection",
            severity=Severity.OK,
            summary="Device in BROM mode — flashing operations are possible.",
            evidence=evidence,
        )
    if mode == DeviceMode.PRELOADER:
        return Finding(
            check_id="usb.state",
            title="USB connection",
            severity=Severity.WARN,
            summary=(
                "Device in Preloader mode. mtk-rescue will pass --crash to drop to BROM, "
                "but Preloader is less reliable for risky operations."
            ),
            evidence=evidence,
        )
    return Finding(
        check_id="usb.state",
        title="USB connection",
        severity=Severity.CRITICAL,
        summary=(
            "No MediaTek device detected. Connect the phone in BROM mode "
            "(power off, hold Vol Up + Vol Down, plug USB, keep holding 30s)."
        ),
        evidence=evidence,
    )


# TODO(mvp+1): check_gpt_health()       — read LU0 sector 1, verify "EFI PART" magic
# TODO(mvp+1): check_preloader_header() — read LU1 first 8 bytes, verify UFS_BOOT magic
# TODO(mvp+1): check_seccfg_state()     — dump seccfg, parse version + lock + hash type
# TODO(mvp+1): check_partition_manifest()— compare GPT partitions vs device expected set


Check = Callable[[], Finding]

CHECKS: list[Check] = [
    check_usb_state,
]
