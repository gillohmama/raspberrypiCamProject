# Wigglegram Camera

A self-contained handheld wigglegram camera: a Raspberry Pi 4 with an Arducam
Multi Camera Adapter V2.2 (2–4 cameras, one CSI port, muxed), a FREENOVE
800×480 touchscreen as the viewfinder, and a PiSugar 3 Plus button as the
shutter. Shoot a burst across the cameras, get a bouncing 3D GIF.

Currently running 4× Raspberry Pi Camera Module 3 (IMX708, 12MP, autofocus).
IMX219 works too; the two cannot be mixed.

Runs on **Raspberry Pi OS Bullseye only** (the Arducam adapter does not work
on newer releases). Python 3.9, pygame 1.9.6, apt-installed picamera2.

```
sudo python3 wigglecam.py              # two cameras, grid view (the default)
sudo python3 wigglecam.py --cameras A,C   # these two ports specifically
sudo python3 wigglecam.py 4            # all four ports
sudo python3 wigglecam.py --view live  # one camera, full screen
sudo python3 wigglecam.py --preview-mode safe
sudo python3 wigglecam.py --rotate 0   # cameras mounted the right way up
sudo python3 wigglecam.py --mux-settle 100 --gpio-settle 50   # back off the timing
```

The bare number counts ports from A, so `2` means A and B. When the ports you
are actually using are not the first N — a dead port B, say — name them with
`--cameras A,C` (or `--cameras 1,3`). Everything downstream works on port
indices, so a gap is not a special case: the grid draws two tiles labelled A
and C, the status bar shows those two letters, and tapping cycles between
them.

The cameras sit upside down in the case, so images are rotated 180° by
default — the IMX219 does this in the sensor, at no cost. `--rotate 0`
turns it off if they are ever remounted.

## Controls

A round shutter button sits in the top-left corner of the screen; the rest
of it is the viewfinder. Holding the button fills a
clock-style ring — when the circle completes, the gallery opens with every
wigglegram on the device (newest first), each playing its loop. The same
button (now a back arrow) returns to the camera.

| Action                  | PiSugar button | Keyboard | Touch |
|-------------------------|----------------|----------|-------|
| Capture wigglegram      | single tap     | SPACE    | tap the button |
| Capture, from a terminal | — | `pkill -USR1 -f wigglecam.py` | — |
| Open gallery            | double tap     | G        | hold the button ~1 s |
| (gallery) older / newer | —              | → / ←    | swipe left / right |
| (gallery) slower/faster | —              | - / +    | tap left / right half |
| (gallery) back to camera| single tap     | G        | tap the button |
| Switch live camera      | —              | 1–4      | tap the image |
| Toggle live/grid view   | —              | V        | —     |
| Toggle preview fast/safe| —              | F        | —     |
| Quit                    | —              | ESC      | —     |

Photos and GIFs land in `~/piCameraPics` (the invoking user's home, not
root's) as `<timestamp>_cam<N>.jpg` + `<timestamp>_wigglegram.gif`.

pygame here draws to the framebuffer and reads `/dev/input` directly, so
neither the keyboard nor the on-screen button works over VNC — VNC delivers
input to the X session, which the app is not using. Hence the signal: from an
SSH shell, `sudo pkill -USR1 -f wigglecam.py` fires the shutter.

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

- **grid** (default): every camera gets a full-size tile and they round-robin,
  one mux switch per tile. With two cameras and the default timings that is
  several frames per second each — roughly what Arducam's own demo manages,
  and close enough to live to frame a shot with.
- **live** (`--view live` / V key): ONE camera streams full screen and the mux
  is never touched between frames, so it runs at whatever the pipe sustains.
  Tap the image or press 1–4 to move to the next healthy camera; if the live
  one dies, the view hops on its own.

Live view used to keep corner thumbnails of the other cameras, refreshed
round-robin every ~10 s. Each refresh was two mux switches and a visible
stall in the viewfinder, spent on a picture of a camera that physically
cannot be running at the same time as the live one — so they are gone. The
consequence is that a camera which quietly dies is no longer noticed within
30 s: it is discovered during the next capture, which costs one timeout and
worker respawn before that camera is marked offline and skipped. Once
offline it is retried in the background every 30 s as before. Use grid view
if you want to check every port on purpose.

### Preview strategies

- **fast** (default): the stream keeps running; the mux is switched live, two
  stale frames are flushed, the next is used. ~0.3 s per switched frame; in
  live view (no switching) it's whatever the pipe sustains, ~15–25 fps.
- **safe**: full stop → switch → configure → start → settle → capture → stop
  per frame (~0.6 s), the proven-reliable old method.

Three worker deaths while in fast mode auto-demote the session to safe.
`--preview-mode safe` or the F key force it.

The other brake on preview rate is the raw stream. Pinning it to the still
sensor mode makes the viewfinder's field of view match the photos, but forces
a full-resolution readout for every preview frame. That is **off by default**
now — previews run whatever fast, usually binned, mode libcamera picks, so the
viewfinder sees a little wider than the stills will. `--pin-raw` trades the
frame rate back for a truthful field of view.

### Capture bursts

The mux means the cameras are shot one after another, so a capture is
bracketed by `burst_begin` / `burst_end`:

- **Exposure and white balance are locked** to whatever the first camera's
  AE settled on, and forced on the rest. Without this each sensor meters
  for itself and the finished loop flickers in brightness and colour —
  the most visible artefact in a wigglegram, and not something the GIF
  encoder can fix afterwards. Every camera is the same model in the same
  sensor mode, so the values transfer exactly.
- **Focus is locked with it**, on a sensor that has a lens motor. Camera
  Module 3 autofocuses in the viewfinder and then holds whatever the first
  camera settled on for the rest of the burst — two cameras focusing
  independently give you two frames at different focus distances, which reads
  worse in a wigglegram than either being slightly soft. `--focus 3.3` fixes
  the lens outright (dioptres; 0 is infinity) if the autofocus hunts.
- **The AE settle disappears** for every camera after the first (0.4 s
  each), because there is no longer any AE to converge.
- In fast mode the burst also **keeps the stream running** and switches the
  mux underneath it, as fast previews do, instead of reconfiguring per
  camera. In safe mode it doesn't — safe mode already means live mux
  switching is unreliable on this rig — but the exposure lock still applies.

If a streaming burst kills the worker, the camera is retried once on the
respawned worker (which comes back without a burst, i.e. on the proven
sequence) before being blamed, and two such deaths disable streaming
bursts for the session. Each capture logs `burst took N.Ns` — compare that
number between modes rather than guessing.

### Hardware invariants (verified in the field — do not "simplify")

- Mux switch = GPIO **and** I2C, in that order, camera stopped (safe path):
  BCM 4/17/18 = select A / select B / OE, then write `0x04+n` to reg `0x00`
  at I2C `0x70`; OE must be driven HIGH before the first I2C contact.
- The settle delays are **not** hardware constants. They started at 100 ms
  (GPIO) and 200 ms (I2C) as conservative padding and were wrongly written up
  here as verified minimums; the switch is an analog CSI mux plus one register
  write and settles far faster. Defaults are now 5 ms and 25 ms, tunable with
  `--gpio-settle` / `--mux-settle`. If a tile shows the *previous* camera's
  picture, raise `--gpio-settle`; if libcamera reports frontend timeouts on
  wiring you trust, raise `--mux-settle`. Tune these only on a rig that is
  known good — a marginal connector produces the same frontend timeout as a
  too-short delay, and you cannot tell them apart.
- A wedged I2C bus (everything errno 110) survives reboots; the worker
  recovers it in software: ~10 SCL pulses, a STOP condition, then
  `raspi-gpio set 2/3 a0 pu` to restore ALT0.
- PiSugar (0x57): prefer the pisugar-server socket, which *pushes*
  `single`/`double`/`long`; without it, poll reg `0x3A` (bit 4 single,
  bit 5 double, clear by writing the value back with the bits masked) at
  **2 Hz maximum** — faster polling is suspected of wedging the bus.
- picamera2 pixel formats use libcamera's inverted names: `BGR888` is
  R,G,B in memory (what PIL/pygame want); `RGB888` is B,G,R.

## Start automatically at boot

```
sudo ./install-service.sh
```

It writes `/etc/systemd/system/wigglecam.service` using the repo's real
location and your username, enables it, starts it, and prints the status.
`Restart=always` means it comes back from crashes *and* from ESC — stop it
with `sudo systemctl stop wigglecam` before running a manual copy, or the
two fight over the camera.

The unit deliberately does **not** take a TTY. Claiming `/dev/tty1` puts
the service in a fight with `getty@tty1`, whose virtual hangup kills it
within milliseconds (`code=killed, signal=HUP`, restarting forever). SDL
reaches the framebuffer on its own without a console, which is also why
launching over SSH works.

If the journal shows the app reaching `ready — photos will be saved to …`
but the screen stays black, it is a display problem rather than a crash:
add `Environment=SDL_VIDEODRIVER=fbcon` and `Environment=SDL_FBDEV=/dev/fb0`
to the unit, or `Environment=DISPLAY=:0` instead if the Pi boots to the
desktop.

```
systemctl status wigglecam           # is it running?
journalctl -u wigglecam -f           # live console output
sudo systemctl stop wigglecam        # before a manual run
sudo systemctl disable wigglecam     # stop auto-starting
```

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
| `wigglecam.service`| systemd unit for starting at boot                   |

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
2. Set `/boot/config.txt` to match the sensor you fitted — `camera_auto_detect=0`
   plus `dtoverlay=imx708` for Camera Module 3, `dtoverlay=imx219` for a v2.
   Every camera on the mux must be the same model: one driver binds, once, at
   boot, through whichever port the mux powers up routing (port A).
3. `libcamera-hello --list-cameras` before running anything. One camera listed
   is correct — the mux only ever presents one. Nothing listed means the
   overlay and the hardware disagree; `dmesg | grep -i imx` will say
   `failed to read chip id ... error -5` when the driver asked and got silence.
4. `sudo python3 wigglecam.py`.
5. Check preview cadence in fast mode; if libcamera logs frontend timeouts,
   run with `--preview-mode safe`, and see the settle notes above before
   assuming the wiring is at fault.

The colours were wrong for a long time on this rig. That turned out to be
**NoIR camera modules** — no infrared filter, so daylight comes out washed and
magenta — and not the `BGR888`/`RGB888` question. If colours look off, check
which modules are fitted before touching `PIXEL_FORMAT`.
