"""Isolate ViGEm path: set RT=200 / LT=100 directly, poll XInputGetState."""
import time
import vgamepad as vg

from xinput_test import XINPUT_STATE
import ctypes

x = ctypes.WinDLL("XInput1_4.dll")

pad = vg.VX360Gamepad()
print("pad created")
pad.right_trigger(value=200)
pad.left_trigger(value=100)
pad.update()
st = XINPUT_STATE()
for i in range(6):
    r = x.XInputGetState(0, ctypes.byref(st))
    print(f"poll {i}: ret={r} RT={st.bRightTrigger} LT={st.bLeftTrigger}",
          flush=True)
    time.sleep(0.3)
pad.reset()
pad.update()
x.XInputGetState(0, ctypes.byref(st))
print("after reset:", st.bRightTrigger, st.bLeftTrigger)
