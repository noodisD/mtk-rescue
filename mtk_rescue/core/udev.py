"""Udev rule installation + runtime kernel-driver detach for MTK devices.

Why this matters: on Linux, when an MTK phone enumerates in BROM mode (0e8d:0003),
the kernel auto-binds cdc_acm (and sometimes `option`) as a serial modem driver.
This:
  - blocks libusb / mtkclient from claiming the device on some kernels
  - eats ~500ms of the ~4-second BROM watchdog window

The udev rule below tells the kernel to grant userspace access and unbinds those
drivers immediately on device-add. The runtime fallback does the same via sysfs
in case the rule isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-mtk-rescue.rules")

UDEV_RULE_CONTENTS = """\
# Installed by mtk-rescue. Grants user access to MediaTek BROM/Preloader devices
# and unbinds kernel serial drivers that would otherwise claim them.

SUBSYSTEM=="usb", ATTR{idVendor}=="0e8d", MODE="0666", TAG+="uaccess"

ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0e8d", \\
  RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/cdc_acm/unbind 2>/dev/null; \\
                    echo -n %k > /sys/bus/usb/drivers/option/unbind 2>/dev/null; \\
                    true'"
"""


def rule_installed() -> bool:
    return UDEV_RULE_PATH.exists() and UDEV_RULE_PATH.read_text() == UDEV_RULE_CONTENTS


def _privileged_runner() -> list[str] | None:
    """Pick the best privilege-escalation tool available. None = already root."""
    if os.geteuid() == 0:
        return []
    for cmd in ("pkexec", "sudo"):
        if shutil.which(cmd):
            return [cmd]
    return None


def install_rule() -> tuple[bool, str]:
    """Install the udev rule. Returns (success, message)."""
    runner = _privileged_runner()
    if runner is None:
        return False, "Neither pkexec nor sudo is available."

    try:
        script = (
            f"cat > {UDEV_RULE_PATH} <<'EOF'\n{UDEV_RULE_CONTENTS}EOF\n"
            "udevadm control --reload-rules && udevadm trigger"
        )
        cmd = [*runner, "sh", "-c", script]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return False, f"Install failed: {result.stderr or result.stdout}"
        return True, f"Installed {UDEV_RULE_PATH}"
    except subprocess.TimeoutExpired:
        return False, "Privileged command timed out."
    except Exception as exc:  # noqa: BLE001
        return False, f"Install failed: {exc}"


def detach_kernel_drivers() -> list[str]:
    """Runtime fallback: walk /sys and unbind cdc_acm/option from any 0e8d device.

    Returns a list of (driver, kpath) strings describing what was detached.
    Silently ignores failures — this is a best-effort fallback.
    """
    detached: list[str] = []
    for driver in ("cdc_acm", "option"):
        bind_dir = Path(f"/sys/bus/usb/drivers/{driver}")
        if not bind_dir.is_dir():
            continue
        for entry in bind_dir.iterdir():
            if not entry.is_symlink():
                continue
            # entry name is e.g. "3-1:1.0" — get the parent usb device's idVendor
            device_dir = entry.resolve().parent
            vendor_file = device_dir / "idVendor"
            try:
                vendor = vendor_file.read_text().strip()
            except OSError:
                continue
            if vendor != "0e8d":
                continue
            try:
                (bind_dir / "unbind").write_text(entry.name)
                detached.append(f"{driver}:{entry.name}")
            except OSError:
                pass
    return detached
