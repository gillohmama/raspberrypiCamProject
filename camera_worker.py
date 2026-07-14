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
  stdout  one JSON header line, then exactly header["len"] raw RGB bytes
  stderr  log lines (relayed into the parent's log)
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

GPIO_SETTLE_S = 0.1    # GPIO half settled before the I2C transaction
MUX_SETTLE_S = 0.2     # mux settle after a completed switch

# libcamera names pixel formats DRM-style: "BGR888" is R,G,B in memory,
# which is what PIL and pygame expect. ("RGB888" would be B,G,R.)
PIXEL_FORMAT = "BGR888"
PREVIEW_SIZE = (640, 360)
STILL_SIZE = (1920, 1080)

AE_SETTLE_PREVIEW_S = 0.30   # exposure convergence before a preview grab
AE_SETTLE_STILL_S = 0.40     # a bit longer before a keeper
FAST_FLUSH_FRAMES = 2        # frames in flight during a live mux switch

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

    def __init__(self):
        self.current = -1
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (GPIO_SEL_A, GPIO_SEL_B, GPIO_SEL_OE):
            GPIO.setup(pin, GPIO.OUT)
        # The GPIO half (including OE high, camera A pattern) must be driven
        # before the very first I2C contact or the mux never responds.
        self._apply_gpio(0)
        time.sleep(GPIO_SETTLE_S)
        self._bus = smbus2.SMBus(I2C_BUS)
        LOG.info("mux ready (bus=%d addr=0x%02X, GPIO %d/%d/%d)",
                 I2C_BUS, MUX_I2C_ADDR, GPIO_SEL_A, GPIO_SEL_B, GPIO_SEL_OE)

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
        time.sleep(GPIO_SETTLE_S)

        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                self._bus.write_byte_data(MUX_I2C_ADDR, MUX_I2C_REG,
                                          MUX_I2C_VALUES[cam])
                time.sleep(MUX_SETTLE_S)
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
    """

    def __init__(self, preview_mode):
        self.preview_mode = preview_mode
        self.mux = MuxController()
        self.mux.select(0)
        self.picam2 = Picamera2()
        self._running_kind = None   # None | "preview" | "still"
        # Pin the raw stream to the still sensor mode so the viewfinder shows
        # the same field of view as the photos (otherwise libcamera picks the
        # full-FoV binned mode for the small preview stream). Dropped
        # automatically if buffer allocation can't afford it.
        self._pin_raw = True
        self._cfg_cache = {}
        LOG.info("camera engine ready (preview_mode=%s)", preview_mode)

    def _config(self, kind, pin_raw):
        key = (kind, pin_raw)
        if key not in self._cfg_cache:
            main = {"size": PREVIEW_SIZE if kind == "preview" else STILL_SIZE,
                    "format": PIXEL_FORMAT}
            make = (self.picam2.create_video_configuration if kind == "preview"
                    else self.picam2.create_still_configuration)
            if pin_raw:
                self._cfg_cache[key] = make(main=main, raw={"size": STILL_SIZE})
            else:
                self._cfg_cache[key] = make(main=main)
        return self._cfg_cache[key]

    def _configure(self, kind):
        try:
            self.picam2.configure(self._config(kind, self._pin_raw))
        except Exception as exc:
            if not self._pin_raw:
                raise
            LOG.warning("configure with pinned raw mode failed (%s) — dropping "
                        "raw stream; viewfinder FoV may differ from stills", exc)
            self._pin_raw = False
            self.picam2.configure(self._config(kind, False))

    def _stop_if_running(self):
        if self._running_kind is not None:
            self.picam2.stop()
            self._running_kind = None

    def preview(self, cam):
        if self.preview_mode == "fast":
            return self._preview_fast(cam)
        return self._preview_safe(cam)

    def _preview_safe(self, cam):
        self._stop_if_running()
        self.mux.select(cam)
        self._configure("preview")
        self.picam2.start()
        self._running_kind = "preview"
        time.sleep(AE_SETTLE_PREVIEW_S)
        arr = self.picam2.capture_array("main")
        self._stop_if_running()
        return arr

    def _preview_fast(self, cam):
        if self._running_kind != "preview":
            self._stop_if_running()
            self.mux.select(cam)
            self._configure("preview")
            self.picam2.start()
            self._running_kind = "preview"
            time.sleep(AE_SETTLE_PREVIEW_S)
        elif self.mux.current != cam:
            self.mux.select(cam)
            for _ in range(FAST_FLUSH_FRAMES):
                self.picam2.capture_array("main")
        return self.picam2.capture_array("main")

    def still(self, cam):
        # Stills always use the safe sequence — reliability over speed here.
        self._stop_if_running()
        self.mux.select(cam)
        self._configure("still")
        self.picam2.start()
        self._running_kind = "still"
        time.sleep(AE_SETTLE_STILL_S)
        self.picam2.capture_array("main")   # discard: AE/AWB still converging
        arr = self.picam2.capture_array("main")
        self._stop_if_running()
        return arr

    def close(self):
        self._stop_if_running()
        self.picam2.close()
        self.mux.close()


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
    args = parser.parse_args()

    # Ctrl-C on the terminal signals the whole process group; shutdown is the
    # parent's job (it closes our stdin, or kills us).
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(name)s %(levelname)s %(message)s")

    LOG.info("starting (pid=%d, preview_mode=%s)", os.getpid(), args.preview_mode)
    try:
        engine = CameraEngine(args.preview_mode)
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
