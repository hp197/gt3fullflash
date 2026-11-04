# gt3fullflash

A small tool that assembles a complete **128 KB flash image** for the
Segway-Ninebot **GT3** vehicle-control unit (VCU) — an Artery **AT32F415**
(ARM Cortex-M4) part from the Ninebot *x3* platform (GT3 / G3 / F3 / E3 / ZT3).

You give it a **slot0 application** (the firmware) plus your device **identity**
(serial, VCU id, MCU id, and the two 6-byte "rand" values), and it produces a
ready-to-flash `package.bin` with a working bootloader, both firmware slots, and
a **valid identity record** — so the controller boots and accepts its identity
instead of coming up unactivated.

This is useful when an AT32F415 VCU has been **mass-erased** (e.g. after a
failed/forced readout-protection unlock) and you need to rebuild a full image
from a bare application dump.

> ⚠️ For your **own** device only. Firmware modification can brick hardware,
> void warranties, and affect road-legality. No warranty — see the disclaimer.

## What it builds

| Offset | Size | Contents |
|-------:|-----:|----------|
| `0x00000` | 4 KB | Bootloader — model-agnostic A/B slot select + OTA/DFU loader |
| `0x01000` | ≤60 KB | **slot0** application (your input) |
| `0x10000` | ≤60 KB | **slot1** — mirror of slot0 (satisfies the bootloader's OTA mirror check) |
| `0x1F000` | 4 KB | **Identity record** (magic + serial + component table + valid checksum) |

The bootloader validates a slot purely structurally — it checks the slot's
stack-pointer is in SRAM (`SP & 0x2FFE0000 == 0x20000000`) and the reset vector
is sane; there is **no application CRC gate** on a cold boot. Mirroring the app
into both slots also satisfies the OTA "both slots identical" compare, so the
image boots regardless of the firmware's OTA-pending flag.

## Identity record format

Reverse-engineered from a live controller and a full platform dump:

```
+0x000  u16    magic 0x505C
+0x00C  u32    valid flag (1)
+0x010  2×u16  version (major.minor)
+0x020  14     serial, ASCII
+0x080  …      per-mode config / tune
+0x180  20     MCU component entry:  [id 12][0x01 0x00][rand 6]
+0x1B4  12     VCU component entry:  [id 12]
+0x1F4  u32    checksum  (see below)
+0x1FE  u16    record version counter (higher wins; erased records fail the checksum)
```

- **id encoding** — 12 bytes, each 16-bit halfword **byte-swapped** (app display
  order → flash order). The tool does this for you.
- **rand** — 6 bytes, stored verbatim (accepted as hex or ASCII on the CLI).
- **checksum** — the sum of the record's 256 signed `int16` halfwords, **excluding**
  the two halfwords at `+0x1F4`/`+0x1F6`, stored little-endian 32-bit at `+0x1F4`.

The firmware re-consolidates the component table at boot (it re-reads the MCU over
the internal bus), so those fields self-heal. A **valid record + the correct
serial** are what make the controller accept the identity. The VCU's own rand
field (`+0x30`) is firmware-managed and gets rewritten on boot — `--vcu-rand`
seeds it, but the firmware ultimately owns it.

## Requirements

- Python 3.8+ (standard library only).
- A slot0 application `.bin`.
- Your device identity values (read them from the Segway-Ninebot app's device
  info, or an earlier BLE/SWD dump).

The bootloader and an identity-template skeleton are embedded in the script, so
no reference dump is required. You can override the bootloader with your own 4 KB
blob via `--bootloader`.

## Install

No build step and no dependencies — it's a single self-contained script. Clone the
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

(the values above are placeholders — use your device's real ones)

| Option | Meaning |
|--------|---------|
| `--slot0` | slot0 application `.bin` (≤ 60 KB) |
| `--serial` | 14-character serial, ASCII |
| `--vcu-id` / `--mcu-id` | 12-byte id as 24 hex chars, in **app display order** |
| `--vcu-rand` / `--mcu-rand` | 6-byte rand, hex (`AABBCC…`) or ASCII |
| `--counter` | identity record version counter (default `0x8958`) |
| `--bootloader` | use your own 4 KB bootloader instead of the embedded one |
| `--no-mirror` | leave slot1 blank instead of mirroring slot0 |
| `-o` / `--out` | output file |

## Flashing

With OpenOCD and an AT32F415 SWD target (e.g. an ST-Link v2):

```bash
openocd -s <scripts> -f interface/stlink.cfg -f target/artery/at32f4x.cfg \
  -c "init" -c "reset halt" \
  -c "flash write_image erase package.bin 0x08000000 bin" \
  -c "verify_image package.bin 0x08000000 bin" \
  -c "reset run" -c "exit"
```

A full 128 KB write takes about a minute; `verify_image` confirms it. The part
must be readout-unprotected (FAP off) to accept the write — **do not re-enable
readout protection afterwards**, as the AT32F415 mass-erases main flash when
protection is restored.

## Caveats

- Calibration (throttle/brake) is **not** part of the rebuilt identity; expect to
  recalibrate after a bare recovery.
- The embedded template is a structural skeleton only; device-specific fields are
  overlaid from your CLI arguments or re-consolidated by the firmware at boot.
- This targets the AT32F415-based GT3 VCU. Other x3 members share the platform but
  verify the chip (`Device ID`/`UID`) before writing.

## Disclaimer

Provided as-is, with no warranty of any kind. Intended for repair and research on
hardware you own. You are responsible for complying with local laws and
regulations regarding vehicle modification and road use. The authors are not
liable for bricked hardware, voided warranties, or any other damage.

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) — see [LICENSE](LICENSE).
Free to share and adapt for **non-commercial** use with attribution; derivatives
must keep the same license.
