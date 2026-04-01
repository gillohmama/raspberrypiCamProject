#!/usr/bin/env python3
"""
Wigglegram Camera - Raspberry Pi 4
===================================
Hardware:
  - Arducam Multi Camera Adapter V2.2 (4 cameras on single CSI port)
  - FREENOVE 4.3" Touchscreen (800x480)
  - PiSugar 3 Plus battery pack & button

Controls:
  - Single click  (PiSugar button) : Capture all 4 cameras + create GIF
  - Double click  (PiSugar button) : Show most recent saved GIF
  - Touch right half of screen (GIF view) : Speed up GIF
  - Touch left  half of screen (GIF view) : Slow down GIF
  - Keyboard +/-  : Adjust GIF frame speed
  - Keyboard SPACE: Trigger capture (testing without button)
  - Keyboard G    : Show latest GIF  (testing without button)
  - Keyboard ESC  : Quit
"""

import os
import sys
import time
import socket
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import smbus2
import pygame
import numpy as np
import RPi.GPIO as GPIO
from PIL import Image
from picamera2 import Picamera2

# ============================================================
#  CONFIGURATION  -- edit values here
# ============================================================

SAVE_DIR = Path.home() / "piCameraPics"
SAVE_DIR.mkdir(exist_ok=True)

DISPLAY_WIDTH  = 800
DISPLAY_HEIGHT = 480

# Full-resolution stills saved per capture
PHOTO_WIDTH  = 1920
PHOTO_HEIGHT = 1080

# Preview resolution used for the live viewfinder quad
PREVIEW_WIDTH  = 400
PREVIEW_HEIGHT = 240

# Initial GIF frame duration (ms). Lower = faster animation.
GIF_SPEED_MS   = 150
GIF_SPEED_MIN  = 40    # fastest allowed
GIF_SPEED_MAX  = 1000  # slowest allowed
GIF_SPEED_STEP = 25    # how much each +/- press changes it

# Frame order for the wigglegram bounce: 1,2,3,4,3,2,1 (0-indexed)
GIF_FRAME_ORDER = [0, 1, 2, 3, 2, 1, 0]

NUM_CAMERAS = 4

# I2C --------------------------------------------------------
I2C_BUS = 1                 # standard RPi I2C bus (GPIO 2/3)

# Arducam Multi Camera Adapter V2.2
# Uses a TCA9548A-style mux — select a camera by writing a
# SINGLE byte (the channel bitmask) with no register address.
ARDUCAM_I2C_ADDR = 0x70

# PiSugar 3 Plus
PISUGAR_I2C_ADDR   = 0x57   # PiSugar 3 / 3 Plus I2C address
PISUGAR_BTN_REG    = 0x3A   # register holding button-tap flags
PISUGAR_SINGLE_BIT = 0x10   # bit 4 = single tap
PISUGAR_DOUBLE_BIT = 0x20   # bit 5 = double tap

# pisugar-server Unix socket (used when the daemon is running)
PISUGAR_SOCKET = "/tmp/pisugar-server.sock"

# ============================================================
#  Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wigglegram")


# ============================================================
#  Arducam Multi Camera Adapter  (I2C mux)
# ============================================================
class ArducamAdapter:
    """
    Controls the Arducam Multi Camera Adapter V2.2.

    Camera switching requires TWO things:
      1. I2C mux  (addr 0x70) — routes the camera's I2C config signals
      2. GPIO pins            — routes the actual CSI image data lanes

    Without the GPIO step the camera detects and configures fine over
    I2C but the sensor image data never reaches the Pi, causing the
    libcamera "frontend has timed out" error.

    GPIO pin mapping (BCM numbering, from AdapterTestDemo.py):
      GPIO 4  (board pin  7) = select bit A
      GPIO 17 (board pin 11) = select bit B
      GPIO 27 (board pin 12) = output enable (OE)

    Camera → (A, B, OE):
      0 → LOW,  LOW,  HIGH
      1 → HIGH, LOW,  HIGH
      2 → LOW,  HIGH, LOW
      3 → HIGH, HIGH, LOW
    """

    _SELECT_I2C = [0x04, 0x05, 0x06, 0x07]   # I2C register values for cam 0-3

    _GPIO_A  = 4    # board pin 7
    _GPIO_B  = 17   # board pin 11
    _GPIO_OE = 18   # board pin 12  (NOT 27 — board pin 12 = BCM 18)

    # (A, B, OE) for cameras 0-3
    _GPIO_CAM = [
        (GPIO.LOW,  GPIO.LOW,  GPIO.HIGH),
        (GPIO.HIGH, GPIO.LOW,  GPIO.HIGH),
        (GPIO.LOW,  GPIO.HIGH, GPIO.LOW),
        (GPIO.HIGH, GPIO.HIGH, GPIO.LOW),
    ]

    def __init__(self, bus: int = I2C_BUS, addr: int = ARDUCAM_I2C_ADDR):
        self._bus     = smbus2.SMBus(bus)
        self._addr    = addr
        self._current = -1

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(self._GPIO_A,  GPIO.OUT)
        GPIO.setup(self._GPIO_B,  GPIO.OUT)
        GPIO.setup(self._GPIO_OE, GPIO.OUT)

        log.info("ArducamAdapter ready  (bus=%d  addr=0x%02X  GPIO 4/17/18)", bus, addr)

    def select(self, cam: int) -> None:
        """Switch both the I2C mux and GPIO CSI mux to camera cam (0-3)."""
        if cam == self._current:
            return
        if not 0 <= cam < NUM_CAMERAS:
            raise ValueError(f"Camera index must be 0-{NUM_CAMERAS - 1}")
        try:
            # 1. GPIO — switch the CSI data lane mux
            a, b, oe = self._GPIO_CAM[cam]
            GPIO.output(self._GPIO_A,  a)
            GPIO.output(self._GPIO_B,  b)
            GPIO.output(self._GPIO_OE, oe)

            # 2. I2C — switch the camera config signal mux (reg 0x00, value 0x04-0x07)
            self._bus.write_byte_data(self._addr, 0x00, self._SELECT_I2C[cam])

            time.sleep(0.1)
            self._current = cam
            log.debug("Arducam: selected camera %d", cam)
        except OSError as exc:
            log.error("Arducam I2C write failed: %s", exc)
            raise

    def close(self) -> None:
        self._bus.close()
        GPIO.cleanup()


# ============================================================
#  Camera Manager
# ============================================================
class CameraManager:
    """
    Wraps a single persistent Picamera2 instance shared across all four
    cameras.  On Bullseye the mux channel is switched via I2C then the
    camera pipeline is stopped, reconfigured, and restarted — the same
    Picamera2 object is reused to avoid the overhead and timing issues
    of recreating it for every camera switch.
    """

    def __init__(self, adapter: ArducamAdapter) -> None:
        self._adapter  = adapter
        self._cam: Optional[Picamera2] = None
        self._current_cam = -1
        self._init_camera()

    # ------------------------------------------------------------------
    def _init_camera(self) -> None:
        """Create the Picamera2 instance once."""
        self._adapter.select(0)
        time.sleep(0.5)
        self._cam = Picamera2()
        self._current_cam = 0
        log.debug("Picamera2 instance created")

    def _switch_camera(self, cam: int) -> None:
        """Switch mux to cam, giving the hardware time to settle."""
        if cam == self._current_cam:
            return
        self._adapter.select(cam)
        time.sleep(0.8)   # generous settle time for CSI lane switch
        self._current_cam = cam

    def _close_camera(self) -> None:
        if self._cam is not None:
            try:
                if self._cam.started:
                    self._cam.stop()
                self._cam.close()
            except Exception:
                pass
            self._cam = None

    # ------------------------------------------------------------------
    def capture_preview(self, cam: int) -> np.ndarray:
        """
        Capture a low-resolution frame for the viewfinder.
        Returns an HxWx3 uint8 RGB array, or a black frame on failure.
        """
        try:
            if self._cam is None:
                self._init_camera()

            self._switch_camera(cam)

            if self._cam.started:
                self._cam.stop()

            cfg = self._cam.create_still_configuration(
                main={"size": (PREVIEW_WIDTH, PREVIEW_HEIGHT), "format": "RGB888"}
            )
            self._cam.configure(cfg)
            self._cam.start()
            time.sleep(0.3)   # let sensor exposure settle
            frame = self._cam.capture_array()
            self._cam.stop()
            return frame

        except Exception as exc:
            log.warning("Preview cam %d failed: %s", cam, exc)
            # Full reinit on failure
            self._close_camera()
            try:
                self._init_camera()
            except Exception:
                pass
            return np.zeros((PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8)

    def capture_photo(self, cam: int) -> Optional[Image.Image]:
        """
        Capture a full-resolution still from camera cam.
        Returns a PIL Image, or None on failure.
        """
        try:
            if self._cam is None:
                self._init_camera()

            self._switch_camera(cam)

            if self._cam.started:
                self._cam.stop()

            cfg = self._cam.create_still_configuration(
                main={"size": (PHOTO_WIDTH, PHOTO_HEIGHT), "format": "RGB888"}
            )
            self._cam.configure(cfg)
            self._cam.start()
            time.sleep(0.5)    # let AEC/AWB settle
            frame = self._cam.capture_array()
            self._cam.stop()
            return Image.fromarray(frame)

        except Exception as exc:
            log.error("Photo cam %d failed: %s", cam, exc)
            self._close_camera()
            return None

    def close(self) -> None:
        self._close_camera()


# ============================================================
#  GIF Creator
# ============================================================
def create_wigglegram_gif(
    images: List[Image.Image],
    path: str,
    frame_ms: int = GIF_SPEED_MS,
) -> str:
    """
    Build a wigglegram GIF from four PIL images.

    Frame order follows GIF_FRAME_ORDER so the animation bounces:
      cam1 → cam2 → cam3 → cam4 → cam3 → cam2 → cam1

    Saves to `path` and returns it.
    """
    frames = [images[i].convert("RGB") for i in GIF_FRAME_ORDER]

    # GIF requires palette mode
    pal_frames = [f.convert("P", palette=Image.ADAPTIVE, colors=256) for f in frames]

    pal_frames[0].save(
        path,
        save_all=True,
        append_images=pal_frames[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
    )
    log.info("GIF saved → %s  (%d frames @ %d ms)", path, len(pal_frames), frame_ms)
    return path


# ============================================================
#  PiSugar 3 Plus Button Monitor
# ============================================================
class PiSugarButton:
    """
    Detects single-click and double-click on the PiSugar 3 Plus
    custom button (the small button next to the power button).

    Strategy
    --------
    1. Try the pisugar-server Unix socket first (if the daemon is
       running).  Best option — handles debounce cleanly.
    2. Fall back to polling I2C register 0x3A directly.

    Callbacks
    ---------
    Set ``on_single_click`` and ``on_double_click`` before calling
    ``start()``.  Both are called from a background daemon thread.
    """

    def __init__(self) -> None:
        self.on_single_click = None
        self.on_double_click = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="pisugar-btn"
        )
        self._thread.start()
        log.info("PiSugarButton monitor started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        """Try socket first, then fall back to I2C polling."""
        if os.path.exists(PISUGAR_SOCKET):
            log.info("PiSugar: using pisugar-server socket")
            self._socket_loop()
        else:
            log.info("PiSugar: socket not found, using I2C polling")
            self._i2c_loop()

    # --- socket method ------------------------------------------------
    def _socket_loop(self) -> None:
        while self._running:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(PISUGAR_SOCKET)
                    sock.settimeout(1.0)
                    log.info("PiSugar: connected to %s", PISUGAR_SOCKET)

                    while self._running:
                        try:
                            sock.sendall(b"get button_tab\n")
                            raw = sock.recv(256).decode(errors="replace").strip()
                        except socket.timeout:
                            continue

                        if "double" in raw:
                            log.info("PiSugar: double-click")
                            sock.sendall(b"set_button_tab clear\n")
                            if self.on_double_click:
                                self.on_double_click()
                        elif "single" in raw:
                            log.info("PiSugar: single-click")
                            sock.sendall(b"set_button_tab clear\n")
                            if self.on_single_click:
                                self.on_single_click()

                        time.sleep(0.08)

            except (ConnectionRefusedError, FileNotFoundError):
                log.warning("PiSugar socket unavailable, switching to I2C polling")
                self._i2c_loop()
                return
            except Exception as exc:
                log.warning("PiSugar socket error: %s — retrying in 2 s", exc)
                time.sleep(2)

    # --- I2C fallback -------------------------------------------------
    def _i2c_loop(self) -> None:
        try:
            bus = smbus2.SMBus(I2C_BUS)
        except Exception as exc:
            log.error("Cannot open I2C bus for PiSugar: %s", exc)
            return

        log.info(
            "PiSugar: polling I2C addr=0x%02X reg=0x%02X",
            PISUGAR_I2C_ADDR, PISUGAR_BTN_REG,
        )

        while self._running:
            try:
                val = bus.read_byte_data(PISUGAR_I2C_ADDR, PISUGAR_BTN_REG)

                if val & PISUGAR_DOUBLE_BIT:
                    log.info("PiSugar: double-click (I2C raw=0x%02X)", val)
                    bus.write_byte_data(
                        PISUGAR_I2C_ADDR, PISUGAR_BTN_REG,
                        val & ~(PISUGAR_DOUBLE_BIT | PISUGAR_SINGLE_BIT),
                    )
                    if self.on_double_click:
                        self.on_double_click()

                elif val & PISUGAR_SINGLE_BIT:
                    log.info("PiSugar: single-click (I2C raw=0x%02X)", val)
                    bus.write_byte_data(
                        PISUGAR_I2C_ADDR, PISUGAR_BTN_REG,
                        val & ~PISUGAR_SINGLE_BIT,
                    )
                    if self.on_single_click:
                        self.on_single_click()

            except OSError as exc:
                log.warning("PiSugar I2C read error: %s", exc)
                time.sleep(0.5)

            time.sleep(0.05)   # ~20 Hz polling

        bus.close()


# ============================================================
#  Display Manager
# ============================================================
class DisplayManager:
    """
    Renders:
      • PREVIEW mode  – four equal quadrants, one per camera, updated
                        sequentially by the background preview thread.
      • GIF mode      – the wigglegram GIF plays full-screen.
      • Status overlay – short centred text messages.
    """

    QUAD_POSITIONS = [
        (0,                  0                  ),   # cam 0  top-left
        (DISPLAY_WIDTH // 2, 0                  ),   # cam 1  top-right
        (0,                  DISPLAY_HEIGHT // 2),   # cam 2  bottom-left
        (DISPLAY_WIDTH // 2, DISPLAY_HEIGHT // 2),   # cam 3  bottom-right
    ]
    QUAD_SIZE = (DISPLAY_WIDTH // 2, DISPLAY_HEIGHT // 2)

    def __init__(self) -> None:
        pygame.init()
        self.screen = pygame.display.set_mode(
            (DISPLAY_WIDTH, DISPLAY_HEIGHT),
            pygame.FULLSCREEN | pygame.NOFRAME,
        )
        pygame.display.set_caption("Wigglegram")
        pygame.mouse.set_visible(False)

        self._font_sm = pygame.font.SysFont("monospace", 16)
        self._font_lg = pygame.font.SysFont("monospace", 36, bold=True)

        self.mode = "preview"   # "preview" | "gif"

        self._preview_surfs: List[Optional[pygame.Surface]] = [None] * NUM_CAMERAS
        self._preview_lock = threading.Lock()

        self._gif_surfs: List[pygame.Surface] = []
        self._gif_idx  = 0
        self._gif_t    = 0.0
        self.gif_speed = GIF_SPEED_MS   # ms per frame, user-adjustable

        self._status_text   = ""
        self._status_expiry = 0.0

    # ------------------------------------------------------------------
    #  Public API
    # ------------------------------------------------------------------

    def update_preview(self, cam: int, frame: np.ndarray) -> None:
        """Called from the preview thread. Converts ndarray → Surface."""
        try:
            surf = pygame.surfarray.make_surface(np.swapaxes(frame, 0, 1))
            surf = pygame.transform.scale(surf, self.QUAD_SIZE)
            with self._preview_lock:
                self._preview_surfs[cam] = surf
        except Exception as exc:
            log.debug("Surface update failed cam %d: %s", cam, exc)

    def show_gif(self, path: str) -> None:
        """Load a GIF from path and switch to gif playback mode."""
        log.info("Loading GIF: %s", path)
        frames: List[pygame.Surface] = []
        try:
            gif = Image.open(path)
            while True:
                rgb = gif.convert("RGB").resize(
                    (DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS
                )
                surf = pygame.image.fromstring(rgb.tobytes(), rgb.size, "RGB")
                frames.append(surf)
                gif.seek(gif.tell() + 1)
        except EOFError:
            pass
        except Exception as exc:
            log.error("Failed to load GIF: %s", exc)
            self.set_status("Could not load GIF", 2.0)
            return

        if frames:
            self._gif_surfs = frames
            self._gif_idx   = 0
            self._gif_t     = time.time()
            self.mode       = "gif"
            log.info("GIF loaded: %d frames", len(frames))

    def show_preview(self) -> None:
        self.mode = "preview"

    def set_status(self, msg: str, secs: float = 2.5) -> None:
        self._status_text   = msg
        self._status_expiry = time.time() + secs

    def adjust_gif_speed(self, delta: int) -> None:
        """delta > 0 slows down; delta < 0 speeds up."""
        self.gif_speed = max(GIF_SPEED_MIN, min(GIF_SPEED_MAX, self.gif_speed + delta))
        log.info("GIF speed → %d ms/frame", self.gif_speed)

    def draw(self) -> None:
        """Call once per frame from the main loop."""
        self.screen.fill((0, 0, 0))

        if self.mode == "preview":
            self._draw_preview()
        else:
            self._draw_gif()

        self._draw_status()
        pygame.display.flip()

    # ------------------------------------------------------------------
    #  Private draw helpers
    # ------------------------------------------------------------------

    def _draw_preview(self) -> None:
        with self._preview_lock:
            surfs = list(self._preview_surfs)

        for i, (x, y) in enumerate(self.QUAD_POSITIONS):
            if surfs[i]:
                self.screen.blit(surfs[i], (x, y))
            else:
                rect = pygame.Rect(
                    x + 2, y + 2,
                    self.QUAD_SIZE[0] - 4,
                    self.QUAD_SIZE[1] - 4,
                )
                pygame.draw.rect(self.screen, (30, 30, 30), rect)
                lbl = self._font_sm.render(f"CAM {i + 1}", True, (120, 120, 120))
                self.screen.blit(lbl, (x + 8, y + 8))

        # Dividing grid lines
        mid_x, mid_y = DISPLAY_WIDTH // 2, DISPLAY_HEIGHT // 2
        pygame.draw.line(self.screen, (70, 70, 70), (mid_x, 0), (mid_x, DISPLAY_HEIGHT))
        pygame.draw.line(self.screen, (70, 70, 70), (0, mid_y), (DISPLAY_WIDTH, mid_y))

        # Speed HUD
        hud = self._font_sm.render(
            f"GIF {self.gif_speed} ms/frame   [+/-] adjust",
            True, (160, 160, 160),
        )
        self._blit_with_bg(hud, 4, DISPLAY_HEIGHT - hud.get_height() - 4, alpha=110)

    def _draw_gif(self) -> None:
        now = time.time()
        if self._gif_surfs:
            if (now - self._gif_t) * 1000 >= self.gif_speed:
                self._gif_idx = (self._gif_idx + 1) % len(self._gif_surfs)
                self._gif_t   = now
            self.screen.blit(self._gif_surfs[self._gif_idx], (0, 0))

        speed_txt = self._font_sm.render(
            f"{self.gif_speed} ms/frame  [tap L=slower  R=faster]",
            True, (220, 220, 220),
        )
        self._blit_with_bg(speed_txt, 4, 4, alpha=130)

        hint = self._font_sm.render(
            "Single-click → back to preview",
            True, (200, 200, 200),
        )
        self._blit_with_bg(hint, 4, DISPLAY_HEIGHT - hint.get_height() - 4, alpha=130)

    def _draw_status(self) -> None:
        if not self._status_text or time.time() > self._status_expiry:
            return
        txt = self._font_lg.render(self._status_text, True, (255, 230, 60))
        x = (DISPLAY_WIDTH  - txt.get_width())  // 2
        y = (DISPLAY_HEIGHT - txt.get_height()) // 2
        self._blit_with_bg(txt, x, y, pad=12, alpha=170)

    def _blit_with_bg(
        self,
        surface: pygame.Surface,
        x: int,
        y: int,
        pad: int = 6,
        alpha: int = 140,
    ) -> None:
        bg = pygame.Surface(
            (surface.get_width() + pad * 2, surface.get_height() + pad * 2),
            pygame.SRCALPHA,
        )
        bg.fill((0, 0, 0, alpha))
        self.screen.blit(bg, (x - pad, y - pad))
        self.screen.blit(surface, (x, y))

    def close(self) -> None:
        pygame.quit()


# ============================================================
#  Main Application
# ============================================================
class WigglegramApp:
    """Wires together all components and runs the main event loop."""

    def __init__(self) -> None:
        self._running   = False
        self._capturing = False

        log.info("Initialising hardware…")
        self._adapter = ArducamAdapter()
        self._cameras = CameraManager(self._adapter)
        self._display = DisplayManager()
        self._button  = PiSugarButton()

        self._button.on_single_click = self._on_single_click
        self._button.on_double_click = self._on_double_click

        self._latest_gif: Optional[str] = self._find_latest_gif()
        if self._latest_gif:
            log.info("Latest GIF on disk: %s", self._latest_gif)

    # ------------------------------------------------------------------
    #  Button callbacks
    # ------------------------------------------------------------------

    def _on_single_click(self) -> None:
        """Single click: capture + create GIF.
        If already in GIF view, return to preview instead."""
        if self._display.mode == "gif":
            self._display.show_preview()
            return
        if not self._capturing:
            threading.Thread(
                target=self._capture_sequence,
                daemon=True,
                name="capture",
            ).start()

    def _on_double_click(self) -> None:
        """Double click: show the most recently saved GIF."""
        if self._latest_gif and os.path.exists(self._latest_gif):
            self._display.set_status("Loading…", 1.5)
            threading.Thread(
                target=self._display.show_gif,
                args=(self._latest_gif,),
                daemon=True,
            ).start()
        else:
            self._display.set_status("No GIF saved yet!", 2.5)

    # ------------------------------------------------------------------
    #  Capture sequence
    # ------------------------------------------------------------------

    def _capture_sequence(self) -> None:
        """Capture 4 photos, save them, then create a wigglegram GIF."""
        self._capturing = True
        images: List[Image.Image] = []
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        try:
            for cam in range(NUM_CAMERAS):
                self._display.set_status(f"Capturing {cam + 1} / {NUM_CAMERAS}…", 4.0)
                img = self._cameras.capture_photo(cam)
                if img is None:
                    self._display.set_status(f"Cam {cam + 1} failed!", 3.0)
                    return

                path = SAVE_DIR / f"{timestamp}_cam{cam + 1}.jpg"
                img.save(str(path), "JPEG", quality=95)
                log.info("Saved still → %s", path)
                images.append(img)

            self._display.set_status("Building GIF…", 3.0)
            gif_path = str(SAVE_DIR / f"{timestamp}_wigglegram.gif")
            create_wigglegram_gif(images, gif_path, self._display.gif_speed)

            self._latest_gif = gif_path
            self._display.set_status("Done!  Double-click to preview", 3.0)

        except Exception as exc:
            log.exception("Capture sequence failed")
            self._display.set_status(f"Error: {str(exc)[:28]}", 3.0)
        finally:
            self._capturing = False

    # ------------------------------------------------------------------
    #  Preview loop (background thread)
    # ------------------------------------------------------------------

    def _preview_loop(self) -> None:
        """
        Continuously cycles cameras 1→2→3→4→1… capturing a preview
        frame from each and pushing it to the display quadrant.
        Pauses while a full capture is in progress.
        """
        cam = 0
        while self._running:
            if self._capturing or self._display.mode == "gif":
                time.sleep(0.1)
                continue
            frame = self._cameras.capture_preview(cam)
            self._display.update_preview(cam, frame)
            cam = (cam + 1) % NUM_CAMERAS

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_latest_gif() -> Optional[str]:
        gifs = sorted(
            SAVE_DIR.glob("*_wigglegram.gif"),
            key=os.path.getmtime,
            reverse=True,
        )
        return str(gifs[0]) if gifs else None

    # ------------------------------------------------------------------
    #  Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._running = True
        self._button.start()

        preview_thread = threading.Thread(
            target=self._preview_loop, daemon=True, name="preview"
        )
        preview_thread.start()

        self._display.set_status("Wigglegram Ready", 2.5)
        clock = pygame.time.Clock()

        try:
            while self._running:
                for event in pygame.event.get():
                    self._handle_event(event)
                self._display.draw()
                clock.tick(30)
        finally:
            self._running = False
            self._shutdown()

    def _handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self._running = False

        elif event.type == pygame.KEYDOWN:
            key = event.key
            if key == pygame.K_ESCAPE:
                self._running = False
            elif key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                self._display.adjust_gif_speed(-GIF_SPEED_STEP)   # faster
            elif key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                self._display.adjust_gif_speed(+GIF_SPEED_STEP)   # slower
            elif key == pygame.K_SPACE:
                self._on_single_click()
            elif key == pygame.K_g:
                self._on_double_click()

        elif event.type == pygame.MOUSEBUTTONDOWN:
            x, _y = event.pos
            if self._display.mode == "gif":
                # Left half → slower, right half → faster
                if x < DISPLAY_WIDTH // 2:
                    self._display.adjust_gif_speed(+GIF_SPEED_STEP)
                else:
                    self._display.adjust_gif_speed(-GIF_SPEED_STEP)

    def _shutdown(self) -> None:
        log.info("Shutting down…")
        self._button.stop()
        self._cameras.close()
        self._adapter.close()
        self._display.close()


# ============================================================
#  Entry Point
# ============================================================
if __name__ == "__main__":
    try:
        app = WigglegramApp()
        app.run()
    except KeyboardInterrupt:
        log.info("Stopped by user (Ctrl-C)")
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)
