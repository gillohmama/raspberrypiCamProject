# Wigglegram Camera

A self-contained handheld wigglegram camera: a Raspberry Pi 4 with an Arducam
Multi Camera Adapter V2.2 (2–4× IMX219, one CSI port, muxed), a FREENOVE
800×480 touchscreen as the viewfinder, and a PiSugar 3 Plus button as the
shutter. Shoot a burst across the cameras, get a bouncing 3D GIF.

Runs on **Raspberry Pi OS Bullseye only** (the Arducam adapter does not work
on newer releases). Python 3.9, pygame 1.9.6, apt-installed picamera2.

```
sudo python3 wigglecam.py 3            # 3 cameras (ports A, B, C)
sudo python3 wigglecam.py 4            # all four ports
sudo python3 wigglecam.py 3 --preview-mode safe
```

## Controls

| Action                  | PiSugar button | Keyboard | Touch |
|-------------------------|----------------|----------|-------|
| Capture wigglegram      | single tap     | SPACE    | —     |
| Play latest GIF         | double tap     | G        | —     |
| (playback) slower/faster| —              | - / +    | left / right half |
| (playback) back to live | single tap     | G        | —     |
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

### Preview strategies

- **fast** (default): the stream keeps running; the mux is switched live, two
  stale frames are flushed, the next is used. ~0.3 s per frame.
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

Everything goes to stdout and `wigglecam.log` (rotating, 1 MB × 3) with
timestamps and a logger name per subsystem: `main`, `ui`, `camsvc`, `camlink`,
`worker`, `pisugar`, `gif`. Useful greps:

```
grep -E "WARN|ERROR" wigglecam.log        # anything unhappy
grep camlink wigglecam.log                # worker kills / respawns / timeouts
grep "worker]" wigglecam.log              # inside the camera process (mux, libcamera)
grep pisugar wigglecam.log                # button + battery I2C behaviour
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
