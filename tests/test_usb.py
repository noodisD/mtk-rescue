from mtk_rescue.core.usb import DeviceMode, usb_id


def test_usb_id_strings():
    assert usb_id(DeviceMode.BROM) == "0e8d:0003"
    assert usb_id(DeviceMode.PRELOADER) == "0e8d:2000"
    assert usb_id(DeviceMode.OFFLINE) == "—"
