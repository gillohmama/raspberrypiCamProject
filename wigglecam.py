#!/usr/bin/env python3
"""Wigglegram camera — entrypoint.

    sudo python3 wigglecam.py 3                 # cameras on ports A, B, C
    sudo python3 wigglecam.py 3 --preview-mode safe
    sudo python3 wigglecam.py 4 --windowed      # development

See README.md for the architecture. This process runs the UI and
supervises the camera worker; if the camera stack becomes unrecoverable
it re-execs itself (with a restart-loop guard).
"""

import argparse
import logging
import logging.handlers
import os
import pwd
import queue
import signal
import sys
import time
import traceback

import pygame

import ui
from camera_client import CameraService
from pisugar import PiSugarButtons

LOG = logging.getLogger("main")

RESTART_ENV = "WIGGLECAM_RESTARTS"
MAX_RESTARTS = 5
RESTART_WINDOW_S = 900


def resolve_pics_dir():
    """~/piCameraPics for the *invoking* user (the app runs under sudo)."""
    user = os.environ.get("SUDO_USER")
    home = None
    if user:
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    if not home:
        home = os.path.expanduser("~")
    pics = os.path.join(home, "piCameraPics")
    os.makedirs(pics, exist_ok=True)
    try:
        uid = int(os.environ.get("SUDO_UID", -1))
        gid = int(os.environ.get("SUDO_GID", -1))
        if uid >= 0:
            os.chown(pics, uid, gid)
    except Exception:
        pass
    return pics


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s",
        "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wigglecam.log")
    try:
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3)
        rotating.setFormatter(fmt)
        root.addHandler(rotating)
    except OSError as exc:
        root.warning("cannot open %s (%s) — console logging only", log_path, exc)


def self_restart(reason):
    """Re-exec in place, refusing if it would loop. Returns False if refused
    (only ever returns on refusal)."""
    now = int(time.time())
    count, first = 0, now
    raw = os.environ.get(RESTART_ENV, "")
    if raw:
        try:
            count_s, first_s = raw.split("@")
            count, first = int(count_s), int(first_s)
        except ValueError:
            pass
    if now - first > RESTART_WINDOW_S:
        count, first = 0, now
    count += 1
    if count > MAX_RESTARTS:
        LOG.critical("RESTART limit reached (%d in %d s) — giving up: %s",
                     count - 1, RESTART_WINDOW_S, reason)
        return False
    os.environ[RESTART_ENV] = "%d@%d" % (count, first)
    LOG.critical("RESTART #%d: %s", count, reason)
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


class App:
    MODE_LIVE = "live"
    MODE_PLAYBACK = "playback"

    def __init__(self, args):
        self.events = queue.Queue()
        self.pics_dir = resolve_pics_dir()
        self.display = ui.DisplayManager(args.num_cams, windowed=args.windowed)
        self.service = CameraService(args.num_cams, args.preview_mode,
                                     self.pics_dir, self.events)
        self.buttons = PiSugarButtons(self.events)
        self.mode = self.MODE_LIVE
        self.latest_gif = self._find_latest_gif()
        self.running = True
        self.fatal_reason = None

    def _find_latest_gif(self):
        try:
            gifs = [os.path.join(self.pics_dir, name)
                    for name in os.listdir(self.pics_dir)
                    if name.endswith("_wigglegram.gif")]
            return max(gifs, key=os.path.getmtime) if gifs else None
        except Exception:
            return None

    # ---------------------------------------------------------------- run

    def run(self):
        self.service.start()
        self.buttons.start()
        LOG.info("app running (pics=%s, latest_gif=%s)",
                 self.pics_dir, self.latest_gif)
        self.display.set_status("Ready — tap the button or SPACE to shoot", 5)
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                self._handle_pygame_event(event)
            self._drain_events()
            if self.fatal_reason:
                break
            if self.mode == self.MODE_LIVE:
                self.display.draw_viewfinder(self.service.get_frames(),
                                             self.service.get_health(),
                                             self.service.preview_mode)
            else:
                self.display.draw_playback()
            clock.tick(30)
        return self.fatal_reason

    # -------------------------------------------------------------- events

    def _handle_pygame_event(self, event):
        if event.type == pygame.QUIT:
            LOG.info("QUIT event")
            self.running = False
        elif event.type == pygame.KEYDOWN:
            self._handle_key(event.key)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            # Touchscreen taps arrive as mouse clicks.
            if self.mode == self.MODE_PLAYBACK:
                self.display.adjust_speed(faster=event.pos[0] >= ui.SCREEN_W // 2)

    def _handle_key(self, key):
        if key == pygame.K_ESCAPE:
            LOG.info("ESC — quitting")
            self.running = False
        elif key == pygame.K_SPACE:
            self._shutter()
        elif key == pygame.K_g:
            if self.mode == self.MODE_PLAYBACK:
                self._exit_playback()
            else:
                self._enter_playback()
        elif key == pygame.K_f:
            self.service.toggle_mode()
        elif key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            if self.mode == self.MODE_PLAYBACK:
                self.display.adjust_speed(faster=True)
        elif key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            if self.mode == self.MODE_PLAYBACK:
                self.display.adjust_speed(faster=False)

    def _drain_events(self):
        while True:
            try:
                kind, arg = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self.display.set_status(arg)
            elif kind == "progress":
                self.display.set_progress(arg)
            elif kind == "gif_ready":
                self.latest_gif = arg
                self.display.set_status("Saved %s" % os.path.basename(arg), 4)
                self._enter_playback()   # show off the result right away
            elif kind == "button":
                self._handle_button(arg)
            elif kind == "fatal":
                self.fatal_reason = arg

    def _handle_button(self, kind):
        if kind == "single":
            self._shutter()
        elif kind == "double":
            self._enter_playback()
        else:
            LOG.info("long tap — no action bound")

    # -------------------------------------------------------------- actions

    def _shutter(self):
        if self.mode == self.MODE_PLAYBACK:
            self._exit_playback()
        else:
            self.service.request_capture()

    def _enter_playback(self):
        if not self.latest_gif or not os.path.exists(self.latest_gif):
            self.display.set_status("No wigglegrams yet — shoot one first")
            return
        try:
            self.display.load_gif(self.latest_gif)
        except Exception as exc:
            LOG.error("cannot load %s: %s", self.latest_gif, exc)
            self.display.set_status("GIF load failed")
            return
        self.mode = self.MODE_PLAYBACK

    def _exit_playback(self):
        self.mode = self.MODE_LIVE

    # ------------------------------------------------------------- cleanup

    def shutdown(self):
        LOG.info("shutting down")
        self.buttons.stop()
        self.service.stop()
        self.service.join(timeout=5)
        if self.service.is_alive():
            LOG.warning("camera service still busy — killing worker directly")
            self.service.link.kill()
        self.display.close()
        LOG.info("bye")


def main():
    parser = argparse.ArgumentParser(description="Wigglegram camera")
    parser.add_argument("num_cams", nargs="?", type=int, default=3,
                        choices=(2, 3, 4),
                        help="how many cameras are connected (ports A, B, …)")
    parser.add_argument("--preview-mode", choices=("fast", "safe"),
                        default="fast",
                        help="fast keeps the stream running across mux "
                             "switches; safe reconfigures per frame")
    parser.add_argument("--windowed", action="store_true",
                        help="don't go fullscreen (development)")
    args = parser.parse_args()

    setup_logging()
    LOG.info("wigglecam starting: cams=%d preview=%s argv=%s",
             args.num_cams, args.preview_mode, sys.argv)

    app = None
    fatal = None
    try:
        app = App(args)
        # Flag-based shutdown: the main loop notices within one frame, so
        # Ctrl-C exits cleanly (workers, pygame and all) within a second or two.
        signal.signal(signal.SIGINT, lambda *_: setattr(app, "running", False))
        signal.signal(signal.SIGTERM, lambda *_: setattr(app, "running", False))
        fatal = app.run()
    except KeyboardInterrupt:
        LOG.info("KeyboardInterrupt")
    except Exception as exc:
        LOG.critical("unhandled exception: %s\n%s", exc, traceback.format_exc())
        fatal = "crash: %s" % exc

    if app is not None:
        if fatal:
            try:
                app.display.fatal_screen("Camera system restarting…")
                time.sleep(3)
            except Exception:
                pass
        app.shutdown()

    if fatal:
        if not self_restart(fatal):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
