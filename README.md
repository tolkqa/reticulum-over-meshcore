# Reticulum over MeshCore

Reticulum interface for using [MeshCore](https://github.com/ripplebiz/MeshCore) over USB or BLE.

> **Project status:** very early and experimental.
> Expect bugs, unstable behavior, and compatibility issues between dependency versions.

## Quick Start

```bash
git clone https://github.com/tolkqa/reticulum-over-meshcore.git
cd reticulum-over-meshcore
pip install -r requirements.txt
python install.py
```

`install.py` copies the interface to `~/.reticulum/interfaces/`.

## Reticulum Config

Open `~/.reticulum/config` and add **one** option under `[interfaces]`:

USB:
```ini
[[MeshCore LoRa]]
  type = MeshCoreInterface
  enabled = true
  port = /dev/ttyUSB0
  speed = 115200
```

BLE:
```ini
[[MeshCore LoRa]]
  type = MeshCoreInterface
  enabled = true
  ble_address = AA:BB:CC:DD:EE:FF
```

How to find values:
- USB port: `ls /dev/ttyUSB*` (Linux) or `ls /dev/cu.*` (macOS)
- BLE address:

```bash
python3 -c "
import asyncio
from bleak import BleakScanner

async def scan():
    for d in await BleakScanner.discover(5):
        print(d.address, d.name)

asyncio.run(scan())
"
```

## Run and Check

```bash
rnsd -v
rnstatus
```

If everything is OK, `MeshCore LoRa` should be `Up`.

## Disclaimer

This project is an independent implementation and is not affiliated with Reticulum or MeshCore maintainers.
The names `Reticulum` and `MeshCore` are used only to describe compatibility.

## Licenses

- This repository: `MIT` (see `LICENSE`)
- Reticulum (`rns`): [`Reticulum License`](https://raw.githubusercontent.com/markqvist/Reticulum/master/LICENSE) (additional restrictions, not MIT)
- MeshCore firmware: [`MIT`](https://raw.githubusercontent.com/ripplebiz/MeshCore/master/license.txt)
- Python `meshcore` library (fdlamotte/meshcore_py): [`MIT`](https://raw.githubusercontent.com/fdlamotte/meshcore_py/main/LICENSE)

By using this project, you also accept dependency license terms.

