# Build and run

> 🇻🇳 Bản tiếng Việt: [INSTALL.vi.md](INSTALL.vi.md)

A web ground station for the Skydroid C12 gimbal camera: two live video streams,
camera commands that are verified by reading them back, gimbal control, and
synchronised session recording.

**You do not need the camera to run this.** A UDP simulator and a synthetic video
source stand in for the hardware, so everything below works on a laptop with
nothing plugged in. That is also how the project was developed.

Verified on Ubuntu 22.04 with Python 3.10.12, from a clean `git clone`.

---

## 1. Prerequisites

| | |
|---|---|
| OS | Linux. Developed on Ubuntu 22.04; the Rubik Pi 3 (arm64) is the deployment target |
| Python | 3.10 or newer (only 3.10.12 has been tested) |
| System package | **`python3-opencv`** — supplies `cv2` and `numpy` |
| Also used | `git`, and `ip` / `ping` from `iproute2` / `iputils-ping` for the network diagnostics |

```bash
sudo apt update
sudo apt install -y git python3-venv python3-opencv
```

`python3-opencv` is the one that matters. `cv2` is **not** installed with pip
here — it comes from the distribution package, and the next step is built around
that fact.

## 2. Install

```bash
git clone <repository-url> WebAppControlC12
cd WebAppControlC12

python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

**`--system-site-packages` is mandatory, not a preference.** Without it the
virtual environment cannot see the system `cv2` and `numpy`, and the video layer
fails to import. See [Troubleshooting](#7-troubleshooting) for the exact error.

The six pip dependencies are small: FastAPI, uvicorn, pytest, pytest-asyncio,
httpx and websockets.

## 3. Verify the install

```bash
.venv/bin/python -m pytest
```

Expect **524 passing** in roughly 45 seconds. The suite runs the simulator over
real UDP sockets and a real uvicorn server, so a green run means the whole stack
works on your machine — not just that the code imports.

## 4. Run without hardware

Two terminals, because the simulator is a separate process.

```bash
# terminal 1 — the simulated camera
.venv/bin/python -m c12ctl.sim.c12_sim --port 15000

# terminal 2 — the app, pointed at the simulator
.venv/bin/python -m c12ctl.web.app \
    --host 127.0.0.1 --port 15000 --local-port 0 \
    --http-port 8000 --video synthetic
```

Open <http://localhost:8000>.

Three flags there are doing real work:

- `--video synthetic` — generates two streams matching the real C12 geometry
  (1280×720@30 and 384×288@25). The default is `--video live`, which expects a
  camera at an RTSP address.
- `--local-port 0` — let the OS pick the local UDP port. The default is 5000,
  which the simulator on the same machine may already hold.
- `--host 127.0.0.1 --port 15000` — talk to the simulator instead of the camera.

### Even quicker: no simulator at all

```bash
.venv/bin/python -m c12ctl.web.app --dry-run --video off
```

`--dry-run` logs the packets it would send instead of opening a socket. Useful
for looking at the interface and reading the command registry; nothing answers,
so the camera state cache stays empty by design.

### What you can try in the UI

- **Camera** — Snapshot, Record, Zoom, palette and resolution. Every write is
  read back before the result is shown, and reports `confirmed` / `mismatch` /
  `unverified`.
- **Gimbal control** — press ARM, then drag the stick or use WASD / arrow keys.
  `Space` and `Esc` are an emergency stop at any time.
- **Record session** — writes video, command traffic and attitude into
  `logs/sessions/<id>/` on one clock.
- **Preflight** and **Sweep reads** — the network and protocol diagnostics.

## 5. Run with a real C12

Read this section before plugging anything in.

### Wiring

- The camera needs **7.2–72 V through the JST-2P connector**. RJ45 carries no
  power.
- The Skydroid cable has 4 conductors (TX±/RX±), so the link negotiates at
  100 Mb/s. That is normal, not a fault.

### Network

The camera is at `192.168.144.108` and runs **no DHCP server**, so the host needs
a static address on that subnet:

```bash
sudo ip addr add 192.168.144.20/24 dev enp8s0   # use your own interface name
sudo ip link set enp8s0 up
```

### Bring-up, in increasing order of risk

```bash
# 1. network diagnostics — no camera commands are sent at all
.venv/bin/python -m c12ctl.diagnose --preflight-only

# 2. capability map — read-only commands only, completely safe
.venv/bin/python -m c12ctl.diagnose -m logs/CAPABILITIES.md

# 3. live video
.venv/bin/python -m c12ctl.web.app --video live

# 4. everything, with the gimbal speed kept low
.venv/bin/python -m c12ctl.web.app --video live --max-speed 10
```

`diagnose` runs preflight first and **stops** if the link layer is broken:
sweeping commands with the cable unplugged only produces a wall of meaningless
timeouts.

Start a session recording before step 2. First contact with real hardware is a
run you cannot repeat; if the camera does something unexpected, the recording
lets you replay what was sent immediately before it.

### Where the camera image appears

In the same two panels you saw with the synthetic source — at the top of the
page, under the button bar. The interface does not change; only the source of the
frames does. The **Video** and **Layout** buttons behave exactly as before.

```
C12 ──RTSP :554 stream=1──┐
     (1280×720 @30, H.265) │
                           ├→ cv2 decode → FrameBus → JPEG encode → <img src="/video/visible">
C12 ──RTSP :555 stream=2──┘   (one thread   (latest     (once for     <img src="/video/thermal">
     (384×288 @25, H.265)       per stream)   frame)      all clients)
```

The two streams arrive on **different RTSP ports** and run fully independently.
The 384×288 thermal image is upscaled ×2 server-side before it is sent.

The command is *simpler* than the simulator one, because `--video live` is
already the default:

```bash
.venv/bin/python -m c12ctl.web.app
```

It opens exactly these two URIs:

```
visible  rtsp://192.168.144.108:554/stream=1
thermal  rtsp://192.168.144.108:555/stream=2
```

Two easy mistakes:

- **Do not pass `--video synthetic`.** With the camera connected you would still
  be watching generated frames.
- **`--host` drives both paths** — it is the UDP command endpoint *and* the RTSP
  host. Pointing it at the simulator with `--host 127.0.0.1` means no real video,
  because nothing serves RTSP there. With real hardware, leave it at the default.

On the Rubik Pi 3, add `--decoder v4l2h265dec` for hardware decoding.

### Telling live frames from synthetic ones

The synthetic source is unmistakable: SMPTE colour bars, a clock and a sweeping
vertical bar. For certainty, `GET /api/video` reports each stream's `uri` —
`rtsp://…` rather than `synthetic:visible`.

The small caption on each panel is live measurement: input fps, output fps,
latency, encode time, KB per frame. Numbers moving means frames are arriving.

### If a video panel stays black

`frames=0` in `/api/video`, and `/video/<name>/snapshot.jpg` returning 503, both
mean nothing has arrived yet. Check in this order:

1. Press **Preflight** and read the `RTSP 554` and `RTSP 555` cards — they test
   the video ports directly.
2. **Has the camera finished booting?** The RTSP streams open only after boot,
   not the moment power is applied.
3. Is the host address on the `192.168.144.x` subnet?
4. Test outside this app: `ffplay rtsp://192.168.144.108:554/stream=1`. If that
   is black too, the problem is the camera or the network, not this app.

### Before arming the gimbal

- Keep the space around the gimbal clear — cables can get wound up.
- Keep `--max-speed` low for the first run. 10 °/s is the deliberate default; the
  hardware ceiling is 63.5 °/s.
- Keep a hand on the STOP button.

## 6. Useful flags

```bash
.venv/bin/python -m c12ctl.web.app --help
```

| Flag | Default | |
|---|---|---|
| `--host` / `--port` | `192.168.144.108` / `5000` | camera UDP endpoint |
| `--local-port` | `5000` | local UDP port; use `0` when 5000 is taken |
| `--http-port` | `8000` | web interface port |
| `--bind` | `127.0.0.1` | HTTP listen address — see the warning below |
| `--video` | `live` | `live`, `synthetic` or `off` |
| `--decoder` | — | `v4l2h265dec` on the Rubik Pi 3 for hardware decoding |
| `--max-speed` | `10.0` | gimbal speed ceiling in °/s |
| `--dry-run` | off | log packets instead of sending them |
| `--packet-log` | — | write every TX/RX packet to a JSONL file |
| `--record-fps` | `5` | recorded frames per second per stream |
| `--no-gimbal` / `--no-telemetry` / `--no-camera` / `--no-record` | on | switch subsystems off |

> ⚠️ **The app has no authentication.** The default `--bind 127.0.0.1` accepts
> connections from this machine only. Changing it to `--bind 0.0.0.0` — to reach
> the interface from a tablet in the field, for example — exposes gimbal control
> to everyone on that network. Only do it on a network you control.

Output goes under `logs/`: `logs/sessions/` for recordings,
`logs/findings.jsonl` for the capability map. The whole directory is gitignored.

## 7. Troubleshooting

### `ModuleNotFoundError: No module named 'cv2'`

The virtual environment was created without `--system-site-packages`. Recreate it:

```bash
rm -rf .venv
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

If it persists, `cv2` is not installed system-wide:
`sudo apt install python3-opencv`, then check with
`python3 -c "import cv2; print(cv2.__version__)"`.

### `Could not bind UDP port 5000: [Errno 98] Address already in use`

Port 5000 is frequently held by a companion app or another ground station. Find
it with `ss -lunp | grep 5000`, close it, or just run with `--local-port 0`.

### Preflight reports failures and `diagnose` stops

That is the intended behaviour when no camera is connected. The output names each
problem and the command that fixes it — most often the static IP from step 5.
To sweep anyway, add `--skip-preflight`.

### The video panel stays black with `--video live`

See [If a video panel stays black](#if-a-video-panel-stays-black) for the full
check order. Note that the missing GStreamer H.265 elements on this dev machine
are not the cause: `cv2` is built with FFMPEG and decodes RTSP H.265 directly.

### Tests fail to collect on a machine with ROS installed

`pytest.ini` already disables the ROS plugins by name (`launch_testing` depends
on `lark`, which is not in this venv). Do not remove that `addopts` block.

## 8. Rubik Pi 3 notes

The Rubik Pi 3 (Qualcomm QCS6490, arm64) is the deployment target. Same
instructions, with one addition — use the hardware decoder:

```bash
.venv/bin/python -m c12ctl.web.app --video live --decoder v4l2h265dec
```

Video performance has only been measured on x86 so far. Measuring it on the Pi is
the deciding factor for whether the WebRTC work in phase 6 is worth doing at all;
see [NEXT.md](NEXT.md).

---

## Where to read next

| | |
|---|---|
| [README.md](README.md) | what each phase does and why, with the measurements |
| [NEXT.md](NEXT.md) | current state and what to do next |
| [PLAN_WEBAPP_C12.md](PLAN_WEBAPP_C12.md) | protocol analysis, architecture, the 7-phase roadmap |
| [PHAN_TICH_SDK_C12.md](PHAN_TICH_SDK_C12.md) | RCSDK bytecode analysis — the authoritative protocol source |

Those documents are written in Vietnamese. The code, logs and interface are in
English.
