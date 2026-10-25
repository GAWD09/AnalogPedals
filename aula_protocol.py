"""AnalogPedals — AULA WIN68 HE analog keys -> virtual Xbox 360 controller.

Clean-room, minimal vendor-HID protocol builders for the WIN68 HE's
vendor interface. The byte layouts below are the hardware-verified
protocol facts documented in README.md; no source code from any other
project is included in this file. Only what AnalogPedals itself needs
is implemented:

  REPORT_ID ................ 0x01 (single report id for this interface)
  build_heartbeat() ........ keep-alive frame, sent every 2 s
  build_open_trigger_test()  switch keys into live travel-test streaming
  build_close_trigger_test() stop travel-test streaming

Frames are 64 bytes: report id byte + a 63-byte body laid out as:

  body[0]  command id
  body[4]  family / length marker (0x18 = 24 for the trigger-test family)
  body[5]  sub-command within the family
  body[6..27] 22-byte key mask for the streaming sub-commands

Streaming frames the board answers with (parsed in analogpedals.py, not
here): body[5]==1, row=body[6], col=body[7], travel = body[8] | body[9]<<8
in 0.01 mm units.
"""

REPORT_ID = 0x01
BODY_LEN = 63
CMD_HEARTBEAT = 0x01
CMD_TRIGGER_TESTS = 0x21      # 33 — actuation / travel-test family
TRIGGER_FAMILY_MARKER = 0x18  # 24 — family marker used by the trigger group
KEY_MASK_LEN = 22             # 22-byte key mask (22 keys per row block)
MASK_ALL = 0xFF               # 0xFF = "every key in this row-slot"


def _wrap(body=None):
    """Return a full 64-byte report (report id + 63-byte body)."""
    b = list(body) if body else [0] * BODY_LEN
    if len(b) != BODY_LEN:
        raise ValueError("body must be exactly %d bytes" % BODY_LEN)
    return [REPORT_ID] + b


def build_heartbeat():
    """Keep-alive frame (cmd 1). Board expects this roughly every 2 s."""
    body = [0] * BODY_LEN
    body[0] = CMD_HEARTBEAT
    return _wrap(body)


def build_open_trigger_test(indices=None):
    """Switch the given keys (or all keys) into live travel-test streaming
    (trigger family, sub 2). AnalogPedals streams ALL keys, so the key
    mask is filled with 0xFF across all 22 slots — a partial mask was never
    needed by this application and is intentionally not implemented."""
    body = [0] * BODY_LEN
    body[0] = CMD_TRIGGER_TESTS
    body[4] = TRIGGER_FAMILY_MARKER
    body[5] = 2  # "open trigger test" sub-command
    body[6:6 + KEY_MASK_LEN] = [MASK_ALL] * KEY_MASK_LEN
    return _wrap(body)


def build_close_trigger_test():
    """Close the live travel-test stream (trigger family, sub 3), returning
    the keyboard to its normal behaviour."""
    body = [0] * BODY_LEN
    body[0] = CMD_TRIGGER_TESTS
    body[4] = TRIGGER_FAMILY_MARKER
    body[5] = 3  # "close trigger test" sub-command
    return _wrap(body)
