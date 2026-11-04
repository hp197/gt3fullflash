#!/usr/bin/env python3
"""
build_package.py -- Segway-Ninebot x3 (GT3 / AT32F415) full-flash packager.

Takes a slot0 application .bin plus a device identity (serial, VCU id, MCU id,
VCU rand, MCU rand) and produces a complete 131072-byte (128 KB) flash image
ready to write over SWD:

  0x00000  bootloader (4 KB, model-agnostic A/B + DFU loader)
  0x01000  slot0 app   (your input .bin)
  0x10000  slot1 app   (mirror of slot0 -- satisfies the bootloader's OTA mirror check)
  0x1F000  identity record (0x200 bytes: magic + serial + component table + valid checksum)
           the records at 0x1F400 / 0x1F800 / 0x1FC00 are left blank; the firmware
           wear-levels fresh copies into them at runtime.

Flash it (OpenOCD, AT32F415 target):
  openocd ... \
    -c "init" -c "reset halt" \
    -c "flash write_image erase package.bin 0x08000000 bin" \
    -c "verify_image package.bin 0x08000000 bin" \
    -c "reset run" -c "exit"

------------------------------------------------------------------------------
Identity record format:

  +0x000  u16   magic 0x505C
  +0x00C  u32   valid flag (1)
  +0x010  2xu16 version (major.minor)
  +0x020  14    serial, ASCII
  +0x080  ...   per-mode config / tune
  +0x180  20    MCU component entry:  [id 12][0x01 0x00][rand 6]
  +0x1B4  12    VCU component entry:  [id 12]
  +0x1F4  u32   checksum (see below)
  +0x1FE  u16   record version counter (higher wins; blank/erased records fail the checksum)

  id encoding : 12 bytes, each 16-bit halfword byte-swapped (app display order -> flash order)
  rand        : 6 bytes, stored verbatim
  checksum    : sum of the 256 signed int16 halfwords of the record, EXCLUDING the two
                halfwords at +0x1F4 and +0x1F6; stored little-endian 32-bit at +0x1F4.

The firmware re-consolidates the component table on boot (it re-reads the MCU over the
internal bus), so those fields self-heal. A valid record + the correct serial are what
make the controller accept the identity and leave the "not activated / not paired" state.
The VCU's own rand field (+0x30) is firmware-managed and gets rewritten on boot; --vcu-rand
seeds it but the firmware owns it.
------------------------------------------------------------------------------
"""
import argparse, base64, struct, sys, hashlib

# ---- embedded blobs (base64) --------------------------------------------------
# Bootloader: the open x3-vcu-boot replica (BSD-3 Artery BSP + CMSIS, built from source
# at https://github.com/hp197/x3-vcu-boot). Model-agnostic: brings up the clock, reads the
# boot descriptor, commits a staged OTA (slot1 -> slot0), then validates the slot0 SP and
# launches it. 3028 B of code, padded with 0xFF to the 4 KB region. Override via --bootloader.
BOOTLOADER = base64.b64decode("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s/v48Aj7EDNF6gNEAPsK+qJFCNke6wQEAPH/M2vSokVp2QI4dERA6glAoPsCiaTrCgRMRcJGS0ZU01HQAC5p0LHrCgVk6wMEBPoM/P1A/EBM6gUFxukAVAAnR+fC8SADIPoD95VAAfoC8CH6A/NP6hVOOEMBDLP7/vcf+oX4DvsXM0HqA0EH+wjzi0IE+gL0B9lpGAfx/zwv0otCLdkCPylEyxqBsrP7/vAO+xAzQeoDQQD7CPOLQgfZaRgA8f88F9KLQhXZAjgpRMkaQOoHRzvnN0YwRgnnB0bn5hhG++ZBRavSuOsCCmnrDgIBOBNGpOdgRunnGEaV52dG0eeBRnznAjgsREfnrPECDCtEL+c4RgjnN0bp5nBHAL8AAAAAAAAAAAECAwQGBwgJ+LUAv/i8CLyeRnBH+LUAv/i8CLyeRnBHABJ6AP///////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////w==")
# Identity template: structural skeleton ONLY (magic / flag / version / config).
# All device-identifying fields are zeroed; your values are overlaid at build time.
TEMPLATE   = base64.b64decode("XFAAAAAAAAAAAAAAAQAAAAMAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABzMgAAAwAFBQAFEBkAYxAZAGMFAAcAAAAAAQAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAgAAAAAAAAAcFCMZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAV4k=")

ID_BASE   = 0x1F000
OFF_SER   = 0x20
OFF_VRAND = 0x30
OFF_MCU   = 0x180
OFF_VCU   = 0x1B4
OFF_CKLO  = 0x1F4
OFF_CKHI  = 0x1F6
OFF_CTR   = 0x1FE
SLOT_MAX  = 0xF000     # 60 KB per slot

def hexbytes(s, n):
    b = bytes.fromhex(s.replace(" ", "").replace(":", ""))
    if len(b) != n:
        sys.exit(f"expected {n} bytes ({n*2} hex chars), got {len(b)}")
    return b

def swap16pairs(b):                 # app display order <-> flash order
    out = bytearray(b)
    for i in range(0, len(b) - 1, 2):
        out[i], out[i+1] = b[i+1], b[i]
    return bytes(out)

def rand6(s):
    s2 = s.replace(" ", "")
    try:
        b = bytes.fromhex(s2)
        if len(b) == 6:
            return b
    except ValueError:
        pass
    b = s.encode()
    if len(b) != 6:
        sys.exit(f"rand must be 6 bytes (hex or ASCII): {s!r}")
    return b

def checksum(rec):
    return sum(struct.unpack_from('<h', rec, i*2)[0]
               for i in range(0x100) if i*2 not in (OFF_CKLO, OFF_CKHI)) & 0xFFFFFFFF

def build_record(serial, vcu_id, mcu_id, vcu_rand, mcu_rand, counter):
    rec = bytearray(TEMPLATE)
    rec[OFF_SER:OFF_SER+14]       = (serial.encode() + b"\x00"*14)[:14]
    rec[OFF_MCU:OFF_MCU+12]       = swap16pairs(mcu_id)
    rec[OFF_MCU+12:OFF_MCU+14]    = b"\x01\x00"
    rec[OFF_MCU+14:OFF_MCU+20]    = mcu_rand
    rec[OFF_VCU:OFF_VCU+12]       = swap16pairs(vcu_id)
    rec[OFF_VRAND:OFF_VRAND+6]    = vcu_rand
    struct.pack_into('<H', rec, OFF_CTR, counter & 0xFFFF)
    struct.pack_into('<I', rec, OFF_CKLO, 0)
    s = checksum(rec)
    struct.pack_into('<H', rec, OFF_CKLO, s & 0xFFFF)
    struct.pack_into('<H', rec, OFF_CKHI, (s >> 16) & 0xFFFF)
    return bytes(rec)

def main():
    ap = argparse.ArgumentParser(
        description="Build a 128 KB Segway-Ninebot x3 (GT3) full-flash package.")
    ap.add_argument("--slot0", required=True, help="slot0 application .bin")
    ap.add_argument("--serial", required=True, help="14-char serial, e.g. 0123456789ABCD")
    ap.add_argument("--vcu-id", required=True, help="12-byte VCU id, hex (app order), 24 hex chars")
    ap.add_argument("--mcu-id", required=True, help="12-byte MCU id, hex (app order), 24 hex chars")
    ap.add_argument("--vcu-rand", required=True, help="6-byte VCU rand, hex or ASCII")
    ap.add_argument("--mcu-rand", required=True, help="6-byte MCU rand, hex or ASCII")
    ap.add_argument("--counter", type=lambda x: int(x, 0), default=0x8958,
                    help="identity record version counter (default 0x8958)")
    ap.add_argument("--bootloader", help="override the embedded bootloader with a 4 KB .bin")
    ap.add_argument("--no-mirror", action="store_true",
                    help="leave slot1 blank instead of mirroring slot0")
    ap.add_argument("-o", "--out", required=True, help="output 128 KB .bin")
    a = ap.parse_args()

    boot = open(a.bootloader, "rb").read() if a.bootloader else BOOTLOADER
    if len(boot) != 0x1000:
        sys.exit(f"bootloader must be exactly 4096 bytes, got {len(boot)}")
    app = open(a.slot0, "rb").read()
    if len(app) > SLOT_MAX:
        sys.exit(f"slot0 too big: {len(app)} > {SLOT_MAX}")

    vcu_id, mcu_id = hexbytes(a.vcu_id, 12), hexbytes(a.mcu_id, 12)
    vcu_rnd, mcu_rnd = rand6(a.vcu_rand), rand6(a.mcu_rand)

    img = bytearray(b"\xFF" * 131072)
    img[0x00000:0x01000] = boot
    img[0x01000:0x01000+len(app)] = app
    if not a.no_mirror:
        img[0x10000:0x10000+len(app)] = app
    rec = build_record(a.serial, vcu_id, mcu_id, vcu_rnd, mcu_rnd, a.counter)
    img[ID_BASE:ID_BASE+0x200] = rec
    open(a.out, "wb").write(img)

    stored = (struct.unpack_from('<H', rec, OFF_CKHI)[0] << 16) | struct.unpack_from('<H', rec, OFF_CKLO)[0]
    ok = checksum(rec) == stored
    print(f"wrote {a.out} ({len(img)} bytes)  sha256={hashlib.sha256(img).hexdigest()[:16]}")
    print(f"  boot  @0x00000 : reset=0x{struct.unpack_from('<I',img,4)[0]:08x}"
          f"{' (custom)' if a.bootloader else ' (embedded)'}")
    print(f"  slot0 @0x01000 : {len(app)}B  SP=0x{struct.unpack_from('<I',img,0x1000)[0]:08x} "
          f"reset=0x{struct.unpack_from('<I',img,0x1004)[0]:08x}")
    print(f"  slot1 @0x10000 : {'mirror of slot0' if not a.no_mirror else 'blank'}")
    print(f"  ident @0x1F000 : serial={a.serial!r}  checksum=0x{stored:08x} "
          f"counter=0x{a.counter & 0xFFFF:04x}  -> {'VALID' if ok else 'INVALID'}")

if __name__ == "__main__":
    main()
