"""AnalogPedals — AULA WIN68 HE analog keys -> virtual Xbox 360 controller.

Per-key analog Hall-effect travel (0.01 mm) is read from the keyboard's
vendor HID interface and mapped to a virtual Xbox 360 pad via ViGEmBus.

Features
* Bind any key to any control: RT/LT pedals, stick axes (analog), buttons.
* Response curves per group: pedal gamma, per-axis stick gammas
  (out = fraction^gamma; >1 = softer low end), dead zone, full travel.
* Axis inversion per stick axis.
* OS-level key blocking (keyboard lib) so mapped keys stop typing.
* Passive key-name auto-learner (HID edge + keyboard hook cross-match)
  so blocking and labels work even for keys not in the built-in table.
* Named presets in presets.json (UI save/apply/delete; CLI --preset).

Protocol (reverse-engineered from the official AULA WebHID driver,
hardware-verified): vendor interface MI_02 (usage page 0xFF1B); streaming
opens with cmd 33 sub 2 on report ID 1; frames have row=body[6],
col=body[7] (index = row*22+col), travel = body[8] | body[9]<<8 in
0.01 mm units; heartbeat = cmd 1 every 2 s.

CLI:  python analogpedals.py [--bind CTRL=IDX ...] [--preset NAME]
UI:   python pedals_ui.py
"""
import argparse
import configparser
import collections
import json
import os
import sys
import threading
import time

import hid

import aula_protocol as P

VID, PID = 0x2E3C, 0xC365
_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "pedals.ini")
PRESETS_PATH = os.path.join(_HERE, "presets.json")
KEYMAP_PATH = os.path.join(_HERE, "keymap.json")

CONTROLS = {
    "RT":     {"label": "RT — trigger right (pedal)",  "group": "pedal",
               "kind": "trigger"},
    "LT":     {"label": "LT — trigger left (pedal)",   "group": "pedal",
               "kind": "trigger"},
    "LSX_L":  {"label": "Left stick ⇧ left",           "group": "stick"},
    "LSX_R":  {"label": "Left stick ⇨ right",          "group": "stick"},
    "LSY_U":  {"label": "Left stick ⇧ up",             "group": "stick"},
    "LSY_D":  {"label": "Left stick ⇩ down",           "group": "stick"},
    "RSX_L":  {"label": "Right stick ⇧ left",          "group": "stick"},
    "RSX_R":  {"label": "Right stick ⇨ right",         "group": "stick"},
    "RSY_U":  {"label": "Right stick ⇧ up",           "group": "stick"},
    "RSY_D":  {"label": "Right stick ⇩ down",          "group": "stick"},
    "BTN_A":  {"label": "Button A",                    "group": "button",
               "kind": "btn", "btn": "A"},
    "BTN_B":  {"label": "Button B",                    "group": "button",
               "kind": "btn", "btn": "B"},
    "BTN_X":  {"label": "Button X",                    "group": "button",
               "kind": "btn", "btn": "X"},
    "BTN_Y":  {"label": "Button Y",                    "group": "button",
               "kind": "btn", "btn": "Y"},
    "LB":     {"label": "Bumper left (LB)",            "group": "button",
               "kind": "btn", "btn": "LB"},
    "RB":     {"label": "Bumper right (RB)",           "group": "button",
               "kind": "btn", "btn": "RB"},
    "BACK":   {"label": "Back / Select",               "group": "button",
               "kind": "btn", "btn": "BACK"},
    "START":  {"label": "Start",                       "group": "button",
               "kind": "btn", "btn": "START"},
}

# (negative control, positive control). XInput: left = -X, down = -Y,
# so the Y pairs are (down, up).  [FIX: previously (up, down) = inverted]
AXIS_PAIRS = {
    "LSX": ("LSX_L", "LSX_R"),
    "LSY": ("LSY_D", "LSY_U"),
    "RSX": ("RSX_L", "RSX_R"),
    "RSY": ("RSY_D", "RSY_U"),
}
STICK_AXES = tuple(AXIS_PAIRS)


def make_default_bindings():
    return {"RT": 46, "LT": 69, "LSX_L": 68, "LSX_R": 70}


DEFAULTS = {
    "full_travel_mm": 3.6,
    "dead_zone_mm": 0.08,
    "jiggle_filter": 4,
    "button_threshold": 0.35,
    "gamma_pedal": 1.3,
    "gamma_lsx": 1.15,
    "gamma_lsy": 1.15,
    "gamma_rsx": 1.15,
    "gamma_rsy": 1.15,
    "invert_lsx": 0,
    "invert_lsy": 0,
    "invert_rsx": 0,
    "invert_rsy": 0,
    "block_mapped_keys": 0,
    "auto_learn_names": 1,
}


def find_vendor_path():
    for d in hid.enumerate(VID, PID):
        if d["interface_number"] == 2 and 0xFF00 <= d["usage_page"] <= 0xFFFF:
            return d["path"]
    return None


def travel_frac(t_raw, full_raw, dead_raw):
    if t_raw <= dead_raw:
        return 0.0
    f = (t_raw - dead_raw) / max(1, (full_raw - dead_raw))
    return max(0.0, min(1.0, f))


def shape(frac, gamma):
    if frac <= 0 or gamma <= 0:
        return 0.0
    return frac ** gamma


def prime_pad(pad):
    """Clear ViGEm's idle stick drift on a fresh virtual pad.

    A new pad reports a small default offset (LX~-3356, LY~-1869 ≈ 10%/6%)
    and vgamepad skips sending a report identical to its internal zeroed
    state, so writing plain 0 is a no-op. Forcing a genuine nonzero write
    first pushes a report through, then settling both sticks to true 0
    clears the drift. Best-effort: a failure only leaves the cosmetic
    offset, so it never propagates.
    """
    try:
        pad.left_joystick_float(0.01, 0.0)
        pad.right_joystick_float(0.01, 0.0)
        pad.update()
        pad.left_joystick_float(0.0, 0.0)
        pad.right_joystick_float(0.0, 0.0)
        pad.update()
    except Exception:
        pass


class TravelReader(threading.Thread):
    """Streams all keys. Latest raw travel per index in `values`; rising
    edges in `edges` [(t, idx)]. `latency_ms` is kept for UI compatibility
    but is not measured (it would only time our own code)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.values = {}
        self.edges = collections.deque(maxlen=256)
        self.latency_ms = None
        self.running = True
        self.dev = None
        self.lock = threading.Lock()
        self._frames = 0
        self.fps = 0.0
        self._prev = {}

    def get(self, idx):
        if idx is None:
            return 0
        with self.lock:
            return self.values.get(idx, 0)

    def stats(self):
        return self._frames

    def _open(self):
        path = find_vendor_path()
        if path is None:
            return False
        try:
            self.dev = hid.device()
            self.dev.open_path(path)
            d = [0] * 63
            d[0] = 33
            d[4] = 24
            d[5] = 2
            d[6:28] = [0xFF] * 22
            self.dev.write(bytes([P.REPORT_ID] + d))
            return True
        except Exception as e:
            print(f"[reader] open failed: {e}")
            self.dev = None
            return False

    def run(self):
        hb_next = 0.0
        fps_t0 = time.time()
        fps_frames = 0
        while self.running:
            if self.dev is None:
                if not self._open():
                    time.sleep(1.0)
                    continue
                print("[reader] connected to vendor interface")
            try:
                if time.time() >= hb_next:
                    self.dev.write(bytes(P.build_heartbeat()))
                    hb_next = time.time() + 2.0
                r = self.dev.read(64, timeout_ms=100)
                if not r:
                    continue
                body = list(r[1:])
                if len(body) < 10 or body[0] != 33 or body[5] != 1:
                    continue
                idx = body[6] * 22 + body[7]
                travel = body[8] | (body[9] << 8)
                now = time.time()
                with self.lock:
                    prev = self._prev.get(idx, 0)
                    self.values[idx] = travel
                    self._prev[idx] = travel
                    if prev <= 5 and travel > 25:
                        self.edges.append((now, idx))
                self._frames += 1
                fps_frames += 1
                if now - fps_t0 >= 1.0:
                    self.fps = fps_frames / (now - fps_t0)
                    fps_t0 = now
                    fps_frames = 0
            except Exception:
                try:
                    self.dev.close()
                except Exception:
                    pass
                self.dev = None
                time.sleep(1.0)

    def stop(self):
        self.running = False


# --------------------------------------------------------------------
# Key-name learning (HID edges × OS keyboard hook)
# --------------------------------------------------------------------
def load_keynames():
    if os.path.exists(KEYMAP_PATH):
        try:
            with open(KEYMAP_PATH, encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}


def save_keynames(names):
    try:
        with open(KEYMAP_PATH, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in names.items()}, f, indent=1)
    except Exception:
        pass


try:
    import keyboard as _kb
    _KB_OK = True
except Exception:
    _kb = None
    _KB_OK = False


class KeyNameLearner:
    """Passively learns idx -> OS key-name by matching keyboard press
    events to a single fresh HID press within a ±150 ms window."""

    def __init__(self, reader, names=None):
        self.reader = reader
        self.names = dict(names if names is not None else load_keynames())
        self._events = collections.deque(maxlen=256)
        self._hook = None
        self.active = _KB_OK

    def start(self):
        if self.active and self._hook is None:
            try:
                self._hook = _kb.on_press(lambda e: self._events.append(
                    (time.time(), e.name)))
            except Exception:
                self.active = False

    def update(self):
        """Called from the UI loop; returns list of (idx, name) learned."""
        learned = []
        if not self.active or self._hook is None:
            self._events.clear()
            return learned
        while self._events:
            t, name = self._events.popleft()
            if name is None:
                continue
            match = None
            with self.reader.lock:
                candidates = [(et, idx) for et, idx in self.reader.edges
                              if abs(et - t) <= 0.15]
            if len(candidates) == 1:
                match = candidates[0][1]
                with self.reader.lock:
                    self.reader.edges.remove(candidates[0])
            if match is not None and name not in self.names.values():
                self.names[match] = name
                learned.append((match, name))
        if learned:
            save_keynames(self.names)
        return learned

    def stop(self):
        try:
            if self._hook is not None:
                _kb.unhook(self._hook)
        except Exception:
            pass
        self._hook = None


class KeyBlocker:
    """OS-level suppression of the physical keys bound to controls."""

    def __init__(self):
        self.available = _KB_OK
        self._blocked = []

    def apply(self, bindings, names):
        if not self.available:
            return []
        prevented = []
        for name in sorted(self._blocked):
            try:
                _kb.unblock_key(name)
            except Exception:
                pass
        self._blocked = []
        chars = set()
        for idx in bindings.values():
            if idx is None:
                continue
            n = names.get(idx)
            if n:
                chars.add(str(n).lower())
        for name in sorted(chars):
            try:
                _kb.block_key(name)
                self._blocked.append(name)
            except Exception as e:
                prevented.append(f"{name}: {e}")
        return prevented

    def stop(self):
        if not self.available:
            return
        for name in self._blocked:
            try:
                _kb.unblock_key(name)
            except Exception:
                pass
        self._blocked = []


# --------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------
class MappingEngine:
    """Reader travel -> full pad state per bindings + curve settings."""

    def __init__(self, bindings=None, params=None):
        self.bindings = dict(make_default_bindings()
                             if bindings is None else bindings)
        self.params = dict(DEFAULTS if params is None else params)
        self._pad_state = {}

    def load(self):
        bindings, params = load_config()
        self.bindings = bindings
        self.params = params
        return self

    def save(self):
        save_config(self.bindings, self.params)

    def gamma(self, group_or_axis):
        p = self.params
        if group_or_axis == "pedal" or group_or_axis == "button":
            return p.get("gamma_pedal", 1.0)
        if group_or_axis in STICK_AXES:
            return p.get("gamma_" + group_or_axis.lower(), 1.0)
        return 1.0

    def frac(self, reader, control):
        t = reader.get(self.bindings.get(control))
        full = int(self.params["full_travel_mm"] * 100)
        dead = int(self.params["dead_zone_mm"] * 100)
        return travel_frac(t, full, dead)

    def tick(self, reader):
        p = self.params
        out = {}
        for c in ("RT", "LT"):
            out[c] = int(round(shape(self.frac(reader, c),
                                     self.gamma("pedal")) * 255))
        for axis, (neg, pos) in AXIS_PAIRS.items():
            g = self.gamma(axis)
            fn = shape(self.frac(reader, neg), g)
            fp = shape(self.frac(reader, pos), g)
            v = fp - fn
            key = "invert_" + axis.lower()
            if p.get(key):
                v = -v
            out[axis] = v
        thresh = p.get("button_threshold", 0.35)
        full = int(p["full_travel_mm"] * 100)
        dead = int(p["dead_zone_mm"] * 100)
        for c, meta in CONTROLS.items():
            if meta.get("kind") != "btn":
                continue
            t = reader.get(self.bindings.get(c))
            out["DOWN_" + c] = (self.bindings.get(c) is not None
                                and travel_frac(t, full, dead) >= thresh)
        out["fps"] = reader.fps
        return out

    def apply_to_pad(self, pad, state):
        changed = False
        s = self._pad_state
        if state.get("RT", 0) != s.get("RT", 0):
            pad.right_trigger(value=state["RT"]); changed = True
        if state.get("LT", 0) != s.get("LT", 0):
            pad.left_trigger(value=state["LT"]); changed = True
        # Compose each stick ONCE, using the CURRENT state for both axes,
        # so X and Y changing in the same frame don't clobber each other.
        for meth, ax, ay in (("left_joystick_float", "LSX", "LSY"),
                             ("right_joystick_float", "RSX", "RSY")):
            x = min(1.0, max(-1.0, state.get(ax, 0.0)))
            y = min(1.0, max(-1.0, state.get(ay, 0.0)))
            if (abs(x - s.get(ax, 0.0)) > 0.004
                    or abs(y - s.get(ay, 0.0)) > 0.004):
                getattr(pad, meth)(x, y)
                changed = True
        import vgamepad as vg
        for c, meta in CONTROLS.items():
            if meta.get("kind") != "btn":
                continue
            down = state.get("DOWN_" + c, False)
            k = "DOWN_" + c
            if down != s.get(k, False):
                # vgamepad names the bumpers LEFT/RIGHT_SHOULDER
                const = {"LB": "LEFT_SHOULDER",
                         "RB": "RIGHT_SHOULDER"}.get(meta["btn"], meta["btn"])
                btn = getattr(vg.XUSB_BUTTON, "XUSB_GAMEPAD_" + const)
                (pad.press_button if down else pad.release_button)(btn)
                changed = True
            s[k] = down
        if changed:
            pad.update()
        for k, v in state.items():
            s[k] = v
        return changed


# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
def load_config():
    bindings = make_default_bindings()
    params = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH, encoding="utf-8")
        if cp.has_section("bindings"):
            # [FIX] a blank value now loads as None (unbound) instead of
            # being skipped, so unbinding a default control persists.
            for c in CONTROLS:
                if c in cp["bindings"]:
                    s = cp["bindings"][c].strip()
                    if s == "":
                        bindings[c] = None
                    else:
                        try:
                            bindings[c] = int(float(s))
                        except ValueError:
                            pass
        if cp.has_section("pedals"):
            g = cp["pedals"]
            for k in ("full_travel_mm", "dead_zone_mm", "button_threshold"):
                if k in g:
                    params[k] = g.getfloat(k)
            if "jiggle_filter" in g:
                params["jiggle_filter"] = g.getint("jiggle_filter")
        if cp.has_section("curve"):
            g = cp["curve"]
            if "gamma_pedal" in g:
                params["gamma_pedal"] = g.getfloat("gamma_pedal")
            legacy = g.getfloat("gamma_stick", None)
            for axis in STICK_AXES:
                k = "gamma_" + axis.lower()
                params[k] = g.getfloat(k, legacy if legacy is not None
                                       else params[k])
        if cp.has_section("options"):
            g = cp["options"]
            for k in ("invert_lsx", "invert_lsy", "invert_rsx", "invert_rsy",
                      "block_mapped_keys", "auto_learn_names"):
                if k in g:
                    params[k] = g.getint(k)
    return bindings, params


def save_config(bindings, params):
    cp = configparser.ConfigParser()
    cp["bindings"] = {c: ("" if bindings.get(c) is None
                          else str(bindings.get(c))) for c in CONTROLS}
    cp["pedals"] = {
        "full_travel_mm": str(params["full_travel_mm"]),
        "dead_zone_mm": str(params["dead_zone_mm"]),
        "jiggle_filter": str(params["jiggle_filter"]),
        "button_threshold": str(params["button_threshold"]),
    }
    cp["curve"] = {"gamma_pedal": str(params["gamma_pedal"])}
    for axis in STICK_AXES:
        cp["curve"]["gamma_" + axis.lower()] = str(params["gamma_" + axis.lower()])
    cp["options"] = {k: str(int(params.get(k, 0)))
                     for k in ("invert_lsx", "invert_lsy", "invert_rsx",
                               "invert_rsy", "block_mapped_keys",
                               "auto_learn_names")}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)


# --------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------
def load_presets():
    if os.path.exists(PRESETS_PATH):
        try:
            with open(PRESETS_PATH, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_presets(presets):
    try:
        with open(PRESETS_PATH, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=1)
    except Exception:
        pass


def snapshot(engine):
    """Current bindings + params as a preset payload."""
    return {"bindings": dict(engine.bindings), "params": dict(engine.params)}


def apply_preset(engine, preset):
    if "bindings" in preset:
        engine.bindings = {k: (None if v in ("", None) else int(v))
                           for k, v in preset["bindings"].items()}
    if "params" in preset:
        engine.params.update(preset["params"])
        # prune removed keys so stale values don't linger
        for k in list(engine.params):
            if k not in DEFAULTS and k != "gamma_stick":
                engine.params.pop(k, None)


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bind", action="append", default=[],
                    metavar="CTRL=IDX", help="e.g. --bind RT=46; blank IDX unbinds")
    ap.add_argument("--preset", help="apply a named preset from presets.json")
    ap.add_argument("--full-travel-mm", type=float)
    ap.add_argument("--dead-zone-mm", type=float)
    ap.add_argument("--gamma-pedal", type=float)
    ap.add_argument("--block-keys", action="store_true",
                    help="suppress typed output of mapped keys (OS hook)")
    return ap.parse_args()


def cli_main():
    args = parse_args()
    engine = MappingEngine().load()
    if args.preset:
        presets = load_presets()
        if args.preset not in presets:
            print(f"preset {args.preset!r} not found; have: "
                  f"{', '.join(presets) or 'none'}"); sys.exit(2)
        apply_preset(engine, presets[args.preset])
        print(f"applied preset {args.preset}")
    for b in args.bind:
        name, _, val = b.partition("=")
        name = name.upper().strip()
        if name not in CONTROLS:
            print(f"unknown control {name!r}; valid: {', '.join(CONTROLS)}")
            sys.exit(2)
        if not val.strip():
            engine.bindings[name] = None   # [FIX] None, consistent everywhere
        else:
            engine.bindings[name] = int(val)
    if args.full_travel_mm is not None:
        engine.params["full_travel_mm"] = args.full_travel_mm
    if args.dead_zone_mm is not None:
        engine.params["dead_zone_mm"] = args.dead_zone_mm
    if args.gamma_pedal is not None:
        engine.params["gamma_pedal"] = args.gamma_pedal

    try:
        import vgamepad as vg
        pad = vg.VX360Gamepad()
    except Exception as e:
        print("ERROR: cannot create virtual Xbox controller:", e)
        sys.exit(1)
    prime_pad(pad)

    print("AnalogPedals bindings:", engine.bindings)
    print(f"  full travel {engine.params['full_travel_mm']:.2f} mm, "
          f"dead zone {engine.params['dead_zone_mm']:.2f} mm, "
          f"gamma {engine.params['gamma_pedal']:.2f}/"
          f"{engine.params['gamma_lsx']:.2f}")
    print("  Ctrl+C to exit.\n")

    reader = TravelReader()
    reader.start()

    blocker = None
    learner = None
    builtin = {46: "W", 69: "S", 68: "A", 70: "D"}
    if args.block_keys:
        if not _KB_OK:
            print("keyboard lib missing -> cannot block keys")
        else:
            print("[block] attaching hook…")
            learner = KeyNameLearner(reader)
            learner.start()
            blocker = KeyBlocker()
            failed = blocker.apply(engine.bindings,
                                   {**builtin, **learner.names})
            for f in failed:
                print("[block] could not suppress:", f)
    shown = {}
    try:
        while True:
            state = engine.tick(reader)
            engine.apply_to_pad(pad, state)
            interesting = {c: state[c] for c in ("RT", "LT")
                           if engine.bindings.get(c) is not None}
            for axis in STICK_AXES:
                neg, pos = AXIS_PAIRS[axis]
                if (engine.bindings.get(neg) is not None
                        or engine.bindings.get(pos) is not None):
                    interesting[axis] = round(state[axis] * 127)
            q = {c: (v if not isinstance(v, float) else int(v))
                 for c, v in interesting.items()}
            for c, v in q.items():
                if shown.get(c) != v:
                    shown[c] = v
                    print(f"  {c:<4} {v}", flush=True)
            if learner is not None:
                learner.update()
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        if blocker:
            blocker.stop()
        if learner:
            learner.stop()
        pad.reset()
        pad.update()
        try:
            d = hid.device()
            pth = find_vendor_path()
            if pth:
                d.open_path(pth)
                d.write(bytes(P.build_close_trigger_test()))
                d.close()
        except Exception:
            pass
        print("\nexited cleanly — pad reset, streaming closed")


if __name__ == "__main__":
    cli_main()
