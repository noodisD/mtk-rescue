"""Expected state for Redmi Note 8 Pro (codename begonia, MT6785, Helio G90T, UFS)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceProfile:
    codename: str
    chipset: str
    storage: str
    preloader_header: bytes
    ufs_lu_sizes: dict[str, int]
    seccfg_hash_type: str  # "sej" (hardware) or "sw"
    expected_partitions: tuple[str, ...]


PROFILE = DeviceProfile(
    codename="begonia",
    chipset="MT6785",
    storage="ufs",
    preloader_header=b"UFS_BOOT",
    ufs_lu_sizes={"boot1": 0x400000, "boot2": 0x400000},
    seccfg_hash_type="sej",
    expected_partitions=(
        "recovery", "misc", "para", "expdb", "frp", "vbmeta", "nvcfg", "nvdata",
        "metadata", "persist", "protect1", "protect2", "seccfg", "otp", "sec1",
        "proinfo", "efuse", "nvram", "md1img", "boot_para", "spmfw", "audio_dsp",
        "scp1", "scp2", "sspm_1", "sspm_2", "cam_vpu1", "cam_vpu2", "cam_vpu3",
        "gz1", "gz2", "lk", "lk2", "boot", "logo", "dtbo", "tee1", "tee2",
        "vendor", "system", "cache", "gsort", "oem_misc1", "exaid", "cust",
        "userdata",
    ),
)
