"""Probe 2: enable analog travel streaming (cmd 33 sub 2, all keys) on
the AULA vendor interface and dump received frames.

Uses the reference Aether-HE builders. Prints write results + d.error(),
and every input frame received, hex, for N seconds.
"""
import hid
import time
import sys

import ref_aether_protocol as P

VID, PID = 0x2E3C, 0xC365


def fmt(b):
    return " ".join(f"{x:02X}" for x in b)


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    devs = hid.enumerate(VID, PID)
    cand = [d for d in devs if d["interface_number"] == 2
            and 0xFF00 <= d["usage_page"] <= 0xFFFF]
    if not cand:
        print("no vendor interface"); return
    path = cand[0]["path"]
    d = hid.device()
    d.open_path(path)
    print(f"opened {path[:80]}")

    all_mask = [0xFF] * 22          # select every possible key index
    pkt = bytes(P.build_open_trigger_test(range(22 * 8)))
    n = d.write(pkt)
    print(f"open-travel-test write -> {n} err={d.error()!r}")

    t0 = time.time()
    last = None
    while time.time() - t0 < secs:
        r = d.read(64, timeout_ms=200)
        if r:
            hexs = fmt(r)
            if hexs != last:
                print(f"{time.time()-t0:5.2f}s  {hexs}")
                last = hexs
    d.close()
    print("done")


if __name__ == "__main__":
    main()
