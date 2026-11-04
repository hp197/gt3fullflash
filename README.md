# gt3fullflash

A small tool that assembles a complete 128 KB flash image for the Segway-Ninebot
GT3 vehicle-control unit (VCU), an Artery AT32F415 (ARM Cortex-M4) part from the
Ninebot x3 platform (GT3 / G3 / F3 / E3 / ZT3).

Give it a slot0 application (the firmware) plus your device identity (serial, VCU
id, MCU id, and the two 6-byte "rand" values). It writes a ready-to-flash
`package.bin` containing a working bootloader, both firmware slots, and a valid
identity record, so the controller boots and accepts its identity instead of
coming up unactivated.

This is useful when an AT32F415 VCU has been mass-erased (for example after a
failed or forced readout-protection unlock) and you need to rebuild a full image
from a bare application dump.

> For your own device only. Firmware modification can brick hardware, void
> warranties, and affect road-legality. No warranty; see the disclaimer.

## What it builds

| Offset | Size | Contents |
|-------:|-----:|----------|
| `0x00000` | 4 KB | Bootloader (model-agnostic A/B slot select + OTA loader) |
| `0x01000` | ≤60 KB | slot0 application (your input) |
| `0x10000` | ≤60 KB | slot1, a mirror of slot0 (satisfies the OTA mirror check) |
| `0x1F000` | 4 KB | Identity record (magic + serial + component table + checksum) |

The bootloader validates a slot structurally: it checks that the slot's stack
pointer lands in SRAM (`SP & 0x2FFE0000 == 0x20000000`) and that the reset vector
is sane. There is no application CRC check on a cold boot. Mirroring the app into
both slots also satisfies the OTA "both slots identical" compare, so the image
boots regardless of the firmware's OTA-pending flag.

## Requirements

- Python 3.8+ (standard library only).
- A slot0 application `.bin`.
- Your device identity values (from the Segway-Ninebot app's device info, or an
  earlier BLE/SWD dump).
- Internet access to download the bootloader (not needed if you pass `--bootloader`).

The bootloader is [x3-vcu-boot](https://github.com/hp197/x3-vcu-boot), an open,
from-source implementation of the x3 VCU bootloader. It brings up the clock, reads
the boot descriptor, commits a staged OTA (slot1 to slot0), then validates and
launches slot0.

By default the tool downloads the latest released build from the
[x3-vcu-boot](https://github.com/hp197/x3-vcu-boot) repo and verifies it against the
release's `SHA256SUMS.txt`. Use `--bootloader-version
<tag>` to pin a specific release, or `--bootloader <file>` to supply your own `.bin`
(≤ 4 KB) and skip the download. Only the identity-template skeleton is embedded in
the script.

## Install

No build step and no dependencies; it is a single self-contained script. Clone the
repo (or just download `build_package.py`) and run it with Python:

```bash
git clone https://github.com/hp197/gt3fullflash.git
cd gt3fullflash
python3 build_package.py --help
```

Optionally make it executable and put it on your `PATH`:

```bash
chmod +x build_package.py
```

## Usage

```bash
python3 build_package.py \
  --slot0   slot0_app.bin \
  --serial  0123456789ABCD \
  --vcu-id  001122334455667788990011 \
  --mcu-id  AABBCCDDEEFF001122334455 \
  --vcu-rand AABBCCDDEEFF \
  --mcu-rand 313233343536 \
  -o package.bin
```

(the values above are placeholders; use your device's real ones)

| Option | Meaning |
|--------|---------|
| `--slot0` | slot0 application `.bin` (≤ 60 KB) |
| `--serial` | 14-character serial, ASCII |
| `--vcu-id` / `--mcu-id` | 12-byte id as 24 hex chars, in app display order |
| `--vcu-rand` / `--mcu-rand` | 6-byte rand, hex (`AABBCC…`) or ASCII |
| `--counter` | identity record version counter (default `0x8958`) |
| `--bootloader` | use a local bootloader `.bin` (≤ 4 KB) instead of downloading |
| `--bootloader-version` | which x3-vcu-boot release tag to download (default `latest`) |
| `--no-mirror` | leave slot1 blank instead of mirroring slot0 |
| `-o` / `--out` | output file |

## Flashing

Flash the full 128 KB `package.bin` to `0x08000000` with **x3utils**, the
ScooterHacking x3 toolkit. It drives the AT32F415 over SWD, handles connecting to the
VCU (including a readout-protected part via its power-on catch), validates the image,
and writes it. This is the recommended path.

OpenOCD is a manual fallback if you would rather flash it yourself, for example with
an ST-Link v2:

```bash
openocd -s <scripts> -f interface/stlink.cfg -f target/artery/at32f4x.cfg \
  -c "init" -c "reset halt" \
  -c "flash write_image erase package.bin 0x08000000 bin" \
  -c "verify_image package.bin 0x08000000 bin" \
  -c "reset run" -c "exit"
```

A full 128 KB write takes a few seconds and `verify_image` confirms it. The part
must be readout-unprotected (FAP off) to accept the write. Do not re-enable readout
protection afterwards: the AT32F415 mass-erases main flash when protection is
restored.

## Caveats

This targets the AT32F415-based GT3 VCU. Other x3 members share the platform, but
verify the chip (`Device ID` / `UID`) before writing.

## Disclaimer

Provided as-is, with no warranty of any kind. Intended for repair and research on
hardware you own. You are responsible for complying with local laws and regulations
regarding vehicle modification and road use. The authors are not liable for bricked
hardware, voided warranties, or any other damage.

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/); see
[LICENSE](LICENSE). Free to share and adapt for non-commercial use with attribution;
derivatives must keep the same license.
