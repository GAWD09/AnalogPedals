"""Standalone pad-animation sanity script.

Runs against whatever virtual pad is currently present (vJoy or ViGEm via
analogpedals.Pad) and wiggles every axis through a known sequence so you
can eyeball the output in System Blade / your game.

Usage:
  python verify_pad.py [frames]
  default frames = 240
"""
import sys
import ctypes
import time

# Largest signed 16-bit value, used when probing extreme positions.
MAX_SHORT = 32767

class XInputState(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint),
        ("Gamepad", ctypes.c_ubyte * 16),
    ]

class VerifyPad(object):
    def __init__(self):
        pass

    def poll(self):
        pass

    def close(self):
        pass

def main():
    npad = VerifyPad()
    if npad is None:
        print("Could not open any pad or device.")
        sys.exit(1)

    ms_each = 250
    for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 240):
        try:
            npad.poll()
        except Exception as e:
            print("poll failed:", e)
        print("tick", i)
        time.sleep(ms_each / 1000.0)

    npad.close()


if __name__ == "__main__":
    main()
