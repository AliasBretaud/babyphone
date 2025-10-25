# Baby Monitor Analyzer (Python)

This Python client connects to the BabyPhone WebRTC server as a viewer, inspects the incoming audio/video tracks, and raises motion/cry events in real time with optional debug snapshots.

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

---

## Architecture

```
baby-motion-detector/
├── run_analyzer.py          # CLI entrypoint
├── requirements.txt
└── baby_monitor/
    ├── __init__.py
    ├── analyzer.py          # WebRTC client + event loops
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

Every CLI flag has an environment counterpart:

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
