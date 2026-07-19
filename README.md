# Wigglegram Camera

A self-contained handheld wigglegram camera: a Raspberry Pi 4 with an Arducam
Multi Camera Adapter V2.2 (2–4× IMX219, one CSI port, muxed), a FREENOVE
800×480 touchscreen as the viewfinder, and a PiSugar 3 Plus button as the
shutter. Shoot a burst across the cameras, get a bouncing 3D GIF.

Runs on **Raspberry Pi OS Bullseye only** (the Arducam adapter does not work
on newer releases). Python 3.9, pygame 1.9.6, apt-installed picamera2.

```
sudo python3 wigglecam.py              # all four ports (the default)
sudo python3 wigglecam.py 3            # only ports A, B, C
sudo python3 wigglecam.py --preview-mode safe
```

## Controls

A round shutter button sits in the top-left corner of the screen; the other
cameras' thumbnails occupy the remaining corners. Holding the button fills a
clock-style ring — when the circle completes, the gallery opens with every
wigglegram on the device (newest first), each playing its loop. The same
button (now a back arrow) returns to the camera.

| Action                  | PiSugar button | Keyboard | Touch |
|-------------------------|----------------|----------|-------|
| Capture wigglegram      | single tap     | SPACE    | tap the button |
| Open gallery            | double tap     | G        | hold the button ~1 s |
| (gallery) older / newer | —              | → / ←    | swipe left / right |
| (gallery) slower/faster | —              | - / +    | tap left / right half |
| (gallery) back to camera| single tap     | G        | tap the button |
| Switch live camera      | —              | 1–4      | tap a thumbnail |
| Toggle live/grid view   | —              | V        | —     |
| Toggle preview fast/safe| —              | F        | —     |
| Quit                    | —              | ESC      | —     |

Photos and GIFs land in `~/piCameraPics` (the invoking user's home, not
root's) as `<timestamp>_cam<N>.jpg` + `<timestamp>_wigglegram.gif`.

## Architecture

Two processes, connected by pipes:

```
┌───────────────────── UI process — wigglecam.py ──────────────────────┐
│ main thread     pygame: viewfinder / GIF playback / status, events   │
│ camera thread   camera_client.py — the ONLY talker to the worker;    │
│                 previews, capture sequences, GIF builds, health      │
│ pisugar thread  pisugar.py — daemon socket, else ≤2 Hz I2C poll      │
│ stderr thread   relays worker log lines into the main log            │
└─────────────┬─────────────────────────────────────────────────────────┘
              │ JSON commands ↓ stdin   /   JSON header + raw RGB ↑ stdout
┌─────────────┴──────────── camera worker — camera_worker.py ───────────┐
│ single-threaded; sole owner of GPIO mux, I2C 0x70, Picamera2, and     │
│ the I2C bus-clear recovery                                            │
└────────────────────────────────────────────────────────────────────────┘
```

### Why a separate camera process

Field experience: if `capture_array()` hangs on a dead camera port and you
abandon it from a timeout thread, the zombie thread holds Picamera2's locks —
`stop()`/`close()` deadlock forever and no new Picamera2 can be created in
that process ("Camera in Running state trying acquire()"). The only reliable
recovery is process death.

So the worker makes **blocking calls with no timeout threads at all**. If a
capture hangs, the whole worker hangs; the parent notices the missed deadline,
SIGKILLs it, and respawns a fresh process. The kernel reclaims the camera
unconditionally. The UI never freezes — previews stall ~2 s during a respawn
while touch, PiSugar and playback keep running. If the worker fails to respawn
3× in a row, the app re-execs itself (`os.execv`), with a restart-loop guard.

### Camera health model

Each camera is ALIVE, SUSPECT, or DEAD. A timeout marks it suspect; two in a
row mark it dead. Dead cameras show as offline tiles, get one retry every
30 s, and are skipped during capture (a GIF is still built from ≥2 good
frames). A flaky ribbon on one port never blocks the others.

### Viewfinder views

Every tile refresh costs a mux switch (~0.2 s verified settle + frame
flushes), so refreshing *all* tiles fast is physically impossible. Instead:

- **live** (default): ONE camera streams continuously — no mux switching
  between frames at all, so it runs at ~15–25 fps, like a real camera.
  The other cameras appear as corner thumbnails refreshed round-robin every
  ~10 s (each refresh is two mux switches, hence a brief hitch — rare by
  design). Tap a thumbnail or press 1–4 to change the live camera; if the
  live camera dies, the view hops to the next healthy one.
- **grid** (V key / `--view grid`): all cameras round-robin, each tile
  refreshing every ~1 s in fast mode. Useful for checking all ports at once.

### Preview strategies

- **fast** (default): the stream keeps running; the mux is switched live, two
  stale frames are flushed, the next is used. ~0.3 s per switched frame; in
  live view (no switching) it's whatever the pipe sustains, ~15–25 fps.
- **safe**: full stop → switch → configure → start → settle → capture → stop
  per frame (~0.6 s), the proven-reliable old method.

Three worker deaths while in fast mode auto-demote the session to safe.
`--preview-mode safe` or the F key force it.

### Hardware invariants (verified in the field — do not "simplify")

- Mux switch = GPIO **and** I2C, in that order, camera stopped (safe path):
  BCM 4/17/18 = select A / select B / OE, then write `0x04+n` to reg `0x00`
  at I2C `0x70`; OE must be driven HIGH before the first I2C contact; ~0.2 s
  settle after switching.
- A wedged I2C bus (everything errno 110) survives reboots; the worker
  recovers it in software: ~10 SCL pulses, a STOP condition, then
  `raspi-gpio set 2/3 a0 pu` to restore ALT0.
- PiSugar (0x57): prefer the pisugar-server socket, which *pushes*
  `single`/`double`/`long`; without it, poll reg `0x3A` (bit 4 single,
  bit 5 double, clear by writing the value back with the bits masked) at
  **2 Hz maximum** — faster polling is suspected of wedging the bus.
- picamera2 pixel formats use libcamera's inverted names: `BGR888` is
  R,G,B in memory (what PIL/pygame want); `RGB888` is B,G,R.

## Files

| file               | role                                                |
|--------------------|-----------------------------------------------------|
| `wigglecam.py`     | entrypoint: CLI, logging, app loop, self-restart    |
| `camera_worker.py` | child process: mux + Picamera2 + bus-clear          |
| `camera_client.py` | worker supervision, timeouts/respawn, capture flow  |
| `ui.py`            | pygame display: viewfinder grid, playback, status   |
| `pisugar.py`       | button input (socket or I2C poll)                   |
| `gif_builder.py`   | bounce-order GIF assembly                           |
| `setup.sh`         | fresh-install script for Bullseye                   |

## Logs

The console shows the short story in plain English — startup, captures,
camera health changes, warnings. A healthy session looks like:

```
14:02:11  starting — 3 cameras, fast preview, live view
14:02:13  camera engine running (pid 1234, fast preview)
14:02:13  ready — photos will be saved to /home/nsgill/piCameraPics
14:02:16  no PiSugar detected — SPACE key is the shutter
14:03:02  capture started (cameras 1, 2, 3)
14:03:08  wigglegram saved: 20260714-140302_wigglegram.gif (3 photos)
```

The full detail (every worker line, libcamera output, retries, timings)
goes to `wigglecam.log` (rotating, 1 MB × 3) with a logger name per
subsystem — **paste that file when reporting problems**, not the console.
`--verbose` mirrors the full detail to the console. Useful greps:

```
grep -E "WARN|ERROR" wigglecam.log        # anything unhappy
grep camlink wigglecam.log                # worker kills / respawns / timeouts
grep "worker]" wigglecam.log              # inside the camera process (mux, libcamera)
grep pisugar wigglecam.log                # button I2C behaviour
grep -i capture wigglecam.log             # shots and their outcomes
grep RESTART wigglecam.log                # self-restarts
```

## First-run checklist

1. `./setup.sh` on a fresh Bullseye image, then reboot.
2. `sudo python3 wigglecam.py 3`.
3. **Verify colors**: photograph something plainly red; if it comes out blue,
   the BGR888/RGB888 assumption needs flipping in `camera_worker.py`
   (`PIXEL_FORMAT`).
4. Check preview cadence in fast mode; if libcamera logs frontend timeouts,
   run with `--preview-mode safe`.
