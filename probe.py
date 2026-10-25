"""Probe the AULA WIN68 HE vendor HID interface (2E3C:C365, MI_02).

Enumerates the keyboard's HID collections, opens the vendor interface,
sends the device-info query (cmd 13) and heartbeat (cmd 1), and prints
every reply frame. Read-only config queries only — nothing persistent.
"""
import hid
import time

VID, PID = 0x2E3C, 0xC365


def fmt(b):
    return " ".join(f"{x:02X}" for x in b)


def main():
    devs = hid.enumerate(VID, PID)
    print(f"Found {len(devs)} HID collections for {VID:04X}:{PID:04X}:")
    for d in devs:
        print(f"  int={d['interface_number']} path={d['path'][:60]}... "
              f"usage_page=0x{d['usage_page']:04X} usage=0x{d['usage']:04X}")

    # Vendor interface = MI_02
    cand = [d for d in devs if d["interface_number"] == 2]
    if not cand:
        # fall back: vendor usage page (>= 0xFF00)
        cand = [d for d in devs if d["usage_page"] >= 0xFF00]
    if not cand:
        print("No vendor interface found!")
        return
    path = cand[0]["path"]
    print(f"\nOpening: {path}")
    d = hid.device()
    d.open_path(path)
    d.set_nonblocking(False)

    # heartbeat (cmd 1)
    hb = [1] + [0] * 63
    n = d.write(bytes(hb))
    print(f"heartbeat write -> {n}")
    t0 = time.time()
    while time.time() - t0 < 1.5:
        r = d.read(64, timeout_ms=200)
        if r:
            print("IN:", fmt(r))

    # device info (cmd 13)
    info = [13] + [0] * 63
    n = d.write(bytes(info))
    print(f"device-info write -> {n}")
    t0 = time.time()
    while time.time() - t0 < 2.5:
        r = d.read(64, timeout_ms=200)
        if r:
            print("IN:", fmt(r))

    d.close()


if __name__ == "__main__":
    main()
