"""Smoke tests for LogParser against real mtkclient output snippets."""

from mtk_rescue.core.log_parser import EventKind, LogParser


# A trimmed version of the user's own mtkclient session output — verbatim from
# the conversation history; perfect ground truth for parser regression tests.
SAMPLE_OUTPUT = """\
Preloader - Detected regular mode !
Preloader - 	CPU:			MT6785(Helio G90)
Preloader - 	HW version:		0x0
Preloader - 	WDT:			0x10007000
Preloader - Disabling Watchdog...
Preloader - HW code:			0x813
Preloader - Target config:		0xe7
Preloader - 	SBC enabled:		True
Preloader - 	SLA enabled:		True
Preloader - ME_ID:			38042185DA5E85BB0CA1FED26092B169
Preloader - SOC_ID:			ACD4AE0820E78233152F0E4D04FBEC17EF9DF12D451902E1A17A8B5C57A4FFB8
Preloader - [LIB]: Auth file is required. Use --auth option.
DaHandler - Device is protected.
DaHandler - Device is in BROM-Mode. Bypassing security.
Exploitation - Kamakiri Run
PLTools - Successfully sent payload: /home/x/mt6785_payload.bin
DAXFlash - Uploading xflash stage 1 from MTK_DA_V5.bin
Mtk - Patched "hash_check" in preloader
DAXFlash - Successfully uploaded stage 1, jumping ..
DAXFlash - Successfully received DA sync
DAXFlash - Sending emi data ...
DAXFlash - DRAM setup failed: unpack requires a buffer of 12 bytes. Use mtk.py with --preloader preloader.bin !
DaHandler - [LIB]: Failed to upload da.
"""


def _events(lines: str) -> list:
    p = LogParser()
    out = []
    for line in lines.splitlines():
        out.extend(p.feed(line))
    return out


def test_extracts_device_info():
    events = _events(SAMPLE_OUTPUT)
    by_key = {e.key: e for e in events if e.kind == EventKind.DEVICE_INFO}
    assert by_key["cpu"].value == "MT6785(Helio G90)"
    assert by_key["hw_code"].value == "0x813"
    assert by_key["target_config"].value == "0xe7"
    assert by_key["sbc_enabled"].value == "True"
    assert by_key["sla_enabled"].value == "True"
    assert by_key["me_id"].value == "38042185DA5E85BB0CA1FED26092B169"
    assert by_key["soc_id"].value.startswith("ACD4AE0820E78233")


def test_detects_state_transitions():
    events = _events(SAMPLE_OUTPUT)
    states = {e.key for e in events if e.kind == EventKind.STATE}
    assert "watchdog_disabled" in states
    assert "kamakiri" in states
    assert "payload_sent" in states
    assert "da_stage1" in states
    assert "da_sync" in states


def test_detects_dram_error_with_auto_fix():
    events = _events(SAMPLE_OUTPUT)
    errors = [e for e in events if e.kind == EventKind.ERROR]
    keys = {e.key for e in errors}
    assert "dram_needs_preloader" in keys
    dram_err = next(e for e in errors if e.key == "dram_needs_preloader")
    assert dram_err.suggested_fix == "set_preloader"
    assert "preloader" in dram_err.message.lower()


def test_detects_auth_required_error():
    events = _events(SAMPLE_OUTPUT)
    errors = {e.key for e in events if e.kind == EventKind.ERROR}
    assert "auth_required" in errors


def test_state_keys_deduped_within_session():
    """The watchdog-disabled line appears once. If it appeared twice, we'd want one event."""
    lines = "Preloader - Disabling Watchdog...\nPreloader - Disabling Watchdog...\n"
    events = list(LogParser().feed_lines(lines.splitlines()))
    watchdog_events = [e for e in events if e.key == "watchdog_disabled"]
    assert len(watchdog_events) == 1


def test_partition_not_found_captures_name():
    line = "DaHandler - [LIB]: Error: Couldn't detect partition: preloader"
    events = list(LogParser().feed(line))
    assert events
    assert events[0].kind == EventKind.ERROR
    assert events[0].key == "partition_not_found"
    assert "preloader" in events[0].value
