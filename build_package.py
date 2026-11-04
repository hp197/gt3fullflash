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
Identity record format (reverse-engineered):

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
# Bootloader: a 4 KB x3 bootloader (model-agnostic: validates slot SP + reset vector,
# A/B slot select, OTA/DFU). Replace with your own via --bootloader if you prefer.
BOOTLOADER = base64.b64decode("kAgAIA0CAAgVAgAIFwIACBkCAAgbAgAIHQIACAAAAAAAAAAAAAAAAAAAAAAfAgAIIQIACAAAAAAjAgAIJQIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgnAgAIJwIACCcCAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAnAgAIJwIACAAAAAAnAgAIJwIACAAAAAAAAAAAJwIACCcCAAgnAgAIJwIACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAnAgAIAAAAAAAAAAAnAgAIJwIACAAAAAAAAAAAAAAAACcCAAgA8AL4APA6+AqgkOgADIJEg0Sq8QEH2kUB0QDwL/iv8gkOuugPABPwAQ8Yv/saQ/ABAxhHGAwAADgMAAAQOiS/eMh4wfrYUgckvzDIMMFEvwRoDGBwRwAAACMAJAAlACYQOii/eMH72FIHKL8wwUi/C2BwRx+1H70QtRC9APCq+BFG//f3/wDwrfwA8Mj4A7T/9/L/A7wA8IP6AAAJSIBHCUgAR/7n/uf+5/7n/uf+5/7n/uf+5/7nBEgFSQVKBktwRwAAdQYACHEBAAiQAgAgkAgAIJAEACCQBAAgcEdT6gIMAPBpgC3p8EtP8AAGACsfv7P6g/UD+gX0JPoF9l5AEr8WQ7L6gvUC+gX0xfEgBR6/IvoF/ETqDAQgNVbqBExP6hREGL9kHE/wAAhP8AAJkEJx6wMMOdMAKRm/sfqB9wH6B/aw+oD3APoH9sfxIAcevyD6B/xG6gwGIDe2+/T8p+sFBxA/B/AfC8vxIAYM+gv7LPoG9kS/s0YAJiAvpL9eRk/wAAtb6gYMCL9P8AELGesLCav7AnxI6wYIwBsG+wLMC/sDzHHrDAHB5wtGAkZBRkhGvejwixO1T/AAAE/wAAGv8wCAvegcQHBHcEdwR3BHcEd1RgDwK/iuRgUAaUZTRiDwBwCFRhiwILX/92T/veggQE/wAAZP8AAHT/AACE/wAAsh8AcBrEas6MAJrOjACazowAms6MAJjUZwRxC1BEav8wCAIEa96BBA//cvvwBIcEcwAgAgELUEIADw5PwHSABoB0mw+/HwB0kIYAhGAGhP9HpxSEMESQhgEL0AADgAACBAQg8ALAAAIDAAACABtQAgAJnB8wkCGrEBIgLrkSAA4IgKCL0t6fBHBUYORgAnT/ADCKlGAPCY+xQgAPAd+zBG//fk/wdGACQL4E/0gGEB+wSQAPBp+4BGuPEDDwDQAuBkHLxC8dMAvwDwKPu8QgLQACC96PCHASD75xC1A0YAIALgHFwMVEAckEL60xC9LenwR4BGiUYURkZGT/ADCgDwY/tPRgAlEOA5iDBGAPDq+oJGuvEDDwDQCeAwiDmIiEIA0ATgthy/HK0cpULs0wC/APD0+qVCAtIAIL3o8IcBIPvnAAAQtRJIQGi4sXK2APAj+Gi5ACAOSUhgAPCd+A1IAPCG+EC5ISEfIADwq/gD4CEhHyAA8Kb4YrYH4AVIAPB3+Bi5ISEfIADwnPgQvQAAPAAAIAAQAAhwtQAkACUAJhpIgGgIuQEgcL0ZSABoGUkIQLDxAF8B0Akg9ecTSIFoFUgA8C34CLECIO3nEk0QTgAkFOBP9IByEEkwRv/3ff9P9IByDUkoRv/3gf8IuQMg2ucG9YB2BfWAdQT1gHQDSIBooELm2AAgzucAADwAACAAAAEIAAD+LwAQAAhIAAAgcLUERg1GACZytilGIEb/9yn/ALkCJmK2MEZwvRC1DCIISQlI//dJ/wZIAGgHSQloiEIF0ANIBUsOyw7AAPAc+BC9AAA8AAAgAPgBCKANAAhwtQRGIGgHSQhAsPEAXwfRZWggaIDzCIgAv6hHASBwvQAg/OcAAP4vELUAJHK2DCEGSP/37/4ERiyxDCIESQNI//cd/wRGYrYgRhC9APgBCDwAACAgKQLRCkoQcAPgISkB0QlKEHAHShJ4B0sbeJpCAt0EShJ4AeADShJ4A0sacnBHAAAoAAAgKQAAICICACAiSMBpIPCAUADxgFAfSchhH0gAeEDwBAAdSVA5gfhQABpIwGkg8IBQGEnIYQhGAGgg8AEAQBwIYAC/FEgAaMDzQAAAKPnQEUhAaCDwAwAPSUhgAL8NSEBowPOBAAAo+dEKSABoC0kIQAhJCGAAIEhgQfYQcMhiT/SAEAhjT/QfAIhgyAMESQhgcEcAAAAQAkBQcABA///y/gjtAOAAv3BHA0lJaGDzBxEBSlFgcEcAAAAQAkADSUloYPMKIQFKUWBwRwAAABACQANJSWhg880hAUpRYHBHAAAAEAJAASgH0QdJSW0h8DABMDEFSlFlBeADSUltIfAwAQFKUWVwRwAAABACQAUoJ9Lf6ADwAwoRGB8AE0oSaGHzAAIRSxpgHOAPShJoYfMQQg1LGmAV4AxKEmhh8xhiCksaYA7gCEoSamHzAAIGSxpiB+AFSlJqYfMAAgNLWmIA4AC/AL9wRwAAABACQBC1AUYAIAlLA+sRQhJoAfAfBAEjo0AaQAHwHwQBI6NAmkIA0ADgASAQvQAAABACQDC1ACQAJQDgZBwRIP/34P8BKALQtPVAX/bTESD/99j/ASgB0AAlAOABJShGML0AADC1AkYAIAAjcrk1TGRoJPSANDNNbGAzSCxGJGsk8AB0BPEAdCxjGeAtTGRoJPSANAT1gDQqTWxgASoG0SpILEZkaCT0ADRsYAjgKEgkTGRoJPQANAT1ADQhTWxgJEygQgTZJEygQgHSACMm4CJMoEIE2SJMoEIB0gEjHuAgTKBCBNkgTKBCAdICIxbgHUygQgTZHUygQgHSAyMO4BtMoEIE2RtMoEIB0gQjBuAYTKBCA9kYTKBCANIFIwlMZGhh85VEB01sYA0JBUxkaGXzXnQDTWxgLEbkamPzGmTsYjC9ABACQAAJPQAAJPQAABJ6AGCCOwBAS0wAgFhPABBeXwCUNXcAEBt/ACC8vgAoa+4AMNc9Adjp3AEWSABoIPABAEAcFEkIYAC/EkgAaMDzQAAAKPnQD0hAaCDwAwANSUhgAL8MSEBowPOBAAAo+dEJSABoCUkIQAdJCGAAIEhgQfYQcMhiT/SAEAhjT/QfAIhgcEcAAAAQAkD///L+A0lJaGDzAQEBSlFgcEcAAAAQAkACSEBowPOBAHBHAAAAEAJAACEx4DIoCt0ZShJoMiNaQ0/w4CNaYaDxMgKQsgbgFEoSaEJDT/DgI1phACAAIk/w4COaYRpGEmlC8AECGmEAv0/w4CIRaQHwAQIasQH0gDIAKvXQT/DgIhJpIvABAk/w4CMaYQAimmEAKMvRcEcAADAAACABSchgcEcAAAAgAkBwtQRGDUYDJgpIAGkg8AEAQBwISQhhJYBP9IAQAPAw+AZGBEgAaSDwAQACSQhhMEZwvQAAACACQANIAGkg8IAAgDABSQhhcEcAIAJAAyALScloAfABAQmxACAO4AdJyWjB84ABCbEBIAfgBEnJaMHzABEJsQIgAOADIHBHACACQAC1AkYDI//34f8DRgPg//fd/wNGUh4LuQAq+NECuQQjGEYAvTC1BEYDJQ1IAGkg8AIAgBwKSQhhCEZEYQBpIPBAAEAwCGFIBP/32v8FRgRIAGkg8AIAAkkIYShGML0AAAAgAkACSANJSGADSEhgcEcjAWdFACACQKuJ780A8Cr4//cm/AAgEEkIcv/3Jf3/96f8GOAMSAB6iLEAv7/zT48KSABoAPTgYAlJCEMAHQdJCGC/80+PAL8AvwC//edP9Hpw//cd/+XnIgIAIAztAOAAAPoFELX/98/+FCAZSQhgASEIRv/33P0Av//3If4AKPvQByEBIP/3M/4BIQIg//fP/QC/GSD/9/v9ASj60QAg//eU/QQg//el/QQg//eY/QEg//ep/QIg//fY/gC///ff/gIo+9EAIP/3nv0A8AT4EL0AAAAgAkAt6f9fACQAJQAmoUaiRqNGACADkAKQAZD/98b+AJAAmCCxASgU0AIodNEV4ENIQG3A80AgQLFBSABrwPNAYBixP0hASQhgAuA/SD5JCGBl4D5IPEkIYGHgOEhAaMDzAEY2SMBqwA8guzRIQGjA84NEMkhAaMDzQXUNuQ8sA9EoAUAcBEQA4KQcJrkxSGBDLUkIYD3gKUhAaMDzQEAgsSpIYEMoSQhgM+AoSGBDJUkIYC7gIkjAasDzAxADkB9IwGrA8wggApAdSMBqAPAHAAGQFrnf+HywCeAYSEBowPNAQBCx3/hksAHg3/hgsAEhAZgB+gDwA5kA+wH8Apir+wBwAUZiRgAjOEb/95H6DUkIYATg/+cMSAtJCGAAvwC/B0hAaMDzAxkLSBD4CaAFSABoIPoK8ANJCGC96P+fABACQABs3AI4AAAgABJ6AAAk9AAACT0ArA0ACAQoCNFP8OAhCWlB8AQBT/DgIhFhB+BP8OAhCWkh8AQBT/DgIhFhcEdaUAAAAAAAAAAAAAAAAAAAAAAAAAECAwQGBwgJOBQACAAAACA8AAAArAEACHQUAAg8AAAgVAgAAMgBAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==")
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
