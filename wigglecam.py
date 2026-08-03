#!/usr/bin/env python3
"""Wigglegram camera — entrypoint.

    sudo python3 wigglecam.py                   # all 4 ports (the default)
    sudo python3 wigglecam.py 3                 # only ports A, B, C
    sudo python3 wigglecam.py --preview-mode safe
    sudo python3 wigglecam.py --windowed        # development

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


def resolve_pics_dir(explicit=None):
    """Where photos go.

    Under sudo that's the invoking user's ~/piCameraPics. Under systemd
    there is no invoking user, so pass --pics-dir (see wigglecam.service).
    """
    if explicit:
        pics = os.path.abspath(os.path.expanduser(explicit))
    else:
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
    existed = os.path.isdir(pics)
    os.makedirs(pics, exist_ok=True)
    if not existed:
        # A freshly created directory would be root-owned; match its parent
        # so the photos stay reachable without sudo.
        try:
            parent = os.stat(os.path.dirname(pics))
            os.chown(pics, parent.st_uid, parent.st_gid)
        except Exception as exc:
            LOG.debug("cannot chown %s: %s", pics, exc)
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


def parse_cameras(spec):
    """"A,C" or "1,3" -> [0, 2]. Ports need not be contiguous: a rig with a
    dead port B is a perfectly good two-camera rig on A and C."""
    cams = []
    for token in spec.replace(" ", "").split(","):
        if not token:
            continue
        name = token.upper()
        if len(name) == 1 and name in ui.PORT_LETTERS:
            index = ui.PORT_LETTERS.index(name)
        elif name.isdigit() and 1 <= int(name) <= len(ui.PORT_LETTERS):
            index = int(name) - 1
        else:
            raise argparse.ArgumentTypeError(
                "%r is not a camera port — use letters A-D or numbers 1-4"
                % token)
        if index in cams:
            raise argparse.ArgumentTypeError("port %s listed twice"
                                             % ui.PORT_LETTERS[index])
        cams.append(index)
    if len(cams) < 2:
        raise argparse.ArgumentTypeError(
            "a wigglegram needs at least two cameras")
    return cams


def valid_size(spec):
    """Validate WIDTHxHEIGHT here so a typo fails at the prompt rather than
    three worker respawns later. The worker parses it again for real."""
    parts = str(spec).lower().split("x")
    if len(parts) != 2 or not all(p.isdigit() and int(p) >= 64 for p in parts):
        raise argparse.ArgumentTypeError(
            "expected WIDTHxHEIGHT, e.g. 4608x2592, not %r" % spec)
    return "%dx%d" % (int(parts[0]), int(parts[1]))


def valid_focus(spec):
    if str(spec).lower() == "auto":
        return "auto"
    try:
        dioptres = float(spec)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected 'auto' or a lens position in dioptres, not %r" % spec)
    if dioptres < 0:
        raise argparse.ArgumentTypeError("lens position cannot be negative")
    return str(dioptres)


class App:
    MODE_LIVE = "live"
    MODE_GALLERY = "gallery"

    def __init__(self, args):
        self.events = queue.Queue()
        self.pics_dir = resolve_pics_dir(args.pics_dir)
        self.view = args.view
        self.display = ui.DisplayManager(args.cams, windowed=args.windowed)
        self.service = CameraService(args.cams, args.preview_mode,
                                     self.pics_dir, self.events,
                                     view=args.view, rotate=args.rotate,
                                     tuning={"gpio_settle_ms": args.gpio_settle,
                                             "mux_settle_ms": args.mux_settle,
                                             "pin_raw": args.pin_raw,
                                             "still_size": args.still_size,
                                             "focus": args.focus})
        self.buttons = PiSugarButtons(self.events)
        self.mode = self.MODE_LIVE
        self.gifs = []               # gallery contents, newest first
        self.gif_idx = 0
        self.running = True
        self.fatal_reason = None
        # Set by SIGUSR1 and acted on by the main loop rather than in the
        # handler, so a shot can be fired over SSH. The on-screen button and
        # SPACE both go through pygame, which reads /dev/input directly and
        # so never sees anything typed into a VNC session.
        self.shutter_requested = False
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
        self.display.set_status(
            "Ready — tap the button to shoot"
            + ("" if self.view == "grid" else ", the image to switch camera"), 5)
        clock = pygame.time.Clock()
        while self.running:
            for event in pygame.event.get():
                self._handle_pygame_event(event)
            self._drain_events()
            if self.shutter_requested:
                self.shutter_requested = False
                LOG.info("shutter fired by SIGUSR1")
                self._shutter()
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
        if self.mode == self.MODE_LIVE:
            # With the corner thumbnails gone, tapping the viewfinder is how
            # you change camera by touch (1-4 still work on a keyboard).
            if self.view == "live":
                self.service.next_live_cam()
            return
        if down_pos is None:
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
    parser.add_argument("num_cams", nargs="?", type=int, default=4,
                        choices=(2, 3, 4),
                        help="how many cameras are connected, counting from "
                             "port A (2 = A and B). Use --cameras when the "
                             "ports you are using are not the first N.")
    parser.add_argument("--cameras", type=parse_cameras, default=None,
                        metavar="PORTS",
                        help="exactly which ports to use, e.g. 'A,C' or "
                             "'1,3' — overrides the count")
    parser.add_argument("--preview-mode", choices=("fast", "safe"),
                        default="fast",
                        help="fast keeps the stream running across mux "
                             "switches; safe reconfigures per frame")
    parser.add_argument("--pics-dir", default=None,
                        help="where to save photos and GIFs "
                             "(default: ~/piCameraPics of the sudo user; "
                             "required when started by systemd)")
    parser.add_argument("--rotate", type=int, choices=(0, 180), default=180,
                        help="camera image rotation; 180 (the default) suits "
                             "the upside-down mounting in the case")
    parser.add_argument("--view", choices=("live", "grid"), default="grid",
                        help="grid (the default) round-robins every camera "
                             "into a full-size tile; live streams one camera "
                             "and touches nothing else")
    parser.add_argument("--gpio-settle", type=float, default=None,
                        help="ms to wait after moving the mux select GPIOs "
                             "(worker default 5; raise it if a tile shows the "
                             "wrong camera's picture)")
    parser.add_argument("--mux-settle", type=float, default=None,
                        help="ms to wait after the mux I2C write "
                             "(worker default 25; raise it if libcamera "
                             "reports frontend timeouts)")
    parser.add_argument("--pin-raw", action="store_true",
                        help="make the viewfinder field of view match the "
                             "stills; costs preview rate on IMX708")
    parser.add_argument("--still-size", type=valid_size, default=None,
                        metavar="WxH",
                        help="photo resolution, e.g. 4608x2592 for a full "
                             "IMX708 frame (worker default 2304x1296, which "
                             "reads out four times faster and so keeps the "
                             "frames of a wigglegram closer together)")
    parser.add_argument("--focus", type=valid_focus, default=None,
                        help="'auto' (the default) autofocuses in the "
                             "viewfinder and freezes for the capture; give a "
                             "number instead to fix the lens, in dioptres "
                             "(0 = infinity, 3.3 ≈ 30 cm)")
    parser.add_argument("--windowed", action="store_true",
                        help="don't go fullscreen (development)")
    parser.add_argument("--verbose", action="store_true",
                        help="show full detail on the console too "
                             "(wigglecam.log always has it)")
    args = parser.parse_args()

    # FIRST, before pygame touches the console: SDL's framebuffer driver
    # grabs the VT during display init, and whoever else owns that console
    # hangs us up mid-startup. We have no terminal worth dying for.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    args.cams = args.cameras or list(range(args.num_cams))

    setup_logging(args.verbose)
    LOG.info("starting — cameras on ports %s, %s preview, %s view, rotated %d°",
             "/".join(ui.PORT_LETTERS[c] for c in args.cams),
             args.preview_mode, args.view, args.rotate)
    LOG.debug("argv=%s", sys.argv)

    app = None
    fatal = None
    try:
        app = App(args)
        # Flag-based shutdown: the main loop notices within one frame, so
        # Ctrl-C exits cleanly (workers, pygame and all) within a second or two.
        signal.signal(signal.SIGINT, lambda *_: setattr(app, "running", False))
        signal.signal(signal.SIGTERM, lambda *_: setattr(app, "running", False))
        # sudo pkill -USR1 -f wigglecam.py  — shoot without the touchscreen.
        signal.signal(signal.SIGUSR1,
                      lambda *_: setattr(app, "shutter_requested", True))
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
