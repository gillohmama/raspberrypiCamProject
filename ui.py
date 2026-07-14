#!/usr/bin/env python3
"""pygame display: viewfinder grid, GIF playback, status overlay.

Targets pygame 1.9.6 (SDL1) on Bullseye — stick to the old-school API:
no pygame 2 flags, default font only, frombuffer with "RGB".
"""

import logging
import math
import time

import pygame
from PIL import Image, ImageSequence

LOG = logging.getLogger("ui")

SCREEN_W, SCREEN_H = 800, 480
STATUS_H = 30
PORT_LETTERS = "ABCD"

MIN_FRAME_MS = 40
MAX_FRAME_MS = 1000
DEFAULT_FRAME_MS = 150
SPEED_STEP = 1.25

THUMB_W, THUMB_H = 144, 81      # 16:9 like the preview frames
THUMB_MARGIN = 8

# On-screen shutter button, top-left corner. Tap = shoot; hold = the pie
# fills like a clock and the gallery opens; in the gallery it's the back
# button.
SHUTTER_CENTER = (48, 48)
SHUTTER_R = 36

BG = (12, 12, 16)
TILE_BG = (24, 24, 30)
TILE_DEAD_BG = (56, 14, 14)
BAR_BG = (22, 22, 26)
PANEL_BG = (18, 18, 24)
TEXT = (220, 220, 220)
TEXT_DIM = (150, 150, 158)
TEXT_OK = (90, 220, 90)
TEXT_BAD = (235, 90, 90)
TEXT_MODE = (150, 150, 225)
TEXT_FLASH = (255, 230, 80)


class DisplayManager:
    def __init__(self, num_cams, windowed=False):
        pygame.display.init()
        pygame.font.init()
        flags = 0 if windowed else pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), flags)
        pygame.display.set_caption("wigglecam")
        pygame.mouse.set_visible(False)
        self.num_cams = num_cams
        self.font = pygame.font.Font(None, 24)
        self.font_big = pygame.font.Font(None, 44)
        self.font_small = pygame.font.Font(None, 18)
        self.tiles, self.info_rect = self._layout(num_cams)
        self._tile_cache = {}          # cam -> (seq, scaled surface)
        self._thumb_hits = []          # [(rect, cam)] from the last live draw
        self.status_msg = ""
        self.status_until = 0.0
        self.progress_msg = None       # sticky banner while capturing
        # playback state
        self.gif_frames = []
        self.frame_ms = DEFAULT_FRAME_MS
        self._gif_idx = 0
        self._gif_next_t = 0.0
        self._speed_flash_until = 0.0
        LOG.debug("display up: %dx%d %s, %d camera tiles", SCREEN_W, SCREEN_H,
                  "windowed" if windowed else "fullscreen", num_cams)

    @staticmethod
    def _layout(n):
        """Tile rects per camera + an optional spare rect for the info panel."""
        area_h = SCREEN_H - STATUS_H                       # 450
        if n == 2:
            return [pygame.Rect(0, 0, 400, area_h),
                    pygame.Rect(400, 0, 400, area_h)], None
        half_h = area_h // 2                               # 225 — 16:9 at 400 wide
        cells = [pygame.Rect(0, 0, 400, half_h),
                 pygame.Rect(400, 0, 400, half_h),
                 pygame.Rect(0, half_h, 400, half_h),
                 pygame.Rect(400, half_h, 400, half_h)]
        if n == 3:
            return cells[:3], cells[3]
        return cells[:4], None

    @staticmethod
    def _fit(rect, w, h):
        """Largest w:h-shaped rect that fits inside rect, centered."""
        scale = min(rect.w / float(w), rect.h / float(h))
        fw, fh = max(1, int(w * scale)), max(1, int(h * scale))
        return pygame.Rect(rect.x + (rect.w - fw) // 2,
                           rect.y + (rect.h - fh) // 2, fw, fh)

    def _text(self, msg, color, font=None, center_in=None, topleft=None):
        surf = (font or self.font).render(msg, True, color)
        if center_in is not None:
            self.screen.blit(surf, surf.get_rect(center=center_in.center))
        elif topleft is not None:
            self.screen.blit(surf, topleft)
        return surf

    # ------------------------------------------------------------ messages

    def set_status(self, msg, secs=4.0):
        self.status_msg = msg
        self.status_until = time.time() + secs

    def set_progress(self, msg):
        """Sticky center banner (capture progress); None clears it."""
        self.progress_msg = msg

    # ---------------------------------------------------------- viewfinder

    def draw_viewfinder(self, frames, health, preview_mode,
                        view="grid", live_cam=0, hold_progress=0.0):
        self.screen.fill(BG)
        if view == "live":
            self._draw_live(frames, health, live_cam)
        else:
            for cam, rect in enumerate(self.tiles):
                label_pos = None
                if rect.topleft == (0, 0):   # make room for the button
                    label_pos = (SHUTTER_CENTER[0] + SHUTTER_R + 10, rect.y + 4)
                self._draw_tile(cam, rect, frames.get(cam),
                                health.get(cam, "alive"), label_pos=label_pos)
            if self.info_rect is not None:
                self._draw_info_panel(self.info_rect, preview_mode)
        if self.progress_msg:
            self._draw_banner(self.progress_msg)
        self._draw_status_bar(health, preview_mode)
        self._draw_shutter(hold_progress)
        pygame.display.flip()

    def _draw_live(self, frames, health, live_cam):
        area = pygame.Rect(0, 0, SCREEN_W, SCREEN_H - STATUS_H)
        self._draw_tile(live_cam, area, frames.get(live_cam),
                        health.get(live_cam, "alive"), live=True,
                        label_pos=(SHUTTER_CENTER[0] + SHUTTER_R + 10, 10))
        # The other cameras take the free corners (top-left is the button's).
        corners = [
            (SCREEN_W - THUMB_MARGIN - THUMB_W, THUMB_MARGIN),
            (SCREEN_W - THUMB_MARGIN - THUMB_W,
             area.bottom - THUMB_MARGIN - THUMB_H),
            (THUMB_MARGIN, area.bottom - THUMB_MARGIN - THUMB_H),
        ]
        others = [c for c in range(self.num_cams) if c != live_cam]
        self._thumb_hits = []
        for corner, cam in zip(corners, others):
            rect = pygame.Rect(corner, (THUMB_W, THUMB_H))
            self._draw_thumb(cam, rect, frames.get(cam),
                             health.get(cam, "alive"))
            self._thumb_hits.append((rect, cam))

    def _draw_thumb(self, cam, rect, frame, state):
        alive = state == "alive"
        if not alive:
            pygame.draw.rect(self.screen, TILE_DEAD_BG, rect)
        elif frame is None:
            pygame.draw.rect(self.screen, TILE_BG, rect)
        else:
            seq, w, h, data = frame
            cached = self._tile_cache.get(cam)
            if (cached is None or cached[0] != seq
                    or cached[1].get_size() != rect.size):
                surf = pygame.image.frombuffer(data, (w, h), "RGB")
                surf = pygame.transform.smoothscale(surf, rect.size)
                self._tile_cache[cam] = (seq, surf)
            self.screen.blit(self._tile_cache[cam][1], rect)
        pygame.draw.rect(self.screen,
                         (200, 200, 200) if alive else TEXT_BAD, rect, 1)
        label = "%d (%s)%s" % (cam + 1, PORT_LETTERS[cam],
                               "" if alive else " OFF")
        self._text(label, TEXT if alive else TEXT_BAD, font=self.font_small,
                   topleft=(rect.x + 4, rect.y + 3))

    def hit_thumbnail(self, pos):
        """Camera index of the thumbnail under a tap, or None."""
        for rect, cam in self._thumb_hits:
            if rect.collidepoint(pos):
                return cam
        return None

    def hit_shutter(self, pos):
        """True if a tap landed on the shutter/back button (finger-sized slop)."""
        dx = pos[0] - SHUTTER_CENTER[0]
        dy = pos[1] - SHUTTER_CENTER[1]
        return dx * dx + dy * dy <= (SHUTTER_R + 10) ** 2

    def _draw_shutter(self, hold_progress=0.0, back=False):
        center = SHUTTER_CENTER
        pygame.draw.circle(self.screen, (30, 30, 36), center, SHUTTER_R)
        pygame.draw.circle(self.screen, (235, 235, 235), center, SHUTTER_R, 2)
        if back:
            points = [(center[0] + 10, center[1] - 14),
                      (center[0] + 10, center[1] + 14),
                      (center[0] - 14, center[1])]
            pygame.draw.polygon(self.screen, (235, 235, 235), points)
        else:
            pygame.draw.circle(self.screen, (235, 235, 235), center, 14)
        if hold_progress > 0.0:
            self._draw_hold_pie(center, SHUTTER_R - 6, min(1.0, hold_progress))

    def _draw_hold_pie(self, center, radius, progress):
        """Clock-wipe: a filled pie from 12 o'clock sweeping clockwise."""
        steps = max(2, int(progress * 48))
        points = [center]
        sweep = progress * 2.0 * math.pi
        for i in range(steps + 1):
            angle = -math.pi / 2 + sweep * i / steps
            points.append((int(center[0] + radius * math.cos(angle)),
                           int(center[1] + radius * math.sin(angle))))
        pygame.draw.polygon(self.screen, (255, 200, 60), points)

    def _draw_tile(self, cam, rect, frame, state, live=False, label_pos=None):
        label = "cam%d (%s)" % (cam + 1, PORT_LETTERS[cam])
        if live:
            label = "LIVE  " + label
        if state == "dead":
            pygame.draw.rect(self.screen, TILE_DEAD_BG, rect)
            self._text("%s OFFLINE" % label, TEXT_BAD, center_in=rect)
            sub = pygame.Rect(rect.x, rect.y + 26, rect.w, rect.h)
            self._text("retrying every 30 s", TEXT_DIM, center_in=sub)
        elif frame is None:
            pygame.draw.rect(self.screen, TILE_BG, rect)
            self._text("%s waiting…" % label, TEXT_DIM, center_in=rect)
        else:
            seq, w, h, data = frame
            dest = self._fit(rect, w, h)
            cached = self._tile_cache.get(cam)
            if (cached is None or cached[0] != seq
                    or cached[1].get_size() != dest.size):
                surf = pygame.image.frombuffer(data, (w, h), "RGB")
                # The live view is screen-native (800x450) — skip the scale.
                if dest.size != (w, h):
                    surf = pygame.transform.smoothscale(surf, dest.size)
                self._tile_cache[cam] = (seq, surf)
            if dest.size != rect.size:
                pygame.draw.rect(self.screen, TILE_BG, rect)
            self.screen.blit(self._tile_cache[cam][1], dest)
        surf = self.font.render(label, True, (255, 255, 255))
        lx, ly = label_pos if label_pos else (rect.x + 6, rect.y + 4)
        backing = pygame.Rect(lx - 6, ly - 4,
                              surf.get_width() + 12, surf.get_height() + 8)
        pygame.draw.rect(self.screen, PANEL_BG, backing)
        self.screen.blit(surf, (lx, ly))

    def _draw_info_panel(self, rect, preview_mode):
        pygame.draw.rect(self.screen, PANEL_BG, rect)
        lines = [
            ("WIGGLECAM", TEXT),
            ("preview: %s" % preview_mode.upper(), TEXT_MODE),
            ("", TEXT_DIM),
            ("button / SPACE   shoot", TEXT_DIM),
            ("hold button / G  gallery", TEXT_DIM),
            ("V                live/grid view", TEXT_DIM),
            ("1-4              live camera", TEXT_DIM),
            ("F                preview mode", TEXT_DIM),
            ("ESC              quit", TEXT_DIM),
        ]
        y = rect.y + 14
        for msg, color in lines:
            if msg:
                self._text(msg, color, topleft=(rect.x + 16, y))
            y += 26

    def _draw_status_bar(self, health, preview_mode):
        bar = pygame.Rect(0, SCREEN_H - STATUS_H, SCREEN_W, STATUS_H)
        pygame.draw.rect(self.screen, BAR_BG, bar)
        if time.time() < self.status_until and self.status_msg:
            self._text(self.status_msg, TEXT, topleft=(8, bar.y + 6))
        x = SCREEN_W - 8
        for cam in range(self.num_cams - 1, -1, -1):
            ok = health.get(cam, "alive") == "alive"
            surf = self.font.render(str(cam + 1), True,
                                    TEXT_OK if ok else TEXT_BAD)
            x -= surf.get_width() + 8
            self.screen.blit(surf, (x, bar.y + 6))
        surf = self.font.render(preview_mode.upper(), True, TEXT_MODE)
        x -= surf.get_width() + 16
        self.screen.blit(surf, (x, bar.y + 6))

    def _draw_banner(self, msg):
        banner = pygame.Rect(0, (SCREEN_H - 70) // 2, SCREEN_W, 70)
        overlay = pygame.Surface((banner.w, banner.h))
        overlay.set_alpha(210)
        overlay.fill((0, 0, 0))
        self.screen.blit(overlay, banner.topleft)
        self._text(msg, (255, 255, 255), font=self.font_big, center_in=banner)

    # ------------------------------------------------------------ playback

    def load_gif(self, path):
        image = Image.open(path)
        frames = []
        for frame in ImageSequence.Iterator(image):
            rgb = frame.convert("RGB")
            scale = min(SCREEN_W / float(rgb.width), SCREEN_H / float(rgb.height))
            size = (max(1, int(rgb.width * scale)),
                    max(1, int(rgb.height * scale)))
            rgb = rgb.resize(size, Image.BILINEAR)
            frames.append(pygame.image.frombuffer(rgb.tobytes(), size, "RGB"))
        if not frames:
            raise ValueError("no frames in %s" % path)
        self.gif_frames = frames
        self._gif_idx = 0
        self._gif_next_t = 0.0
        LOG.debug("loaded %s (%d frames)", path, len(frames))

    def draw_gallery(self, index, total):
        now = time.time()
        if now >= self._gif_next_t:
            self._gif_idx = (self._gif_idx + 1) % len(self.gif_frames)
            self._gif_next_t = now + self.frame_ms / 1000.0
        self.screen.fill((0, 0, 0))
        frame = self.gif_frames[self._gif_idx]
        self.screen.blit(frame,
                         frame.get_rect(center=(SCREEN_W // 2, SCREEN_H // 2)))
        header = pygame.Rect(0, 8, SCREEN_W, 24)
        self._text("%d / %d" % (index + 1, total), TEXT, center_in=header)
        if now < self._speed_flash_until:
            flash = pygame.Rect(0, SCREEN_H - 70, SCREEN_W, 50)
            self._text("%d ms/frame" % self.frame_ms, TEXT_FLASH,
                       font=self.font_big, center_in=flash)
        if now < self.status_until and self.status_msg:
            below = pygame.Rect(0, 34, SCREEN_W, 24)
            self._text(self.status_msg, TEXT, center_in=below)
        self._draw_shutter(back=True)
        pygame.display.flip()

    def adjust_speed(self, faster):
        ms = self.frame_ms / SPEED_STEP if faster else self.frame_ms * SPEED_STEP
        self.frame_ms = int(min(MAX_FRAME_MS, max(MIN_FRAME_MS, round(ms))))
        self._speed_flash_until = time.time() + 1.5
        LOG.debug("playback speed: %d ms/frame", self.frame_ms)

    # -------------------------------------------------------------- fatal

    def fatal_screen(self, msg):
        self.screen.fill((44, 10, 10))
        y = SCREEN_H // 2 - 24 * len(str(msg).split("\n"))
        for line in str(msg).split("\n"):
            surf = self.font_big.render(line, True, (255, 215, 215))
            self.screen.blit(surf, (SCREEN_W // 2 - surf.get_width() // 2, y))
            y += 48
        pygame.display.flip()

    def close(self):
        pygame.quit()
