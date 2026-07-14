#!/usr/bin/env python3
"""PiSugar 3 Plus button input.

Prefers the pisugar-server unix socket, which PUSHES the plain strings
"single"/"double"/"long" to every connected client when the button is
tapped (there is no poll command for taps — just hold the connection).
Without the daemon, falls back to polling I2C register 0x3A at 2 Hz
MAXIMUM — hammering the PiSugar's MCU faster is suspected of wedging
the whole I2C bus, and it intermittently NACKs (errno 121) even when
healthy, so failures back off hard and never affect the cameras.

Fully optional: with no PiSugar present this thread goes quiet after a
few probes and the keyboard remains the only shutter.
"""

import logging
import os
import socket
import threading
import time

LOG = logging.getLogger("pisugar")

SOCKET_PATH = "/tmp/pisugar-server.sock"
I2C_BUS = 1
PISUGAR_ADDR = 0x57
BTN_REG = 0x3A
SINGLE_BIT = 0x10
DOUBLE_BIT = 0x20

POLL_INTERVAL_S = 0.5      # 2 Hz — the verified safe maximum
BACKOFF_AFTER = 20         # consecutive failures before slowing way down
BACKOFF_RETRY_S = 10.0
GIVE_UP_PROBES = 5         # never seen at all -> stop polling entirely


class PiSugarButtons(threading.Thread):
    """Posts ("button", "single"|"double"|"long") onto the shared queue."""

    def __init__(self, events):
        super().__init__(daemon=True, name="pisugar")
        self.events = events
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            if os.path.exists(SOCKET_PATH):
                self._socket_loop()
            else:
                LOG.debug("pisugar-server socket not found — probing I2C")
                self._i2c_loop()
        except Exception as exc:
            LOG.error("button monitor died: %s — keyboard input only",
                      exc, exc_info=True)

    def _post(self, kind):
        LOG.info("button: %s", kind)
        self.events.put(("button", kind))

    def _sleep(self, secs):
        deadline = time.monotonic() + secs
        while self._running and time.monotonic() < deadline:
            time.sleep(0.2)

    # ---------------------------------------------------------- socket mode

    def _socket_loop(self):
        while self._running:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(SOCKET_PATH)
                sock.settimeout(1.0)
                LOG.info("connected to %s", SOCKET_PATH)
                while self._running:
                    try:
                        raw = sock.recv(256)
                    except socket.timeout:
                        continue
                    if not raw:
                        raise ConnectionResetError("server closed the socket")
                    text = raw.decode(errors="replace")
                    if "double" in text:
                        self._post("double")
                    elif "single" in text:
                        self._post("single")
                    elif "long" in text:
                        self._post("long")
            except (ConnectionRefusedError, FileNotFoundError):
                LOG.warning("socket unavailable — switching to I2C polling")
                self._i2c_loop()
                return
            except Exception as exc:
                LOG.warning("socket error: %s — reconnecting in 2 s", exc)
                self._sleep(2.0)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    # ------------------------------------------------------------- I2C mode

    def _i2c_loop(self):
        try:
            import smbus2
            bus = smbus2.SMBus(I2C_BUS)
        except Exception as exc:
            LOG.info("no PiSugar (%s) — SPACE key is the shutter", exc)
            return

        seen = False
        consec_fail = 0
        while self._running:
            try:
                val = bus.read_byte_data(PISUGAR_ADDR, BTN_REG)
                if not seen:
                    LOG.info("PiSugar button ready")
                elif consec_fail >= BACKOFF_AFTER:
                    LOG.info("PiSugar responding again")
                seen = True
                consec_fail = 0
                # Tap flags latch until cleared: write the value back with
                # the handled bits masked off.
                if val & DOUBLE_BIT:
                    bus.write_byte_data(PISUGAR_ADDR, BTN_REG,
                                        val & ~(DOUBLE_BIT | SINGLE_BIT))
                    self._post("double")
                elif val & SINGLE_BIT:
                    bus.write_byte_data(PISUGAR_ADDR, BTN_REG,
                                        val & ~SINGLE_BIT)
                    self._post("single")
            except OSError as exc:
                consec_fail += 1
                LOG.debug("PiSugar read failed (%d): %s", consec_fail, exc)
                if not seen:
                    # Not fitted at all — one console line, then stop
                    # touching the bus for good.
                    if consec_fail >= GIVE_UP_PROBES:
                        LOG.info("no PiSugar detected — SPACE key is the shutter")
                        break
                    self._sleep(0.5)
                elif consec_fail >= BACKOFF_AFTER:
                    # It was there and vanished — keep retrying, gently and
                    # quietly; its NACK storms must never wedge the bus.
                    if consec_fail == BACKOFF_AFTER:
                        LOG.warning("PiSugar stopped responding — retrying "
                                    "quietly every %.0f s", BACKOFF_RETRY_S)
                    self._sleep(BACKOFF_RETRY_S)
                else:
                    self._sleep(0.5)
            self._sleep(POLL_INTERVAL_S)
        try:
            bus.close()
        except Exception:
            pass
