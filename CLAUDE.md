# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BabyPhone is a local-network baby monitor with WebRTC-based video/audio streaming and AI-powered analysis:
- **Node.js server** (`node-server/`): HTTPS WebRTC signaling server with web UI
- **Python analyzer** (`baby-motion-detector/`): Video/audio analysis for pose estimation and cry detection
- One-way streaming: broadcasters → multiple viewers via P2P WebRTC
- Docker deployment with auto-generated self-signed certificates

## Common Commands

### Node.js Server
```bash
cd node-server

# Install dependencies
npm install

# Run HTTP server (dev)
node server.js

# Run HTTPS server (requires certs in ./certs/)
node server_https.js

# Generate certificates for local development
CERT_DIR=./certs CERT_HOSTNAMES="localhost 127.0.0.1" bash bin/generate-certs.sh

# Docker deployment
docker compose up -d
```

### Python Analyzer
```bash
cd baby-motion-detector

# Setup virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run analyzer (viewer with AI analysis)
python run_analyzer.py --signaling wss://localhost:3443/ws --room baby --no-ssl-verify

# Run headless broadcaster (Raspberry Pi)
python run_broadcaster.py --signaling wss://localhost:3443/ws --room baby --no-ssl-verify \\
  --video-device /dev/video0 --video-format v4l2 --audio-device default
```

## Architecture

### WebRTC Signaling Flow (Node.js)
**Server entry points:**
- `server.js` - HTTP server on port 3000
- `server_https.js` - HTTPS server on port 3443 with certificate loading

**Signaling protocol:**
1. WebSocket connection to `/ws`
2. `join` message with `role` (broadcaster|viewer) and `room`
3. WebRTC offer/answer exchange via `offer`/`answer` messages
4. ICE candidates via `candidate` messages
5. Room-scoped message routing based on `room` field

**Room state management:**
- In-memory Map: `room → {broadcasters: Set, viewers: Set}`
- Message routing: broadcasters ↔ viewers within same room
- Event-log messages: broadcast to all peers in room (excluding sender)

### Python Analyzer Pipeline
**Entry points:**
- `run_analyzer.py` - WebRTC viewer + analyzer
- `run_broadcaster.py` - Headless broadcaster for Raspberry Pi

**Video analysis (dual backend):**
- MediaPipe backend: Pose landmarks → motion magnitude → sustained movement detection
- OpenCV fallback: Frame differencing → motion detection (no snapshots/landmarks)
- Snapshots saved to `output/snapshots/` when `--snapshots` enabled
- "Awake" events triggered by 5-second sustained movement window

**Audio pipeline:**
- PCM → mono conversion
- Optional WAV recording to `output/audio/`
- Spectral analysis for cry detection

**Configuration:**
- CLI args and environment variables (`ANALYZER_*`, `BROADCASTER_*`)
- See `baby-motion-detector/README.md` for full options

### Frontend Structure
- `public/index.html` - Landing page with role selection
- `public/broadcaster.html` - Broadcast page (getUserMedia)
- `public/viewer.html` - Viewer page (receive only)
- `public/client.js` - Shared WebRTC/signaling logic
- `public/styles.css` - UI styles

### Docker Deployment
- Init service (`certgen`) generates self-signed certificates on first run
- Certificates stored in named volume `certs:`
- Main service waits for certgen via `depends_on`
- Override SANs: `CERT_HOSTNAMES="localhost 192.168.1.23" docker compose up`
- Default ports: 3000 (HTTP→HTTPS redirect), 3443 (HTTPS)

## Key Implementation Details

### Certificate Management
- Certificates generated via `bin/generate-certs.sh` using OpenSSL
- Expected location: `./certs/server.key` and `./certs/server.crt`
- Must include all hostnames/IPs in SANs for browser trust
- Docker: auto-generated at runtime, persisted in volume

### Python Dependencies
- MediaPipe: Pose estimation (Python 3.10/3.11 only, falls back to OpenCV on 3.12+)
- aiortc: WebRTC client implementation
- OpenCV: Fallback motion detection and video processing
- FFmpeg/libav: Required by aiortc for media handling

### WebRTC Constraints
- Audio: mono, 48kHz sample rate, noise suppression enabled
- Video: Varies by device, configurable bitrate/resolution
- ICE: Uses Google STUN server by default (sufficient for LAN)
- One-way only: viewers have `recvonly` transceivers

### Remote Shutdown Feature
- Python broadcaster can accept shutdown commands via WebSocket
- Requires `--allow-remote-shutdown` flag
- Execute `sudo /sbin/shutdown -h now` (configure passwordless sudo)

## Testing

### Manual testing checklist
1. Generate/sign certificates for target hostnames
2. Start Node server (HTTP or HTTPS)
3. Open broadcaster page in browser, allow camera/mic
4. Open viewer page in browser
5. Verify WebRTC connection establishment via WebSocket logs
6. Check video/audio streams in viewer
7. (Optional) Start Python analyzer to verify motion/audio detection

### Debugging
- Node server logs WebSocket messages to console
- Python analyzer: set `LOG_LEVEL=DEBUG` for verbose output
- Browser dev tools: inspect WebRTC connection at `chrome://webrtc-internals`
- Certificate errors: verify SANs match accessed hostname
