# mtk-rescue

A GUI diagnostic and recovery tool for bricked MediaTek phones. Probes a phone in BROM
mode, identifies what's actually wrong (preloader mismatch, wiped GPT, corrupted seccfg,
full userdata, …) and offers the right recovery recipe instead of making you guess.

> **Status:** MVP scaffold. Today it covers one device (Redmi Note 8 Pro / begonia / MT6785),
> one diagnostic check (USB enumeration), and one read-only recipe (printgpt). Architecture
> is in place to add the rest incrementally.

## Why

Recovering a deeply-bricked MTK device with mtkclient is a process of probing the device,
reading state, comparing against expected values, and choosing one of half a dozen recipes —
each of which can make things worse if applied to the wrong state. mtk-rescue codifies that
diagnostic logic so the answer to "what should I run next?" comes from observed state, not
guesswork.

## Requirements

- Linux (USB / udev access; Windows/macOS later)
- Python 3.11+
- [mtkclient](https://github.com/bkerler/mtkclient) installed and known path
- `sudo` (mtkclient needs USB raw access)

## Install (dev)

```bash
git clone <repo-url> mtk-rescue
cd mtk-rescue
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
# Tell mtk-rescue where mtkclient lives:
export MTK_RESCUE_MTKCLIENT=/root/android-flash/mtkclient/mtk.py

mtk-rescue
# Or:
python -m mtk_rescue
```

## Architecture

```
mtk_rescue/
├── __main__.py            entry point
├── core/
│   ├── usb.py             USB device detection (BROM / Preloader / offline)
│   ├── findings.py        Finding dataclass + Severity enum
│   ├── checks.py          diagnostic check functions → Finding
│   ├── recipes.py         recovery recipes → command sequences
│   └── mtk.py             mtkclient subprocess wrapper with line streaming
├── devices/
│   └── begonia.py         device-specific expected state (one per supported device)
└── gui/
    ├── app.py             QApplication wiring
    └── main_window.py     central window: status, findings, recipes, log
```

## Roadmap

- [ ] More checks: GPT health, preloader header verify, seccfg state, partition manifest
- [ ] More recipes: GPT restore, preloader flash, seccfg lock/unlock, wipe userdata
- [ ] Device knowledge bases beyond begonia
- [ ] Auto-detect device from USB ID + IMEI / serial
- [ ] Bundle mtkclient (vendor as git submodule)
- [ ] Package as AppImage / Flatpak

## Safety

mtk-rescue uses mtkclient under the hood, which can brick devices if misused. Every recipe
that writes to the device requires explicit confirmation in the UI. Read-only recipes
(printgpt, dump partition, …) run without a prompt.
