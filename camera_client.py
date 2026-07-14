#!/usr/bin/env python3
"""Parent-side camera engine: worker supervision + the camera service thread.

WorkerLink spawns camera_worker.py and speaks its request/response protocol
with hard deadlines. A missed deadline means the worker is wedged somewhere
in libcamera — it gets SIGKILLed and respawned; the kernel reclaims the
camera unconditionally, which is the only reliable recovery (see README).

CameraService is the ONLY thread in this process that talks to the worker.
It round-robins previews across live cameras, runs capture sequences, builds
GIFs, and tracks per-camera health so one flaky ribbon never takes the rest
down.
"""

import json
import logging
import os
import queue
import select
import subprocess
import sys
import threading
import time

from PIL import Image

import gif_builder

LOG_LINK = logging.getLogger("camlink")
LOG_SVC = logging.getLogger("camsvc")

PREVIEW_TIMEOUT_S = 10.0
STILL_TIMEOUT_S = 15.0
READY_TIMEOUT_S = 30.0
QUIT_TIMEOUT_S = 2.0
RESPAWN_DELAY_S = 1.0            # let the kernel release /dev/video* first

DEAD_RETRY_INTERVAL_S = 30.0
THUMB_REFRESH_S = 10.0           # live view: background-camera refresh cadence
TIMEOUTS_TO_DEAD = 2             # consecutive worker-killing failures
SOFT_FAILS_TO_DEAD = 3           # consecutive in-worker errors
FAST_STRIKES_TO_DEMOTE = 3       # worker deaths in fast mode ...
FAST_STRIKE_WINDOW_S = 600.0     # ... within this window -> safe mode
SPAWN_FAILS_TO_FATAL = 3
SERVICE_ERRORS_TO_FATAL = 5

JPEG_QUALITY = 95


class WorkerTimeout(Exception):
    pass


class WorkerDied(Exception):
    pass


def chown_to_invoking_user(path):
    """The app runs under sudo; hand output files to the invoking user."""
    try:
        uid = int(os.environ.get("SUDO_UID", -1))
        gid = int(os.environ.get("SUDO_GID", -1))
        if uid >= 0:
            os.chown(path, uid, gid)
    except Exception as exc:
        LOG_SVC.debug("chown of %s failed: %s", path, exc)


class WorkerLink:
    """Owns the worker subprocess and its framed pipe protocol."""

    def __init__(self, preview_mode):
        self.preview_mode = preview_mode
        self._proc = None
        self._buf = bytearray()

    def start(self):
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "camera_worker.py")
        LOG_LINK.debug("spawning camera worker (preview_mode=%s)", self.preview_mode)
        self._buf = bytearray()
        self._proc = subprocess.Popen(
            [sys.executable, "-u", worker, "--preview-mode", self.preview_mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._pump_stderr, args=(self._proc,),
                         daemon=True, name="worker-stderr").start()
        header, _ = self._read_response(time.monotonic() + READY_TIMEOUT_S)
        if header.get("event") != "ready":
            raise WorkerDied("worker startup failed: %s"
                             % header.get("error", header))
        LOG_LINK.info("camera engine running (pid %d, %s preview)",
                      self._proc.pid, self.preview_mode)

    @staticmethod
    def _pump_stderr(proc):
        # Worker/libcamera chatter goes to the log file only; anything that
        # looks like trouble surfaces on the console too.
        wlog = logging.getLogger("worker")
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                if "ERROR" in line or "WARN" in line or "FATAL" in line:
                    wlog.warning("%s", line)
                else:
                    wlog.debug("%s", line)
        except Exception:
            pass

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def request(self, cmd, timeout):
        """Send one command, return (header, payload). Raises WorkerTimeout /
        WorkerDied; after either, kill() before using the link again."""
        if not self.alive():
            raise WorkerDied("worker not running")
        try:
            self._proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerDied("worker stdin closed: %s" % exc)
        return self._read_response(time.monotonic() + timeout)

    def _read_response(self, deadline):
        header_line = self._read_line(deadline)
        try:
            header = json.loads(header_line.decode("utf-8"))
        except ValueError:
            raise WorkerDied("garbage from worker: %r" % header_line[:200])
        payload = self._read_exact(int(header.get("len", 0)), deadline)
        return header, payload

    def _read_line(self, deadline):
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                return line
            self._fill(deadline)

    def _read_exact(self, n, deadline):
        while len(self._buf) < n:
            self._fill(deadline)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _fill(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerTimeout("no reply from worker within deadline")
        fd = self._proc.stdout.fileno()
        readable, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not readable:
            if self._proc.poll() is not None:
                raise WorkerDied("worker exited (rc=%s)" % self._proc.returncode)
            return
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            raise WorkerDied("worker closed stdout (rc=%s)" % self._proc.poll())
        self._buf.extend(chunk)

    def kill(self):
        proc, self._proc = self._proc, None
        self._buf = bytearray()
        if proc is None:
            return
        if proc.poll() is None:
            LOG_LINK.debug("killing camera worker pid=%d", proc.pid)
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    def shutdown(self):
        """Polite quit for clean app exit; falls back to kill()."""
        if self._proc is None:
            return
        try:
            self.request({"cmd": "quit"}, QUIT_TIMEOUT_S)
        except Exception:
            pass
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except Exception:
            pass
        self.kill()


class CameraService(threading.Thread):
    """Preview pump, capture sequencer and camera health tracker."""

    def __init__(self, num_cams, preview_mode, pics_dir, events, view="live"):
        super().__init__(daemon=True, name="camera-service")
        self.num_cams = num_cams
        self.pics_dir = pics_dir
        self.events = events              # shared queue.Queue to the UI
        self.link = WorkerLink(preview_mode)
        self.capturing = False
        # "live": stream one camera continuously (no mux switches between
        # frames -> near-real-time), refresh the rest every THUMB_REFRESH_S.
        # "grid": round-robin everything (a mux switch per frame, ~1 Hz/cam).
        self.view = view
        self.live_cam = 0
        self._last_thumb_refresh = 0.0
        self._thumb_rr = 0
        self.health = {c: {"state": "alive", "timeouts": 0, "soft": 0,
                           "last_retry": 0.0} for c in range(num_cams)}
        self._frames = {}                 # cam -> (seq, w, h, rgb_bytes)
        self._frames_lock = threading.Lock()
        self._seq = 0
        self._ctrl = queue.Queue()
        self._running = True
        self._rr = num_cams - 1           # round-robin pointer, pre-advanced
        self._spawn_fails = 0
        self._fast_strikes = []
        self._service_errors = 0

    # ------------------------------------------------------------ UI-facing

    @property
    def preview_mode(self):
        return self.link.preview_mode

    def get_frames(self):
        with self._frames_lock:
            return dict(self._frames)

    def get_health(self):
        return {c: self.health[c]["state"] for c in range(self.num_cams)}

    def request_capture(self):
        if self.capturing:
            LOG_SVC.info("capture already in progress — ignored")
            return
        # Flag set before enqueueing so a double-tap can't queue two shoots.
        self.capturing = True
        self._ctrl.put("capture")

    def toggle_mode(self):
        new = "safe" if self.link.preview_mode == "fast" else "fast"
        self._ctrl.put(("mode", new))

    def set_view(self, view):
        self.view = view
        LOG_SVC.debug("view -> %s", view)

    def set_live_cam(self, cam):
        if not 0 <= cam < self.num_cams or cam == self.live_cam:
            return
        if self.health[cam]["state"] != "alive":
            self.events.put(("status", "Camera %d is offline" % (cam + 1)))
            return
        self.live_cam = cam
        LOG_SVC.debug("live camera -> %d", cam + 1)
        self.events.put(("status", "Live view: camera %d" % (cam + 1)))

    def stop(self):
        self._running = False

    # ------------------------------------------------------------ main loop

    def run(self):
        if not self._ensure_worker():
            return
        while self._running:
            try:
                try:
                    item = self._ctrl.get_nowait()
                except queue.Empty:
                    item = None
                if item == "capture":
                    self._do_capture()
                elif isinstance(item, tuple) and item[0] == "mode":
                    self._switch_mode(item[1])
                else:
                    self._preview_tick()
                self._service_errors = 0
            except (WorkerTimeout, WorkerDied) as exc:
                # Escaped a handler somewhere — recover the worker anyway.
                LOG_SVC.error("unhandled worker failure: %s", exc)
                self._recover_worker()
            except Exception as exc:
                self._service_errors += 1
                LOG_SVC.error("service loop error (%d/%d): %s",
                              self._service_errors, SERVICE_ERRORS_TO_FATAL,
                              exc, exc_info=True)
                if self._service_errors >= SERVICE_ERRORS_TO_FATAL:
                    self.events.put(("fatal", "camera service failing repeatedly"))
                    break
                time.sleep(1.0)
        self.link.shutdown()
        LOG_SVC.debug("camera service stopped")

    # ------------------------------------------------------------ previews

    def _pick_cam(self):
        now = time.monotonic()
        for c in range(self.num_cams):
            h = self.health[c]
            if h["state"] == "dead" and now - h["last_retry"] >= DEAD_RETRY_INTERVAL_S:
                h["last_retry"] = now
                LOG_SVC.debug("retrying offline camera %d", c + 1)
                return c
        alive = [c for c in range(self.num_cams)
                 if self.health[c]["state"] == "alive"]
        if not alive:
            return None
        if self.view != "live":
            for _ in range(self.num_cams):
                self._rr = (self._rr + 1) % self.num_cams
                if self.health[self._rr]["state"] == "alive":
                    return self._rr
            return None
        # Live view: stream the live camera; steal a slot for one background
        # camera every THUMB_REFRESH_S (that costs two mux switches, hence
        # the deliberately long cadence).
        if self.live_cam not in alive:
            self.live_cam = alive[0]
            LOG_SVC.info("live view switched to camera %d (previous one went offline)",
                         self.live_cam + 1)
            self.events.put(("status",
                             "Live view: camera %d" % (self.live_cam + 1)))
        others = [c for c in alive if c != self.live_cam]
        if others and now - self._last_thumb_refresh >= THUMB_REFRESH_S:
            self._last_thumb_refresh = now
            self._thumb_rr = (self._thumb_rr + 1) % len(others)
            return others[self._thumb_rr]
        return self.live_cam

    def _preview_tick(self):
        cam = self._pick_cam()
        if cam is None:
            time.sleep(0.5)
            return
        try:
            header, payload = self.link.request({"cmd": "preview", "cam": cam},
                                                PREVIEW_TIMEOUT_S)
        except (WorkerTimeout, WorkerDied) as exc:
            self._worker_incident(cam, exc)
            return
        if not header.get("ok"):
            self._soft_fail(cam, header.get("error", "?"))
            return
        if len(payload) != header["w"] * header["h"] * 3:
            self._soft_fail(cam, "short frame (%d bytes)" % len(payload))
            return
        self._mark_alive(cam)
        with self._frames_lock:
            self._seq += 1
            self._frames[cam] = (self._seq, header["w"], header["h"], payload)

    # ------------------------------------------------------------ health

    def _mark_alive(self, cam):
        h = self.health[cam]
        if h["state"] == "dead":
            LOG_SVC.info("camera %d is back online", cam + 1)
            self.events.put(("status", "Camera %d back online" % (cam + 1)))
        h["state"] = "alive"
        h["timeouts"] = 0
        h["soft"] = 0

    def _mark_dead(self, cam, reason):
        h = self.health[cam]
        if h["state"] != "dead":
            LOG_SVC.warning("camera %d is offline — will keep retrying "
                            "every 30 s (%s)", cam + 1, reason)
            self.events.put(("status", "Camera %d offline" % (cam + 1)))
        h["state"] = "dead"
        h["last_retry"] = time.monotonic()

    def _soft_fail(self, cam, error):
        h = self.health[cam]
        h["soft"] += 1
        LOG_SVC.debug("cam %d soft failure %d/%d: %s",
                      cam + 1, h["soft"], SOFT_FAILS_TO_DEAD, error)
        if h["soft"] >= SOFT_FAILS_TO_DEAD:
            self._mark_dead(cam, error)

    def _worker_incident(self, cam, exc):
        """The worker hung or died while serving `cam`: kill, blame, respawn."""
        LOG_SVC.warning("camera %d not responding — restarting the camera "
                        "engine (%s)", cam + 1, exc)
        self.link.kill()

        h = self.health[cam]
        h["timeouts"] += 1
        if h["timeouts"] >= TIMEOUTS_TO_DEAD:
            self._mark_dead(cam, str(exc))

        if self.link.preview_mode == "fast":
            now = time.monotonic()
            self._fast_strikes = [t for t in self._fast_strikes
                                  if now - t < FAST_STRIKE_WINDOW_S] + [now]
            if len(self._fast_strikes) >= FAST_STRIKES_TO_DEMOTE:
                LOG_SVC.warning("fast preview keeps failing — switching to "
                                "the slower safe mode")
                self.link.preview_mode = "safe"
                self._fast_strikes = []
                self.events.put(("status", "Preview demoted to SAFE mode"))

        self._recover_worker()

    def _recover_worker(self):
        self.link.kill()
        time.sleep(RESPAWN_DELAY_S)
        self._ensure_worker()

    def _ensure_worker(self):
        while self._running:
            try:
                self.link.start()
                self._spawn_fails = 0
                return True
            except Exception as exc:
                self.link.kill()
                self._spawn_fails += 1
                LOG_SVC.error("camera engine failed to start (attempt %d/%d): %s",
                              self._spawn_fails, SPAWN_FAILS_TO_FATAL, exc)
                if self._spawn_fails >= SPAWN_FAILS_TO_FATAL:
                    self.events.put(
                        ("fatal", "camera worker won't start: %s" % exc))
                    return False
                time.sleep(2.0 * self._spawn_fails)
        return False

    # ------------------------------------------------------------ capture

    def _switch_mode(self, new_mode):
        if new_mode == self.link.preview_mode:
            return
        LOG_SVC.info("switching preview to %s (restarting camera engine)",
                     new_mode)
        self.link.kill()
        self.link.preview_mode = new_mode
        self._fast_strikes = []
        if self._ensure_worker():
            self.events.put(("status", "Preview mode: %s" % new_mode.upper()))

    def _do_capture(self):
        self.capturing = True
        try:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            targets = [c for c in range(self.num_cams)
                       if self.health[c]["state"] == "alive"]
            LOG_SVC.info("capture started (cameras %s)",
                         ", ".join(str(c + 1) for c in targets))
            LOG_SVC.debug("capture ts=%s", timestamp)
            if len(targets) < 2:
                self.events.put(("status",
                                 "Need at least 2 live cameras to shoot"))
                return

            saved = []
            for i, cam in enumerate(targets):
                self.events.put(("progress", "Capturing %d/%d (cam %d)…"
                                 % (i + 1, len(targets), cam + 1)))
                try:
                    header, payload = self.link.request(
                        {"cmd": "still", "cam": cam}, STILL_TIMEOUT_S)
                except (WorkerTimeout, WorkerDied) as exc:
                    self._worker_incident(cam, exc)
                    continue
                if not header.get("ok"):
                    self._soft_fail(cam, header.get("error", "?"))
                    continue
                if len(payload) != header["w"] * header["h"] * 3:
                    self._soft_fail(cam, "short still (%d bytes)" % len(payload))
                    continue
                self._mark_alive(cam)
                path = os.path.join(self.pics_dir,
                                    "%s_cam%d.jpg" % (timestamp, cam + 1))
                image = Image.frombytes("RGB", (header["w"], header["h"]),
                                        payload)
                image.save(path, "JPEG", quality=JPEG_QUALITY)
                chown_to_invoking_user(path)
                saved.append(path)
                LOG_SVC.debug("saved %s", path)

            if len(saved) < 2:
                LOG_SVC.warning("capture failed — only %d good photo(s)",
                                len(saved))
                self.events.put(("status", "Capture failed — only %d good "
                                 "frame(s)" % len(saved)))
                return

            gif_path = os.path.join(self.pics_dir,
                                    "%s_wigglegram.gif" % timestamp)
            try:
                gif_builder.build(
                    saved, gif_path,
                    progress=lambda msg: self.events.put(("progress", msg)))
            except Exception as exc:
                LOG_SVC.error("GIF build failed: %s", exc, exc_info=True)
                self.events.put(("status", "GIF build failed: %s" % exc))
                return
            chown_to_invoking_user(gif_path)
            LOG_SVC.info("wigglegram saved: %s (%d photos)",
                         os.path.basename(gif_path), len(saved))
            self.events.put(("gif_ready", gif_path))
        finally:
            self.events.put(("progress", None))
            self.capturing = False
