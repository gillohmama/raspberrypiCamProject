#!/usr/bin/env python3
"""Camera worker process: sole owner of the Arducam mux, Picamera2 and GPIO.

Spawned and supervised by camera_client.py — do not run by hand except for
debugging (it will wait for JSON commands on stdin).

This process is single-threaded and makes only BLOCKING camera calls, on
purpose: if libcamera wedges (dead camera port, frontend timeout), this
process simply hangs and the parent SIGKILLs and respawns it. Never add
timeout threads around capture calls in here — a capture abandoned by a
timeout thread leaves Picamera2's internal locks held and the libcamera
Camera in "Running" state, which is unrecoverable in-process. Process
death is the recovery mechanism.

Protocol (parent speaks first, strict request/response):
  stdin   one JSON object per line, e.g. {"cmd": "preview", "cam": 0}
          commands: ping, preview, still, burst_begin, burst_end, quit
  stdout  one JSON header line, then exactly header["len"] raw RGB bytes
  stderr  log lines (relayed into the parent's log)

A capture is bracketed by burst_begin/burst_end so the stills in it share
one exposure; see CameraEngine.burst_begin. Both are safe to send at any
time — a worker that respawned mid-burst simply comes back without one.
"""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

# Must be set before picamera2 (and with it libcamera) is imported.
# INFO-level libcamera would log several lines per configure() call, which
# in safe preview mode means several lines per frame.
os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "*:WARN")

import RPi.GPIO as GPIO
import smbus2
from picamera2 import Picamera2

try:
    from libcamera import Transform
except ImportError:      # very old picamera2 — fall back to software rotation
    Transform = None

LOG = logging.getLogger("worker")

I2C_BUS = 1
MUX_I2C_ADDR = 0x70
MUX_I2C_REG = 0x00

# BCM numbering. The adapter needs BOTH the GPIO half (CSI data lanes) and
# the I2C half (camera config signals) switched, GPIO first.
GPIO_SEL_A = 4
GPIO_SEL_B = 17
GPIO_SEL_OE = 18

# Per-port (A, B, C, D) states: (sel_a, sel_b, oe). Verified table — OE is
# part of the select encoding on the V2.2 board, not a plain enable.
MUX_GPIO_STATES = [
    (GPIO.LOW, GPIO.LOW, GPIO.HIGH),   # A
    (GPIO.HIGH, GPIO.LOW, GPIO.HIGH),  # B
    (GPIO.LOW, GPIO.HIGH, GPIO.LOW),   # C
    (GPIO.HIGH, GPIO.HIGH, GPIO.LOW),  # D
]
MUX_I2C_VALUES = [0x04, 0x05, 0x06, 0x07]

# Settle delays around a mux switch. These used to be 0.1 s and 0.2 s, which
# was generous padding rather than a measured minimum — the switch is an
# analog CSI mux plus one I2C register write, and Arducam's own samples
# barely wait at all. Short values are what make the viewfinder feel live, so
# they are the default now and tunable from the command line. Raise them if
# previews start showing the *previous* camera's frame, or if libcamera
# reports frontend timeouts on a rig whose wiring is known good.
DEFAULT_GPIO_SETTLE_MS = 5.0
DEFAULT_MUX_SETTLE_MS = 25.0

# libcamera names pixel formats DRM-style: "BGR888" is R,G,B in memory,
# which is what PIL and pygame expect. ("RGB888" would be B,G,R.)
PIXEL_FORMAT = "BGR888"
# Native size of the screen's viewfinder area, so the fullscreen live view
# needs no upscaling. ~1.1 MB per frame over the pipe — trivial.
PREVIEW_SIZE = (800, 450)
# IMX708 can read out 4608x2592, but every millisecond of readout widens the
# gap between the first and last frame of a wigglegram, and the finished GIF
# is 800 px wide either way. 2304x1296 is that sensor's fast full-field mode
# and still leaves room to crop; --still-size takes the full frame if the
# individual JPEGs matter more than how close together they were taken.
DEFAULT_STILL_SIZE = (2304, 1296)

# libcamera AfModeEnum. Named here rather than imported because the enum
# moved around between libcamera versions; the integers did not.
AF_MODE_MANUAL = 0
AF_MODE_CONTINUOUS = 2

AE_SETTLE_PREVIEW_S = 0.30   # exposure convergence before a preview grab
AE_SETTLE_STILL_S = 0.40     # a bit longer before a keeper
FAST_FLUSH_FRAMES = 2        # frames in flight during a live mux switch

# Read off the first camera of a burst and forced on the rest, so the frames
# of a wigglegram match instead of each metering and focusing for itself.
# LensPosition only exists on a sensor with a lens motor (Camera Module 3);
# it is skipped when the metadata does not carry it.
BURST_LOCK_KEYS = ("ExposureTime", "AnalogueGain", "ColourGains",
                   "LensPosition")

BUS_CLEAR_COOLDOWN_S = 10.0
_last_bus_clear = 0.0


def clear_i2c_bus():
    """Software bus-clear for a wedged I2C bus (everything errno 110).

    A slave holding SDA low mid-transfer survives reboots; only a power
    cycle or this procedure recovers it: pulse SCL ~10 times so the stuck
    slave finishes shifting out its byte, issue a STOP condition, then hand
    the pins back to the I2C peripheral (ALT0) via raspi-gpio — RPi.GPIO
    can drive them but cannot restore their ALT function.

    Rate-limited; returns True if it actually ran.
    """
    global _last_bus_clear
    if time.time() - _last_bus_clear < BUS_CLEAR_COOLDOWN_S:
        return False
    _last_bus_clear = time.time()

    if shutil.which("raspi-gpio") is None:
        LOG.warning("raspi-gpio not found — cannot attempt I2C bus-clear")
        return False

    SDA, SCL = 2, 3
    LOG.warning("I2C bus appears stuck — attempting bus-clear (SCL pulses + STOP)")
    try:
        GPIO.setup(SCL, GPIO.OUT, initial=GPIO.HIGH)
        for _ in range(10):
            GPIO.output(SCL, GPIO.LOW)
            time.sleep(0.0005)
            GPIO.output(SCL, GPIO.HIGH)
            time.sleep(0.0005)
        # STOP condition: SDA rises while SCL is high
        GPIO.setup(SDA, GPIO.OUT, initial=GPIO.LOW)
        time.sleep(0.0005)
        GPIO.output(SCL, GPIO.HIGH)
        time.sleep(0.0005)
        GPIO.output(SDA, GPIO.HIGH)
        time.sleep(0.0005)
    except Exception as exc:
        LOG.error("bus-clear GPIO toggling failed: %s", exc)
    finally:
        for pin in (SDA, SCL):
            try:
                subprocess.run(["raspi-gpio", "set", str(pin), "a0", "pu"],
                               check=False, timeout=5)
            except Exception as exc:
                LOG.error("raspi-gpio restore failed for GPIO %d: %s", pin, exc)
                return False

    time.sleep(0.1)
    LOG.info("bus-clear done")
    return True


class MuxController:
    """The Arducam Multi Camera Adapter V2.2 select logic."""

    def __init__(self, gpio_settle_s, mux_settle_s):
        self.current = -1
        self.gpio_settle_s = gpio_settle_s
        self.mux_settle_s = mux_settle_s
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (GPIO_SEL_A, GPIO_SEL_B, GPIO_SEL_OE):
            GPIO.setup(pin, GPIO.OUT)
        # The GPIO half (including OE high, camera A pattern) must be driven
        # before the very first I2C contact or the mux never responds.
        self._apply_gpio(0)
        time.sleep(self.gpio_settle_s)
        self._bus = smbus2.SMBus(I2C_BUS)
        LOG.info("mux ready (bus=%d addr=0x%02X, GPIO %d/%d/%d, settle %.0f/%.0f ms)",
                 I2C_BUS, MUX_I2C_ADDR, GPIO_SEL_A, GPIO_SEL_B, GPIO_SEL_OE,
                 gpio_settle_s * 1000, mux_settle_s * 1000)

    def _apply_gpio(self, cam):
        sel_a, sel_b, oe = MUX_GPIO_STATES[cam]
        GPIO.output(GPIO_SEL_A, sel_a)
        GPIO.output(GPIO_SEL_B, sel_b)
        GPIO.output(GPIO_SEL_OE, oe)

    def select(self, cam, retries=3):
        if cam == self.current:
            return
        # Unknown until BOTH halves succeed: if the I2C write fails the GPIO
        # half has already moved, so a later select() of the previous camera
        # must not early-return.
        self.current = -1

        self._apply_gpio(cam)
        time.sleep(self.gpio_settle_s)

        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                self._bus.write_byte_data(MUX_I2C_ADDR, MUX_I2C_REG,
                                          MUX_I2C_VALUES[cam])
                time.sleep(self.mux_settle_s)
                self.current = cam
                LOG.debug("mux: selected cam %d", cam)
                return
            except OSError as exc:
                last_exc = exc
                LOG.warning("mux i2c write for cam %d failed (attempt %d/%d): %s",
                            cam, attempt, retries, exc)
                # errno 110 = the bus itself is wedged; retrying is pointless
                # until it has been cleared
                if getattr(exc, "errno", None) == 110:
                    clear_i2c_bus()
                time.sleep(0.3)
        raise last_exc

    def close(self):
        try:
            self._bus.close()
        finally:
            GPIO.cleanup()


class CameraEngine:
    """One Picamera2 instance shared across the muxed cameras.

    Preview modes:
      safe  stop -> switch mux -> configure -> start -> settle -> grab -> stop
            (the proven-reliable method, ~0.6 s per frame)
      fast  keep the stream running, switch the mux live, flush the frames
            that were in flight during the switch (~0.3 s per frame).
            Deliberate experiment: all ports carry identical IMX219 timing,
            so the CSI frontend should keep locking. If it upsets libcamera
            the parent kills us and (after enough strikes) respawns in safe.

    Capture bursts (burst_begin / still... / burst_end) exist because the
    cameras are shot one after another: exposure and white balance are
    frozen at the first camera's values so the frames match, and — in fast
    mode — the stream is left running so the rest cost a mux switch rather
    than a full reconfigure.
    """

    def __init__(self, preview_mode, rotate=0, pin_raw=False,
                 gpio_settle_s=DEFAULT_GPIO_SETTLE_MS / 1000.0,
                 mux_settle_s=DEFAULT_MUX_SETTLE_MS / 1000.0,
                 still_size=DEFAULT_STILL_SIZE, focus="auto"):
        self.preview_mode = preview_mode
        self.still_size = still_size
        self.focus = focus
        # The cameras are mounted upside down in the case, so 180 is the
        # normal setting. The IMX219 flips in the sensor itself (free);
        # software rotation is only a fallback if libcamera refuses.
        self._hw_rotate = rotate == 180 and Transform is not None
        self._sw_rotate = rotate == 180 and Transform is None
        self.mux = MuxController(gpio_settle_s, mux_settle_s)
        self.mux.select(0)
        self.picam2 = Picamera2()
        self._running_kind = None   # None | "preview" | "still"
        # Pinning the raw stream to the still sensor mode makes the viewfinder
        # show the same field of view as the photos — but it forces a slow
        # full-resolution readout for every preview frame, which is the single
        # biggest brake on preview rate. Off by default: previews now run
        # whatever fast (usually binned) mode libcamera picks for them, at the
        # cost of a wider field of view than the stills. --pin-raw restores it.
        self._pin_raw = pin_raw
        self._cfg_cache = {}
        self._locked = None      # AE/AWB/focus held for the current burst
        self._burst = False      # burst is keeping the stream running
        # Only Camera Module 3 and friends have a lens motor; asking an
        # IMX219 for AfMode raises, so every focus control is gated on this.
        self._has_af = "AfMode" in getattr(self.picam2, "camera_controls", {})
        if focus != "auto" and not self._has_af:
            LOG.warning("--focus %s ignored: this sensor has no focus motor",
                        focus)
        LOG.info("camera engine ready (preview_mode=%s, rotate=%d%s, "
                 "stills %dx%d, focus %s)",
                 preview_mode, rotate,
                 ", in software" if self._sw_rotate else "",
                 still_size[0], still_size[1],
                 focus if self._has_af else "fixed (no motor)")

    def _config(self, kind, pin_raw, hw_rotate):
        key = (kind, pin_raw, hw_rotate)
        if key not in self._cfg_cache:
            main = {"size": PREVIEW_SIZE if kind == "preview" else self.still_size,
                    "format": PIXEL_FORMAT}
            make = (self.picam2.create_video_configuration if kind == "preview"
                    else self.picam2.create_still_configuration)
            kwargs = {"main": main}
            if pin_raw:
                kwargs["raw"] = {"size": self.still_size}
            if hw_rotate:
                # 180 deg == both flips; the sensor does it while reading out.
                kwargs["transform"] = Transform(hflip=1, vflip=1)
            self._cfg_cache[key] = make(**kwargs)
        return self._cfg_cache[key]

    def _configure(self, kind):
        """Configure, degrading gracefully: drop the pinned raw mode first,
        then the sensor flip (rotating in software instead)."""
        while True:
            try:
                self.picam2.configure(
                    self._config(kind, self._pin_raw, self._hw_rotate))
                return
            except Exception as exc:
                if self._pin_raw:
                    LOG.warning("configure with pinned raw mode failed (%s) — "
                                "dropping raw stream; viewfinder FoV may "
                                "differ from stills", exc)
                    self._pin_raw = False
                    continue
                if self._hw_rotate:
                    LOG.warning("configure with sensor flip failed (%s) — "
                                "rotating frames in software instead", exc)
                    self._hw_rotate = False
                    self._sw_rotate = True
                    continue
                raise

    def _oriented(self, arr):
        # Reversing both axes is a 180 deg rotation; tobytes() later
        # re-serialises it in C order, so the view is safe to return.
        return arr[::-1, ::-1] if self._sw_rotate else arr

    def _focus_controls(self):
        """How the lens should behave outside a burst. Empty on a sensor with
        no motor, where any AfMode would be rejected."""
        if not self._has_af:
            return {}
        if self.focus == "auto":
            return {"AfMode": AF_MODE_CONTINUOUS}
        return {"AfMode": AF_MODE_MANUAL, "LensPosition": float(self.focus)}

    def _apply_controls(self):
        """Re-assert exposure and focus. Necessary after every configure():
        picamera2 rebuilds its control set from the camera configuration,
        dropping anything set earlier. During a burst the locked values win —
        applying the free-running focus mode on top would let the lens move
        between frames, which is the thing the lock exists to prevent."""
        controls = self._locked if self._locked else self._focus_controls()
        if controls:
            self.picam2.set_controls(controls)

    def _stop_if_running(self):
        if self._running_kind is not None:
            self.picam2.stop()
            self._running_kind = None

    @property
    def streaming(self):
        """True while a burst is holding the stream open across mux switches."""
        return self._burst

    def preview(self, cam):
        if self.preview_mode == "fast":
            return self._preview_fast(cam)
        return self._preview_safe(cam)

    def _preview_safe(self, cam):
        self._stop_if_running()
        self.mux.select(cam)
        self._configure("preview")
        self._apply_controls()
        self.picam2.start()
        self._running_kind = "preview"
        time.sleep(AE_SETTLE_PREVIEW_S)
        arr = self.picam2.capture_array("main")
        self._stop_if_running()
        return self._oriented(arr)

    def _preview_fast(self, cam):
        if self._running_kind != "preview":
            self._stop_if_running()
            self.mux.select(cam)
            self._configure("preview")
            self._apply_controls()
            self.picam2.start()
            self._running_kind = "preview"
            time.sleep(AE_SETTLE_PREVIEW_S)
        elif self.mux.current != cam:
            self.mux.select(cam)
            for _ in range(FAST_FLUSH_FRAMES):
                self.picam2.capture_array("main")
        return self._oriented(self.picam2.capture_array("main"))

    def burst_begin(self, cam, stream=True):
        """Open a capture sequence: converge exposure and white balance on
        `cam`, then hold those values for every camera in the burst.

        The sensors otherwise meter independently, so the finished loop
        flickers in brightness and colour from frame to frame — the most
        visible artefact in a wigglegram, and one no amount of GIF
        post-processing fixes properly. Freezing also removes the AE settle
        from every camera after the first, which is most of the dead time
        between shots.

        With `stream`, and only in fast preview mode, the stream is left
        running so the remaining stills cost a mux switch instead of a
        reconfigure. Safe mode means live mux switching has already proved
        unreliable on this rig, so there the burst takes the exposure lock
        and nothing else.
        """
        self._burst = False
        self._locked = None
        self._stop_if_running()
        self.mux.select(cam)
        self._configure("still")
        self._apply_controls()              # free-running AE/AWB/AF, for now
        self.picam2.start()
        self._running_kind = "still"
        time.sleep(AE_SETTLE_STILL_S)
        self.picam2.capture_array("main")   # discard: AE/AWB still converging
        metadata = self.picam2.capture_metadata()

        locked = {"AeEnable": False, "AwbEnable": False}
        for key in BURST_LOCK_KEYS:
            if metadata.get(key) is not None:
                locked[key] = metadata[key]
        if self._has_af and "LensPosition" in locked:
            # Holding a position means taking the lens off autofocus, or the
            # algorithm will simply drive it somewhere else on the next frame.
            locked["AfMode"] = AF_MODE_MANUAL
        self.picam2.set_controls(locked)
        self._locked = locked
        self._burst = bool(stream) and self.preview_mode == "fast"
        LOG.info("burst locked on cam %d (%s), streaming=%s", cam,
                 ", ".join("%s=%s" % (k, locked[k])
                           for k in BURST_LOCK_KEYS if k in locked),
                 self._burst)
        if not self._burst:
            self._stop_if_running()

    def burst_end(self):
        """Close a capture sequence and give metering back to the viewfinder."""
        active = self._burst or self._locked is not None
        self._burst = False
        self._locked = None
        self._stop_if_running()
        if active:
            # configure() would clear these anyway, but an AeEnable=False or a
            # parked lens left behind would freeze the viewfinder at the
            # burst's exposure and focus.
            restore = {"AeEnable": True, "AwbEnable": True}
            restore.update(self._focus_controls())
            self.picam2.set_controls(restore)
            LOG.debug("burst ended, metering and focus unlocked")

    def still(self, cam):
        if self._burst and self._running_kind == "still":
            return self._still_streamed(cam)
        return self._still_safe(cam)

    def _still_streamed(self, cam):
        """Grab off the burst's running stream: switch the mux, flush the
        frames that were in flight during the switch, keep the next one."""
        if self.mux.current != cam:
            self.mux.select(cam)
            for _ in range(FAST_FLUSH_FRAMES):
                self.picam2.capture_array("main")
        return self._oriented(self.picam2.capture_array("main"))

    def _still_safe(self, cam):
        """Full stop -> switch -> configure -> start -> grab -> stop."""
        self._stop_if_running()
        self.mux.select(cam)
        self._configure("still")
        self._apply_controls()
        self.picam2.start()
        self._running_kind = "still"
        if self._locked is None:
            time.sleep(AE_SETTLE_STILL_S)
        self.picam2.capture_array("main")   # discard: first frame after start
        arr = self.picam2.capture_array("main")
        self._stop_if_running()
        return self._oriented(arr)

    def close(self):
        self._stop_if_running()
        self.picam2.close()
        self.mux.close()


def parse_size(spec):
    """"2304x1296" -> (2304, 1296)."""
    try:
        width, height = (int(part) for part in str(spec).lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected WIDTHxHEIGHT, e.g. 2304x1296, not %r" % spec)
    if width < 64 or height < 64:
        raise argparse.ArgumentTypeError("%r is implausibly small" % spec)
    return width, height


def send(obj, payload=b""):
    header = dict(obj)
    header["len"] = len(payload)
    out = sys.stdout.buffer
    out.write((json.dumps(header) + "\n").encode("utf-8"))
    if payload:
        out.write(payload)
    out.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-mode", choices=("fast", "safe"), default="fast")
    parser.add_argument("--rotate", type=int, choices=(0, 180), default=0)
    parser.add_argument("--gpio-settle", type=float,
                        default=DEFAULT_GPIO_SETTLE_MS,
                        help="ms after moving the select GPIOs")
    parser.add_argument("--mux-settle", type=float,
                        default=DEFAULT_MUX_SETTLE_MS,
                        help="ms after the mux I2C write")
    parser.add_argument("--pin-raw", action="store_true",
                        help="match viewfinder field of view to the stills, "
                             "at the cost of preview rate")
    parser.add_argument("--still-size", type=parse_size,
                        default=DEFAULT_STILL_SIZE, help="WIDTHxHEIGHT")
    parser.add_argument("--focus", default="auto",
                        help="'auto', or a fixed lens position in dioptres")
    args = parser.parse_args()

    # Ctrl-C and terminal hangups signal the whole process group; shutdown is
    # the parent's job (it closes our stdin, or kills us).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")

    LOG.info("starting (pid=%d, preview_mode=%s, rotate=%d, pin_raw=%s)",
             os.getpid(), args.preview_mode, args.rotate, args.pin_raw)
    try:
        engine = CameraEngine(args.preview_mode, args.rotate,
                              pin_raw=args.pin_raw,
                              gpio_settle_s=args.gpio_settle / 1000.0,
                              mux_settle_s=args.mux_settle / 1000.0,
                              still_size=args.still_size,
                              focus=args.focus)
    except Exception as exc:
        LOG.error("startup failed: %s", exc, exc_info=True)
        try:
            send({"event": "fatal", "error": str(exc)})
        except Exception:
            pass
        return 1
    send({"event": "ready", "mode": args.preview_mode})

    try:
        for line in sys.stdin:   # EOF (parent gone) ends the loop
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                LOG.error("bad request line: %r", line[:200])
                continue

            cmd = req.get("cmd")
            cam = req.get("cam", -1)
            if cmd == "quit":
                send({"ok": True, "cmd": "quit"})
                break
            if cmd == "ping":
                send({"ok": True, "cmd": "ping"})
                continue
            if cmd == "burst_begin" and isinstance(cam, int) and 0 <= cam <= 3:
                try:
                    engine.burst_begin(cam, stream=bool(req.get("stream", True)))
                except Exception as exc:
                    LOG.warning("burst_begin on cam %d failed: %s", cam, exc)
                    send({"ok": False, "cmd": cmd, "cam": cam, "error": str(exc)})
                    continue
                send({"ok": True, "cmd": cmd, "cam": cam,
                      "streaming": engine.streaming})
                continue
            if cmd == "burst_end":
                try:
                    engine.burst_end()
                except Exception as exc:
                    LOG.warning("burst_end failed: %s", exc)
                    send({"ok": False, "cmd": cmd, "error": str(exc)})
                    continue
                send({"ok": True, "cmd": cmd})
                continue
            if cmd in ("preview", "still") and isinstance(cam, int) and 0 <= cam <= 3:
                t0 = time.monotonic()
                try:
                    arr = engine.preview(cam) if cmd == "preview" else engine.still(cam)
                except Exception as exc:
                    LOG.warning("%s cam %d failed: %s", cmd, cam, exc)
                    send({"ok": False, "cmd": cmd, "cam": cam, "error": str(exc)})
                    continue
                payload = arr.tobytes()
                send({"ok": True, "cmd": cmd, "cam": cam,
                      "w": arr.shape[1], "h": arr.shape[0]}, payload)
                LOG.debug("%s cam %d ok (%dx%d, %.2fs)", cmd, cam,
                          arr.shape[1], arr.shape[0], time.monotonic() - t0)
            else:
                send({"ok": False, "cmd": cmd, "error": "unknown command"})
    except BrokenPipeError:
        LOG.warning("parent went away mid-reply")

    LOG.info("exiting")
    try:
        engine.close()
    except Exception as exc:
        LOG.warning("cleanup error (harmless at exit): %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
