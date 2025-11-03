# BabyPhone WebRTC (LAN, Docker, HTTPS)

A local-network baby monitor: the **broadcaster** (phone/PC in the baby’s room) sends **camera + mic**, and one or more **viewers** (tablet/phone/PC on the same Wi‑Fi) watch **full‑screen**.  
**One‑way**: viewers send **no** audio/video back.

- Home `/` → choose **Broadcaster** (`/broadcaster`) or **Viewer** (`/viewer`)
- Default room: `baby` (you can override via `?room=xxx`; it’s not exposed in the UI)

---

## 📦 Repository Layout

```
.
├── baby-motion-detector/   # Python analyzer client (see README inside)
└── node-server/            # Node.js HTTPS signaling server + web UI
```

The Python analyzer connects as a viewer to classify movements and detect cries, while the Node.js server hosts the WebRTC signaling and UI.

---

## ✨ Features

- One‑directional A/V (no return audio/video from viewers)
- Multiple viewers on the same LAN
- Full‑screen viewing
- Pure web UI (Chrome recommended)
- Headless CLI broadcaster for Raspberry Pi or any Linux machine (`python baby-motion-detector/run_broadcaster.py`)
- Dockerized + compose
- HTTPS out of the box (self‑signed, auto‑generated at runtime by Docker or locally)
- No cloud: signaling and media stay on your LAN (WebRTC P2P)

---

## 🧰 Stack & Technologies

- **Node.js + Express** – static pages + HTTP server
- **WebSocket** (`ws`) – signaling (`/ws`)
- **WebRTC** – `RTCPeerConnection` for P2P media
- **ICE/STUN** – `stun:stun.l.google.com:19302` (sufficient on LAN)
- **UI** – HTML/CSS (`index.html`, `broadcaster.html`, `viewer.html`)
- **Containers** – Docker & docker compose

**Broadcaster audio constraints** (no extra WebAudio DSP):

```js
audio: {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
  sampleRate: 48000
}
```

---

## 🏗️ Architecture

```
+-----------------+                   +-------------------------+
|  Broadcaster    |                   |   Viewer(s) (1..N)      |
|  (phone/PC)     |                   |   (tablet/phone/PC)     |
|                 |                   |                         |
|  getUserMedia() | -- A/V tracks --> |  RTCPeerConnection      |
|  RTCPeerConn    | <-- ICE cand. --> |  (recvonly audio/video) |
+--------^--------+                   +------------^------------+
         |                                         |
         |            WebSocket (signaling)        |
         +------------>  Express + ws  <-----------+
                        (node-server/server_https.js, /ws)
```

**Signaling (short):** viewer joins via WS → server notifies broadcasters → broadcaster sends **offer** → viewer replies **answer** → ICE exchange → P2P established. Media **does not** pass through the server.

---

## 📁 Project Structure

```
.
├── baby-motion-detector/
│   ├── README.md
│   ├── run_analyzer.py
│   └── baby_monitor/…       # Pose & audio analyzers (Python)
└── node-server/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── package.json
    ├── server.js            # HTTP entrypoint (WS + static assets)
    ├── server_https.js      # HTTPS redirector + WS signaling
    ├── public/              # index.html, broadcaster.html, viewer.html, client.js…
    └── bin/generate-certs.sh
```

---

## ⚙️ Config & URLs

- **Ports**
  - **3000 (HTTP)** → redirects to **3443 (HTTPS)**
  - **3443 (HTTPS)** → main service
- **Pages**
  - `https://<host>:3443/`
  - `https://<host>:3443/broadcaster`
  - `https://<host>:3443/viewer`
- **Room**
  - Default `baby`
  - Optional override: `?room=<name>` (e.g., `/viewer?room=nursery`)

---

## 🚀 Run Locally (without Docker)

You must have a key/cert **before** launching Node. Use **Method A** below to create `./certs/server.key` and `./certs/server.crt`.

### Use the provided script

Requires `openssl` available on your machine.

```bash
# From the repo root
cd node-server
CERT_DIR=./certs CERT_HOSTNAMES="localhost 127.0.0.1" bash bin/generate-certs.sh
```

- Add any LAN IP / hostname you want covered by the certificate:
  ```bash
  cd node-server
  CERT_DIR=./certs CERT_HOSTNAMES="localhost 127.0.0.1 192.168.1.23 baby.local" bash bin/generate-certs.sh
  ```

Then start the app:

```bash
# Still inside node-server/
npm install
node server_https.js
# http://localhost:3000 -> redirects to https://localhost:3443
```

---

## 🐳 Run with Docker (Automatic HTTPS)

**Goal:** `docker compose up -d` should just work with **HTTPS** (self‑signed).  
The browser will show a **warning** once (expected for self‑signed certs).

```bash
cd node-server
docker compose up -d
# HTTP  : http://<host>:3000  (redirects to HTTPS)
# HTTPS : https://<host>:3443/
#        https://<host>:3443/broadcaster
#        https://<host>:3443/viewer
```

### Customize SANs (IPs/hostnames) without editing files

Provide a space‑separated list via `CERT_HOSTNAMES` at runtime; the init service will generate a cert covering all SANs if none exist yet:

---

## 🎥 Raspberry Pi Headless Broadcast

Need to stream without a screen? Use the Python headless broadcaster. It captures a USB or CSI webcam + microphone via FFmpeg/libav and speaks WebRTC to the Node signaling server.

1. Install system dependencies on the Pi (once):

   ```bash
   sudo apt update
   sudo apt install ffmpeg v4l-utils libatlas3-base
   ```

   Make sure the camera is enabled (`sudo raspi-config`) and appears under `/dev/video*`. For USB microphones, run `arecord -l` to list ALSA devices.

2. Create a virtualenv and install Python dependencies:

   ```bash
   cd baby-motion-detector
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Launch the broadcaster (adjust devices as needed):

   ```bash
   python run_broadcaster.py \
     --signaling wss://<server>:3443/ws \
     --room baby \
     --no-ssl-verify \
     --video-device /dev/video0 \
     --video-format v4l2 \
     --audio-device default \
     --audio-format alsa
   ```

   Useful flags:

   - `--video-resolution 1920x1080` or `--video-fps 25` to tune capture quality.
   - `--audio-device hw:1,0` if ALSA exposes the mic with a numbered card/device.
   - `--no-video` or `--no-audio` to disable a track (e.g., audio-only monitor).
   - Environment variables (`BROADCASTER_*`) mirror every CLI flag for service files.
   - On macOS (`ffmpeg` + `avfoundation`), set e.g. `--video-device "0:" --video-format avfoundation` and `--audio-device ":0" --audio-format avfoundation`.
    - Sur Raspberry Pi, vérifie la logique ALSA avec `arecord -L`. Utilise par exemple `--audio-device plughw:CARD=Device,DEV=0` et assure-toi que `/usr/share/alsa/alsa.conf` est présent (le script définit `ALSA_CONFIG_PATH` automatiquement si besoin).
    - Pour un flux plus net et fluide, augmente la résolution (`--video-resolution 1920x1080`), la cadence (`--video-fps 30`), le débit (`--video-max-bitrate 4000000`) et, sur les webcams USB, exige un flux MJPEG matériel (`--video-input-format mjpeg`). Tu peux aussi forcer un codec (`--video-preferred-codec H264`) si ta pile FFmpeg dispose de l’encodeur approprié.
   - Améliore l'audio en jouant sur `--audio-gain-db 10`, `--audio-noise-gate-db -50`, `--audio-highpass 120`, `--audio-lowpass 6000`, et active le débruitage FFmpeg (`--audio-denoise --audio-denoise-floor -32`) pour atténuer le souffle/fan constant.
    - Exemple concret sur Raspberry Pi (bonne fluidité sans surcharger le CPU) :
      ```bash
      python run_broadcaster.py \
        --signaling wss://192.168.1.9:3443/ws \
        --room baby \
        --no-ssl-verify \
        --video-device /dev/video0 \
        --video-format v4l2 \
        --video-resolution 1280x720 \
        --video-fps 20 \
        --video-max-bitrate 2500000 \
        --video-preferred-codec VP8 \
        --audio-device plughw:CARD=Device,DEV=0 \
        --audio-format alsa \
        --audio-sample-rate 48000 \
        --audio-channels 1 \
        --audio-gain-db 10 \
        --audio-noise-gate-db -50 \
        --audio-highpass 120 \
        --audio-lowpass 6000 \
        --audio-denoise \
        --audio-denoise-floor -32 \
        --log-level INFO
      ```

Once running, the CLI auto-reconnects to the signaling server and starts sending offers whenever a viewer opens `https://<server>:3443/viewer`. No browser is required on the Raspberry Pi.

```bash
cd node-server
CERT_HOSTNAMES="localhost 127.0.0.1 192.168.1.23 baby.local" docker compose up -d --build
```

**How it works**

- An init‑like service runs `bin/generate-certs.sh` (OpenSSL).
- It writes `server.key` / `server.crt` into a **named volume** `certs:`.
- The main service depends on it, mounts the same volume, and serves HTTPS.

**Security**

- The private key lives only in the **volume** (not in the Git repo, not baked into the image).
- In advanced setups, you can mount the volume **read‑only** for the app once generated.

---

## 🧠 Python Analyzer (Optional)

The `baby-motion-detector/` directory hosts an async Python client that connects as a viewer, analyses pose and audio, and can trigger alerts or snapshots.  
Use Python 3.10 or 3.11 when creating the virtualenv (MediaPipe does not yet publish wheels for 3.12+).  
Refer to `baby-motion-detector/README.md` for installation and usage instructions.
