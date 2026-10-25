"""Read XInput pad #0 (the virtual Xbox 360 controller) and print trigger
and thumbstick values — used to verify AnalogPedals end-to-end."""
import ctypes
import sys


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint),
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


x = ctypes.WinDLL("XInput1_4.dll")
if not hasattr(x, "XInputGetState"):
    x = ctypes.WinDLL("XInput1_3.dll")


def main():
    import time
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    st = XINPUT_STATE()
    last = None
    t0 = time.time()
    while time.time() - t0 < limit:
        r = x.XInputGetState(0, ctypes.byref(st))
        if r != 0:
            print("XInputGetState error", r, flush=True)
            time.sleep(0.2)
            continue
        lx = st.sThumbLX // 256        # scale to +-127 for display
        ly = st.sThumbLY // 256
        cur = (st.bRightTrigger, st.bLeftTrigger, lx, ly)
        if cur != last:
            print(f"RT={st.bRightTrigger:3d}  LT={st.bLeftTrigger:3d}  "
                  f"LX={lx:4d}  LY={ly:4d}", flush=True)
            last = cur
        time.sleep(0.04)
    print("done")


if __name__ == "__main__":
    main()
