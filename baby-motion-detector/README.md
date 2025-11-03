# Baby Monitor Analyzer (Python)

This folder hosts two command-line companions for the BabyPhone project:

- a **viewer/analyzer** that subscribes to the stream and detects events (motion, cries, posture),
- a **headless broadcaster** able to publish video/audio from a Raspberry Pi or any Linux host with no GUI.

> ℹ️ When MediaPipe is not available (e.g., Python 3.12+), the analyzer automatically falls back to an OpenCV-only backend. Motion detection still works, but posture classification (lying/sitting/standing) is disabled.

---

## Features

- Stand-alone WebRTC viewer powered by `aiortc`.
- WebSocket signaling with optional TLS verification bypass for self-signed certificates.
- Pose understanding through MediaPipe Pose (lying vs sitting vs standing) when available.
- Cry detection powered by simple spectral heuristics.
- Wake-up detection by observing posture changes over time.
- Optional annotated snapshots for debugging (MediaPipe backend only).
- Structured logging for all detected events.
- Headless broadcaster CLI that captures local video/audio and serves regular BabyPhone viewers.

---

## Installation

1. Create a virtual environment (Python 3.10 or 3.11 recommended; 3.12+ works in fallback mode):

   ```bash
   cd baby-motion-detector
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

   > `mediapipe`, `aiortc`, and `opencv-python` download native wheels and auxiliary files during installation. Make sure you have a network connection the first time you install them. If you see `No matching distribution found for mediapipe`, double-check that your interpreter is Python 3.10/3.11 or stick to the fallback mode.

---

## Usage

### Analyzer (viewer)

```bash
python run_analyzer.py \
  --signaling wss://localhost:3443/ws \
  --room baby \
  --no-ssl-verify \
  --record-audio \
  --snapshots
```

Key arguments:

- `--signaling`: WebSocket URL of the Node server (default `wss://localhost:3443/ws`).
- `--room`: room to join (`baby` by default or the `?room=...` you set in the web UI).
- `--ssl-verify/--no-ssl-verify`: toggle strict TLS verification (keep disabled for self-signed certs).
- `--audio-dir`: output folder for recorded WAV files (default `baby-motion-detector/output/audio`, created on demand).
- `--record-audio / --no-record-audio`: enable/disable audio recording (off by default).
- `--snapshots / --no-snapshots`: enable annotated snapshots for every detected motion/wake event.
- `--snapshot-dir`: directory for snapshots (default `baby-motion-detector/output/snapshots`).

The script connects, waits for a broadcaster, consumes the media stream, and logs detected events (cry, movement, wake). When `--record-audio` is enabled, files like `baby-motion-detector/output/audio/baby_audio_<timestamp>.wav` are written. When `--snapshots` is enabled and MediaPipe is available, each event produces an annotated image in `snapshot-dir`. The fallback backend skips posture and snapshots but still reports motion.

> ℹ️ On first launch the MediaPipe Tasks model (`pose_landmarker_full.task`) is downloaded automatically into `baby-motion-detector/models/`. Provide your own model by setting the `POSE_MODEL_PATH` environment variable if needed.

### Headless broadcaster

```bash
python run_broadcaster.py \
  --signaling wss://localhost:3443/ws \
  --room baby \
  --no-ssl-verify \
  --video-device /dev/video0 \
  --video-format v4l2 \
  --audio-device default
```

Notable options:

- `--video-resolution 1920x1080` / `--video-fps 25` to change capture quality.
- `--video-format avfoundation --video-device "0:"` on macOS (or use `:0` for audio) when using FFmpeg's avfoundation backend.
- `--video-max-bitrate 4000000` (ou plus) pour augmenter la netteté, `--video-min-bitrate` pour garantir un plancher, `--video-preferred-codec H264` pour prioriser un codec spécifique (si disponible dans FFmpeg/PyAV), `--video-input-format mjpeg` pour forcer un flux MJPEG matériel via v4l2.
- `--audio-device hw:1,0` for USB mics exposed by ALSA.
- `--audio-format alsa` (Linux) or `--audio-format avfoundation` (macOS) to force a specific FFmpeg backend.
- `--audio-gain-db 10`, `--audio-noise-gate-db -50`, `--audio-highpass 120`, `--audio-lowpass 6000`, `--audio-denoise --audio-denoise-floor -32` pour réduire le souffle/ventilateur, et `--allow-remote-shutdown` pour autoriser l'arrêt distant via la page Viewer.
- `--no-video` / `--no-audio` to disable a track entirely.
- `BROADCASTER_*` environment variables mirror every CLI flag (documentation below).
- Sur Raspberry Pi, vérifie les périphériques avec `arecord -L` et passe un nom explicite (`plughw:CARD=Device,DEV=0`, `sysdefault:CARD=Device`, etc.). Le script positionne automatiquement `ALSA_CONFIG_PATH` vers `/usr/share/alsa/alsa.conf` si nécessaire pour éviter l’erreur `Cannot access file /tmp/vendor/share/alsa/alsa.conf`.

---

## Architecture

```
baby-motion-detector/
├── run_analyzer.py          # CLI entrypoint
├── run_broadcaster.py       # Headless broadcaster CLI
├── requirements.txt
└── baby_monitor/
    ├── __init__.py
    ├── analyzer.py          # WebRTC client + event loops
    ├── broadcaster.py       # Headless WebRTC broadcaster
    ├── audio.py             # Cry detection + optional recording
    ├── config.py            # CLI/env configuration loader
    ├── pose.py              # Pose analysis (MediaPipe or OpenCV fallback)
    └── protobuf_compat.py   # Protobuf helpers
```

### Video pipeline (MediaPipe backend)
1. Receive frames via `aiortc`.
2. Run MediaPipe Pose to obtain 3D landmarks.
3. Classify posture (lying/sitting/standing) using heuristic rules.
4. Detect movement by comparing landmark deltas.

### Video pipeline (fallback backend)
1. Receive frames via `aiortc`.
2. Apply frame differencing + smoothing to estimate motion level.
3. Emit motion events without posture labels.

### Audio pipeline
1. Convert PCM frames to mono 16-bit samples.
2. Optionally write WAV chunks continuously.
3. Apply sliding window spectral analysis to flag cry-like patterns.

---

## Environment Variables

### Analyzer (viewer)

Every analyzer flag has an environment counterpart:

| Variable                    | Description                                 | Default value                          |
|----------------------------|---------------------------------------------|----------------------------------------|
| `ANALYZER_SIGNALING`       | WebSocket signaling URL                      | `wss://localhost:3443/ws`              |
| `ANALYZER_ROOM`            | Room to join                                 | `baby`                                 |
| `ANALYZER_SSL_VERIFY`      | `true` / `false` to enable strict TLS        | `false`                                |
| `ANALYZER_AUDIO_DIR`       | Audio recording directory                    | `baby-motion-detector/output/audio`    |
| `ANALYZER_AUDIO_RECORD`    | `true` / `false` to persist WAV files        | `false`                                |
| `ANALYZER_SNAPSHOT_ON_EVENT` | `true` / `false` to capture annotated shots | `false`                                |
| `ANALYZER_SNAPSHOT_DIR`    | Snapshot output directory                    | `baby-motion-detector/output/snapshots`|
| `POSE_MODEL_PATH`          | Custom `.task` model path (optional)         | auto-download                          |

### Headless broadcaster

| Variable                         | Description                                      | Default value                 |
|----------------------------------|--------------------------------------------------|-------------------------------|
| `BROADCASTER_SIGNALING`          | WebSocket signaling URL                           | `wss://localhost:3443/ws`     |
| `BROADCASTER_ROOM`               | Room to join                                      | `baby`                        |
| `BROADCASTER_STUN`               | Comma-separated list of STUN servers             | `stun:stun.l.google.com:19302`|
| `BROADCASTER_SSL_VERIFY`         | `true`/`false` to enable strict TLS               | `false`                       |
| `BROADCASTER_VIDEO_DEVICE`       | FFmpeg input for the camera                       | Linux: `/dev/video0`, otherwise unset |
| `BROADCASTER_VIDEO_FORMAT`       | FFmpeg format for the camera                      | Linux: `v4l2`, otherwise unset        |
| `BROADCASTER_VIDEO_RESOLUTION`   | Capture resolution `WIDTHxHEIGHT`                 | `1280x720`                    |
| `BROADCASTER_VIDEO_FPS`          | Capture frame rate                                | `30`                          |
| `BROADCASTER_VIDEO_MAX_BITRATE`  | Maximum video bitrate (bps)                       | `3000000`                     |
| `BROADCASTER_VIDEO_MIN_BITRATE`  | Minimum video bitrate (bps)                       | unset                         |
| `BROADCASTER_VIDEO_PREFERRED_CODEC` | Preferred codec (`H264`, `VP8`, …)             | unset                         |
| `BROADCASTER_VIDEO_INPUT_FORMAT` | FFmpeg input pixel format (e.g., `mjpeg`)         | unset                         |
| `BROADCASTER_VIDEO_ENABLED`      | `true`/`false` to toggle video capture            | `true`                        |
| `BROADCASTER_AUDIO_DEVICE`       | FFmpeg input for the microphone                   | Linux: `default`, otherwise unset     |
| `BROADCASTER_AUDIO_FORMAT`       | FFmpeg format for the mic                         | Linux: `alsa`, otherwise unset        |
| `BROADCASTER_AUDIO_SAMPLE_RATE`  | Audio sample rate in Hz                           | `48000`                       |
| `BROADCASTER_AUDIO_CHANNELS`     | Number of audio channels                          | `1`                           |
| `BROADCASTER_AUDIO_GAIN_DB`      | Additional gain applied to audio (dB)             | `0`                           |
| `BROADCASTER_AUDIO_NOISE_GATE_DB`| Noise gate threshold (dBFS, negative)             | unset                         |
| `BROADCASTER_AUDIO_HIGHPASS_HZ`  | High-pass cutoff frequency (Hz)                   | unset                         |
| `BROADCASTER_AUDIO_LOWPASS_HZ`   | Low-pass cutoff frequency (Hz)                    | unset                         |
| `BROADCASTER_AUDIO_DENOISE`      | `true` / `false` to enable FFmpeg `afftdn` filter  | `false`                       |
| `BROADCASTER_AUDIO_DENOISE_FLOOR`| Noise floor parameter for `afftdn` (dB)           | `-28`                         |
| `BROADCASTER_ALLOW_SHUTDOWN`     | `true` / `false` to accept remote shutdown        | `false`                       |
| `BROADCASTER_AUDIO_ENABLED`      | `true`/`false` to toggle audio capture            | `true`                        |

---

## Limitations & Future Ideas

- Motion/pose heuristics may need adjustments depending on camera angle and lighting.
- For more accurate crying detection, plug in a dedicated ML model or cloud service.
- Consider adding notifications (email, push) when wake/cry events occur.
- Fallback backend currently provides motion-only insights; posture would require a different on-device model.

---

## Development Tips

- Set `LOG_LEVEL=DEBUG` to increase verbosity.
- Modules are loosely coupled, so you can unit-test audio and video paths separately.
- Respect the licensing terms of any external models, especially MediaPipe Tasks.
