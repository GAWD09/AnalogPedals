"""Capture analog travel frames from the AULA WIN68 HE vendor interface.

Enables travel-test streaming for all keys, sends a heartbeat every 2 s,
and logs every frame: time, row, col, index=row*22+col, travel_raw (0.01 mm).
 Writes AnalogPedals/capture_win68.csv and prints a live summary.
"""
import csv
import hid
import sys
import time

import ref_aether_protocol as P

VID, PID = 0x2E3C, 0xC365
OUT = "capture_win68.csv"


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    devs = hid.enumerate(VID, PID)
    cand = [d for d in devs if d["interface_number"] == 2
            and 0xFF00 <= d["usage_page"] <= 0xFFFF]
    if not cand:
        print("no vendor interface"); return
    path = cand[0]["path"]
    d = hid.device()
    d.open_path(path)

    d.write(bytes(P.build_open_trigger_test(range(22 * 8))))
    rows = []
    last = {}
    t0 = time.time()
    hb_next = t0 + 2.0
    while time.time() - t0 < secs:
        if time.time() >= hb_next:
            d.write(bytes(P.build_heartbeat()))
            hb_next = time.time() + 2.0
        try:
            r = d.read(64, timeout_ms=100)
        except Exception as e:
            print("read error:", e); break
        if not r:
            continue
        body = list(r[1:])
        if len(body) < 10 or body[0] != 33 or body[5] != 1:
            print("other frame:", " ".join(f"{x:02X}" for x in r))
            continue
        row_i, col_i = body[6], body[7]
        idx = row_i * 22 + col_i
        travel = body[8] | (body[9] << 8)
        t = time.time() - t0
        rows.append((t, row_i, col_i, idx, travel))
        prev = last.get(idx, -1)
        if abs(travel - prev) >= 20 or (prev == -1):
            print(f"{t:6.2f}s  idx={idx:3d} (r{row_i},c{col_i:02d})  travel={travel:5d}  "
                  f"{travel/100:5.2f} mm")
        last[idx] = travel
    d.close()

    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "row", "col", "index", "travel_raw"])
        w.writerows(rows)
    print(f"\n{len(rows)} frames -> {OUT}")
    tops = {}
    for _t, row_i, col_i, idx, travel in rows:
        tops[idx] = max(tops.get(idx, 0), travel)
    for idx, tv in sorted(tops.items()):
        print(f"idx {idx:3d}: max travel {tv}")


if __name__ == "__main__":
    main()
