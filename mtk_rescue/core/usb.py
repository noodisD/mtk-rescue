from enum import Enum

try:
    import usb.core
except ImportError:  # pragma: no cover
    usb = None  # type: ignore


MTK_VENDOR = 0x0E8D
MTK_BROM_PID = 0x0003
MTK_PRELOADER_PID = 0x2000


class DeviceMode(Enum):
    BROM = "brom"
    PRELOADER = "preloader"
    OFFLINE = "offline"


def detect_mode() -> DeviceMode:
    if usb is None:
        return DeviceMode.OFFLINE
    if usb.core.find(idVendor=MTK_VENDOR, idProduct=MTK_BROM_PID) is not None:
        return DeviceMode.BROM
    if usb.core.find(idVendor=MTK_VENDOR, idProduct=MTK_PRELOADER_PID) is not None:
        return DeviceMode.PRELOADER
    return DeviceMode.OFFLINE


def usb_id(mode: DeviceMode) -> str:
    return {
        DeviceMode.BROM: f"{MTK_VENDOR:04x}:{MTK_BROM_PID:04x}",
        DeviceMode.PRELOADER: f"{MTK_VENDOR:04x}:{MTK_PRELOADER_PID:04x}",
        DeviceMode.OFFLINE: "—",
    }[mode]
