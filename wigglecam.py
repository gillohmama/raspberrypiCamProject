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

HOLD_TO_GALLERY_S = 1.0      # hold the shutter button this long -> gallery
SWIPE_PX = 60                # horizontal travel that counts as a swipe


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


class _ConsoleFormatter(logging.Formatter):
    """Terse human-readable console lines: time + message, level name only
    when something is actually wrong."""

    def format(self, record):
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = ("" if record.levelno < logging.WARNING
                 else record.levelname + ": ")
        return "%s  %s%s" % (stamp, level, record.getMessage())


def setup_logging(verbose):
    """Console gets the short story; wigglecam.log gets everything."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "wigglecam.log")
    try:
        rotating = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3)
        rotating.setLevel(logging.DEBUG)
        rotating.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s",
            "%Y-%m-%d %H:%M:%S"))
        root.addHandler(rotating)
    except OSError as exc:
        root.warning("cannot open %s (%s) — console logging only", log_path, exc)
    logging.getLogger("PIL").setLevel(logging.INFO)


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
    MODE_GALLERY = "gallery"

    def __init__(self, args):
        self.events = queue.Queue()
        self.pics_dir = resolve_pics_dir()
        self.view = args.view
        self.display = ui.DisplayManager(args.num_cams, windowed=args.windowed)
        self.service = CameraService(args.num_cams, args.preview_mode,
                                     self.pics_dir, self.events,
                                     view=args.view)
        self.buttons = PiSugarButtons(self.events)
        self.mode = self.MODE_LIVE
        self.gifs = []               # gallery contents, newest first
        self.gif_idx = 0
        self.running = True
        self.fatal_reason = None
        # touch state for the on-screen button and swipes
        self._btn_held = False
        self._hold_done = False
        self._down_pos = None
        self._down_t = 0.0

    # ---------------------------------------------------------------- run

    def run(self):
        self.service.start()
        self.buttons.start()
        LOG.info("ready — photos will be saved to %s", self.pics_dir)
        self.display.set_status("Ready — tap the button or SPACE to shoot", 5)
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                self._handle_pygame_event(event)
            self._drain_events()
            hold = self._update_hold()
            if self.fatal_reason:
                break
            if self.mode == self.MODE_LIVE:
                self.display.draw_viewfinder(self.service.get_frames(),
                                             self.service.get_health(),
                                             self.service.preview_mode,
                                             self.view,
                                             self.service.live_cam,
                                             hold)
            else:
                self.display.draw_gallery(self.gif_idx, len(self.gifs))
            clock.tick(30)
        return self.fatal_reason

    def _update_hold(self):
        """Progress (0..1) of a shutter-button hold; opens the gallery when
        the clock animation completes."""
        if not self._btn_held or self._hold_done or self.mode != self.MODE_LIVE:
            return 0.0
        elapsed = time.time() - self._down_t
        if elapsed >= HOLD_TO_GALLERY_S:
            self._hold_done = True
            self._enter_gallery()
            return 0.0
        return elapsed / HOLD_TO_GALLERY_S

    # -------------------------------------------------------------- events

    def _handle_pygame_event(self, event):
        if event.type == pygame.QUIT:
            LOG.info("QUIT event")
            self.running = False
        elif event.type == pygame.KEYDOWN:
            self._handle_key(event.key)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            # Touchscreen taps arrive as mouse clicks.
            self._on_touch_down(event.pos)
        elif event.type == pygame.MOUSEBUTTONUP:
            self._on_touch_up(event.pos)

    def _on_touch_down(self, pos):
        self._down_pos = pos
        self._down_t = time.time()
        if self.display.hit_shutter(pos):
            self._btn_held = True
            self._hold_done = False
        elif self.mode == self.MODE_LIVE and self.view == "live":
            cam = self.display.hit_thumbnail(pos)
            if cam is not None:
                self.service.set_live_cam(cam)

    def _on_touch_up(self, pos):
        was_held, self._btn_held = self._btn_held, False
        down_pos, self._down_pos = self._down_pos, None
        if was_held:
            if self._hold_done:      # gallery already opened by the hold
                self._hold_done = False
                return
            if self.mode == self.MODE_GALLERY:
                self._exit_gallery()
            else:
                self.service.request_capture()
            return
        if self.mode != self.MODE_GALLERY or down_pos is None:
            return
        dx = pos[0] - down_pos[0]
        if abs(dx) >= SWIPE_PX:
            self._gallery_nav(1 if dx < 0 else -1)   # swipe left = older
        else:
            self.display.adjust_speed(faster=pos[0] >= ui.SCREEN_W // 2)

    def _handle_key(self, key):
        if key == pygame.K_ESCAPE:
            LOG.info("ESC — quitting")
            self.running = False
        elif key == pygame.K_SPACE:
            self._shutter()
        elif key == pygame.K_g:
            if self.mode == self.MODE_GALLERY:
                self._exit_gallery()
            else:
                self._enter_gallery()
        elif key == pygame.K_LEFT:
            if self.mode == self.MODE_GALLERY:
                self._gallery_nav(-1)
        elif key == pygame.K_RIGHT:
            if self.mode == self.MODE_GALLERY:
                self._gallery_nav(1)
        elif key == pygame.K_f:
            self.service.toggle_mode()
        elif key == pygame.K_v:
            self.view = "grid" if self.view == "live" else "live"
            self.service.set_view(self.view)
            self.display.set_status("View: %s" % self.view.upper(), 2)
        elif key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
            if self.mode == self.MODE_LIVE:
                self.service.set_live_cam(key - pygame.K_1)
        elif key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            if self.mode == self.MODE_GALLERY:
                self.display.adjust_speed(faster=True)
        elif key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            if self.mode == self.MODE_GALLERY:
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
                self.display.set_status("Saved %s" % os.path.basename(arg), 4)
                self._enter_gallery()   # newest first — shows the new one
            elif kind == "button":
                self._handle_button(arg)
            elif kind == "fatal":
                self.fatal_reason = arg

    def _handle_button(self, kind):
        if kind == "single":
            self._shutter()
        elif kind == "double":
            self._enter_gallery()
        else:
            LOG.info("long tap — no action bound")

    # -------------------------------------------------------------- actions

    def _shutter(self):
        if self.mode == self.MODE_GALLERY:
            self._exit_gallery()
        else:
            self.service.request_capture()

    def _scan_gifs(self):
        """All wigglegrams on disk, newest first."""
        try:
            gifs = [os.path.join(self.pics_dir, name)
                    for name in os.listdir(self.pics_dir)
                    if name.endswith("_wigglegram.gif")]
            gifs.sort(key=os.path.getmtime, reverse=True)
            return gifs
        except Exception:
            return []

    def _enter_gallery(self):
        self.gifs = self._scan_gifs()
        if not self.gifs:
            self.display.set_status("No wigglegrams yet — shoot one first")
            return
        self.gif_idx = 0
        if self._show_gif():
            self.mode = self.MODE_GALLERY

    def _exit_gallery(self):
        self.mode = self.MODE_LIVE

    def _show_gif(self):
        path = self.gifs[self.gif_idx]
        try:
            self.display.load_gif(path)
            return True
        except Exception as exc:
            LOG.error("cannot load %s: %s", path, exc)
            self.display.set_status("GIF load failed")
            return False

    def _gallery_nav(self, step):
        new_idx = max(0, min(len(self.gifs) - 1, self.gif_idx + step))
        if new_idx == self.gif_idx:
            return
        previous = self.gif_idx
        self.gif_idx = new_idx
        if not self._show_gif():
            self.gif_idx = previous

    # ------------------------------------------------------------- cleanup

    def shutdown(self):
        LOG.info("stopping…")
        self.buttons.stop()
        self.service.stop()
        self.service.join(timeout=5)
        if self.service.is_alive():
            LOG.debug("camera service still busy — killing worker directly")
            self.service.link.kill()
        self.display.close()
        LOG.info("stopped cleanly")


def main():
    parser = argparse.ArgumentParser(description="Wigglegram camera")
    parser.add_argument("num_cams", nargs="?", type=int, default=3,
                        choices=(2, 3, 4),
                        help="how many cameras are connected (ports A, B, …)")
    parser.add_argument("--preview-mode", choices=("fast", "safe"),
                        default="fast",
                        help="fast keeps the stream running across mux "
                             "switches; safe reconfigures per frame")
    parser.add_argument("--view", choices=("live", "grid"), default="live",
                        help="live streams one camera near-real-time with "
                             "thumbnails; grid round-robins all cameras")
    parser.add_argument("--windowed", action="store_true",
                        help="don't go fullscreen (development)")
    parser.add_argument("--verbose", action="store_true",
                        help="show full detail on the console too "
                             "(wigglecam.log always has it)")
    args = parser.parse_args()

    setup_logging(args.verbose)
    LOG.info("starting — %d cameras, %s preview, %s view",
             args.num_cams, args.preview_mode, args.view)
    LOG.debug("argv=%s", sys.argv)

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
