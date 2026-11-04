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
import argparse, base64, struct, sys, hashlib, urllib.request

# ---- embedded identity template (base64) ------------------------------------
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

# The open bootloader lives in its own repo and is published as a release asset.
BOOTLOADER_REPO = "hp197/x3-vcu-boot"
BOOTLOADER_ASSET = "x3-vcu-boot.bin"

def _release_base(version):
    if version in (None, "latest"):
        return f"https://github.com/{BOOTLOADER_REPO}/releases/latest/download"
    return f"https://github.com/{BOOTLOADER_REPO}/releases/download/{version}"

def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gt3fullflash"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def download_bootloader(version):
    """Fetch x3-vcu-boot.bin from the public releases and verify its SHA256."""
    base = _release_base(version)
    print(f"downloading bootloader from {BOOTLOADER_REPO} ({version})", file=sys.stderr)
    data = _http_get(f"{base}/{BOOTLOADER_ASSET}")
    try:
        sums = _http_get(f"{base}/SHA256SUMS.txt").decode()
        want = next((ln.split()[0] for ln in sums.splitlines()
                     if ln.strip().endswith(BOOTLOADER_ASSET)), None)
    except Exception as e:
        want = None
        print(f"  (SHA256SUMS unavailable, skipping verify: {e})", file=sys.stderr)
    if want:
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            sys.exit(f"bootloader sha256 mismatch: got {got}, expected {want}")
        print(f"  sha256 verified: {got[:16]}", file=sys.stderr)
    return data

def normalize_bootloader(data):
    """Pad a raw bootloader image up to the 4 KB region; reject anything larger."""
    if len(data) > 0x1000:
        sys.exit(f"bootloader too big: {len(data)} > 4096 bytes")
    return data + b"\xFF" * (0x1000 - len(data))

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
    ap.add_argument("--bootloader", help="use a local bootloader .bin (<=4 KB) instead of downloading")
    ap.add_argument("--bootloader-version", default="latest",
                    help="x3-vcu-boot release tag to download (default: latest)")
    ap.add_argument("--no-mirror", action="store_true",
                    help="leave slot1 blank instead of mirroring slot0")
    ap.add_argument("-o", "--out", required=True, help="output 128 KB .bin")
    a = ap.parse_args()

    if a.bootloader:
        boot, boot_src = open(a.bootloader, "rb").read(), f"custom ({a.bootloader})"
    else:
        boot, boot_src = download_bootloader(a.bootloader_version), f"downloaded {a.bootloader_version}"
    boot = normalize_bootloader(boot)
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
    print(f"  boot  @0x00000 : reset=0x{struct.unpack_from('<I',img,4)[0]:08x} ({boot_src})")
    print(f"  slot0 @0x01000 : {len(app)}B  SP=0x{struct.unpack_from('<I',img,0x1000)[0]:08x} "
          f"reset=0x{struct.unpack_from('<I',img,0x1004)[0]:08x}")
    print(f"  slot1 @0x10000 : {'mirror of slot0' if not a.no_mirror else 'blank'}")
    print(f"  ident @0x1F000 : serial={a.serial!r}  checksum=0x{stored:08x} "
          f"counter=0x{a.counter & 0xFFFF:04x}  -> {'VALID' if ok else 'INVALID'}")

if __name__ == "__main__":
    main()
