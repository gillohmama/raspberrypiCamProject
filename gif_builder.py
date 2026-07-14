#!/usr/bin/env python3
"""Assemble a bounce-order wigglegram GIF from still JPEGs."""

import logging

from PIL import Image

LOG = logging.getLogger("gif")

GIF_WIDTH = 800            # matches the screen; keeps files small
DEFAULT_FRAME_MS = 150


def bounce_order(n):
    """0,1,...,n-1,n-2,...,1 — seamless when looped (3 cams: 0,1,2,1)."""
    if n <= 2:
        return list(range(n))
    return list(range(n)) + list(range(n - 2, 0, -1))


def build(jpeg_paths, out_path, frame_ms=DEFAULT_FRAME_MS, progress=None):
    frames = []
    for i, path in enumerate(jpeg_paths):
        if progress:
            progress("GIF: reading frame %d/%d…" % (i + 1, len(jpeg_paths)))
        image = Image.open(path).convert("RGB")
        height = max(1, round(GIF_WIDTH * image.height / image.width))
        frames.append(image.resize((GIF_WIDTH, height), Image.BILINEAR))

    sequence = [frames[i] for i in bounce_order(len(frames))]
    if progress:
        progress("GIF: encoding…")
    sequence[0].save(out_path, save_all=True, append_images=sequence[1:],
                     duration=frame_ms, loop=0)
    LOG.info("wrote %s (%d frames, %d ms/frame)",
             out_path, len(sequence), frame_ms)
    return out_path
