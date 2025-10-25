from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Optional
import uuid

import websockets
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.rtcconfiguration import RTCIceServer
from aiortc.sdp import candidate_from_sdp
from aiortc.mediastreams import MediaStreamError
from websockets import WebSocketClientProtocol

from .audio import AudioAnalyzer
from .config import AnalyzerConfig
from .pose import PoseAnalyzer


class AnalyzerClient:
    """WebRTC client that consumes the media stream and runs analytics."""

    def __init__(self, config: AnalyzerConfig) -> None:
        self.config = config
        self._pc: Optional[RTCPeerConnection] = None
        self._ws: Optional[WebSocketClientProtocol] = None
        self._video_task: Optional[asyncio.Task] = None
        self._audio_task: Optional[asyncio.Task] = None
        self._pose_analyzer = PoseAnalyzer()
        self._audio_analyzer = AudioAnalyzer(
            config.audio_output_dir, record_audio=config.record_audio
        )
        self._current_posture: Optional[str] = None
        self._rejoin_lock = asyncio.Lock()
        self._stop_requested = False
        self._snapshot_enabled = config.snapshot_on_event
        self._snapshot_dir = Path(config.snapshot_dir)
        if self._snapshot_enabled:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._event_cooldown = 2.0
        self._movement_cooldown = 2.0
        self._last_event_label: Optional[str] = None
        self._last_event_ts: float = 0.0
        self._last_movement_event_ts: float = 0.0
        self._wake_candidate_posture: Optional[str] = None
        self._wake_candidate_since: float = 0.0
        self._wake_min_duration = 3.0
        self._is_awake: bool = False

    async def run(self) -> None:
        """Start the signaling loop and keep running until shutdown is requested."""
        ssl_context = None
        if self.config.signaling_url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.config.disable_ssl_verify:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        logging.info(
            "Connecting to signaling server %s (room=%s)",
            self.config.signaling_url,
            self.config.room,
        )

        while not self._stop_requested:
            try:
                async with websockets.connect(
                    self.config.signaling_url, ssl=ssl_context
                ) as ws:
                    self._ws = ws
                    await ws.send(
                        json.dumps(
                            {"type": "join", "room": self.config.room, "role": "viewer"}
                        )
                    )
                    await self._setup_peer_connection()
                    await self._signaling_loop()
            except websockets.exceptions.ConnectionClosed as exc:
                if self._stop_requested:
                    logging.info("WebSocket closed while stopping: %s", exc)
                    break
                logging.warning("WebSocket closed (%s), retrying in 2s...", exc)
                await asyncio.sleep(2)
                continue
            except Exception:
                if self._stop_requested:
                    break
                logging.exception("Unexpected client error, retrying in 2s")
                await asyncio.sleep(2)
                continue
            finally:
                self._ws = None
            if not self._stop_requested:
                logging.warning("Main loop ended, reconnecting in 2s...")
                await asyncio.sleep(2)

        logging.info("Main loop stopped")

    async def _setup_peer_connection(self) -> None:
        ice_servers = [RTCIceServer(urls=self.config.stun_servers)]
        self._pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        # Prepare recvonly transceivers to mimic the web viewer behavior
        self._pc.addTransceiver("video", direction="recvonly")
        self._pc.addTransceiver("audio", direction="recvonly")

        @self._pc.on("icecandidate")
        async def on_icecandidate(event) -> None:
            candidate = event.candidate
            if candidate and self._ws:
                payload = {
                    "type": "candidate",
                    "candidate": {
                        "candidate": candidate.to_sdp(),
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    },
                }
                logging.info(
                    "Sent local candidate (mid=%s, index=%s)",
                    candidate.sdpMid,
                    candidate.sdpMLineIndex,
                )
                await self._ws.send(json.dumps(payload))

        @self._pc.on("iceconnectionstatechange")
        async def on_ice_state_change() -> None:
            if self._pc is None:
                return
            state = self._pc.iceConnectionState
            logging.info("ICE: %s", state)
            if state == "failed":
                await self._attempt_rejoin("ice connection failed")

        @self._pc.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            if self._pc is None:
                return
            state = self._pc.connectionState
            logging.info("PeerConnection state: %s", state)
            if state == "failed":
                await self._attempt_rejoin("peer connection failed")

        @self._pc.on("track")
        def on_track(track) -> None:
            logging.info("Track %s received", track.kind)
            if track.kind == "video":
                self._video_task = asyncio.create_task(self._consume_video(track))
            elif track.kind == "audio":
                self._audio_task = asyncio.create_task(self._consume_audio(track))

    async def _signaling_loop(self) -> None:
        assert self._ws is not None

        logging.info("Signaling loop started.")
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logging.warning("Invalid JSON message: %s", raw)
                    continue

                msg_type = message.get("type")
                try:
                    if msg_type == "offer":
                        await self._handle_offer(message)
                    elif msg_type == "candidate":
                        await self._handle_remote_candidate(message)
                    elif msg_type == "peer-left":
                        logging.info("Broadcaster disconnected (%s)", message.get("peerId"))
                        await self._reset()
                    elif msg_type == "viewer-joined":
                        logging.info(
                            "Viewer-joined notification received (id=%s)",
                            message.get("viewerId"),
                        )
                except Exception:
                    logging.exception("Error while handling WS message: %s", message)
        except websockets.exceptions.ConnectionClosed as exc:
            logging.warning("WebSocket interrupted (%s) – leaving loop", exc)
            raise
        finally:
            logging.info("Signaling loop finished.")

    async def _handle_offer(self, message: dict) -> None:
        if self._pc is None:
            await self._setup_peer_connection()
        assert self._pc is not None and self._ws is not None
        offer = message.get("offer")
        if not offer:
            logging.warning("Offer missing from message: %s", message)
            return

        logging.info("Offer received – creating local answer.")
        rtc_offer = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await self._pc.setRemoteDescription(rtc_offer)
        logging.info("Offer SDP received (%d bytes)", len(rtc_offer.sdp or ""))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        logging.info(
            "Answer SDP generated (%d bytes)",
            len(self._pc.localDescription.sdp or ""),
        )
        payload = {
            "type": "answer",
            "answer": {
                "type": self._pc.localDescription.type,
                "sdp": self._pc.localDescription.sdp,
            },
            "targetId": message.get("fromId"),
        }
        await self._ws.send(json.dumps(payload))
        logging.info("Answer sent to broadcaster (targetId=%s)", message.get("fromId"))

    async def _handle_remote_candidate(self, message: dict) -> None:
        assert self._pc is not None
        candidate_dict = message.get("candidate")
        if candidate_dict is None:
            await self._pc.addIceCandidate(None)
            return
        candidate_sdp = candidate_dict.get("candidate", "") or ""
        if not candidate_sdp.strip():
            logging.info(
                "Ignoring empty remote candidate (mid=%s, index=%s)",
                candidate_dict.get("sdpMid"),
                candidate_dict.get("sdpMLineIndex"),
            )
            return

        try:
            candidate = candidate_from_sdp(candidate_sdp)
            candidate.sdpMid = candidate_dict.get("sdpMid")
            candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
            await self._pc.addIceCandidate(candidate)
            logging.info(
                "Added remote candidate (mid=%s, index=%s)",
                candidate_dict.get("sdpMid"),
                candidate_dict.get("sdpMLineIndex"),
            )
        except Exception:
            logging.exception("Failed to add remote candidate %s", candidate_dict)

    async def _consume_video(self, track) -> None:
        frame_count = 0
        first_frame_logged = False
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError as exc:  # pragma: no cover - depends on aiortc
                logging.info("Video track ended (%s)", exc)
                break
            except Exception:
                logging.exception("Error while receiving video")
                break

            frame_count += 1
            if not first_frame_logged:
                logging.info(
                    "Video track: first frame received (pts=%s, time=%s)",
                    frame.pts,
                    frame.time,
                )
                first_frame_logged = True

            frame_bgr = frame.to_ndarray(format="bgr24")
            try:
                observation = await asyncio.get_running_loop().run_in_executor(
                    None, self._pose_analyzer.process_frame, frame_bgr
                )
            except Exception:
                logging.exception("Pose analysis error")
                continue
            if not observation:
                continue

            events = self._handle_pose_observation(observation)
            if self._snapshot_enabled and events:
                for event in events:
                    self._save_snapshot(frame_bgr, observation, event)

    async def _consume_audio(self, track) -> None:
        frame_count = 0
        first_frame_logged = False
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError as exc:  # pragma: no cover
                logging.info("Audio track ended (%s)", exc)
                break
            except Exception:
                logging.exception("Error while receiving audio")
                break

            frame_count += 1
            if not first_frame_logged:
                logging.info(
                    "Audio track: first packet received (pts=%s, samples=%s)",
                    frame.pts,
                    frame.samples,
                )
                first_frame_logged = True

            try:
                event = self._audio_analyzer.process_frame(frame)
            except Exception:
                logging.exception("Audio analysis error")
                continue
            if event:
                logging.info(
                    "Cry detected – energy %.3f, ratio %.2f",
                    event.energy,
                    event.ratio_mid_band,
                )

    def _handle_pose_observation(self, observation) -> list[dict]:
        events: list[dict] = []
        posture = observation.posture
        now = time.time()
        extras = observation.extras or {}
        torso_angle = extras.get("torso_angle")
        avg_knee = extras.get("avg_knee_angle")
        leg_ext = extras.get("leg_extension")
        torso_text = (
            f"torso={torso_angle:.1f}°" if torso_angle is not None else "torso=n/a"
        )
        knee_text = f"{avg_knee:.1f}°" if avg_knee is not None else "n/a"
        leg_text = f"{leg_ext:.2f}" if leg_ext is not None else "n/a"

        # Movement detection with cooldown
        if observation.movement_detected and (
            now - self._last_movement_event_ts
        ) >= self._movement_cooldown:
            trace_id = uuid.uuid4().hex[:8]
            logging.info(
                "Trace %s – Movement detected (score=%.3f, posture=%s, %s, knee=%s, leg=%s)",
                trace_id,
                observation.movement_score,
                posture,
                torso_text,
                knee_text,
                leg_text,
            )
            payload = {
                "trace_id": trace_id,
                "label": "movement",
                "description": "movement",
                "extras": {
                    **extras,
                    "movement_score": observation.movement_score,
                    "posture": posture,
                },
            }
            if self._register_event(payload, now):
                events.append(payload)
                self._last_movement_event_ts = now

        # Wake detection (sitting or standing maintained for >= 3s)
        if posture in {"sitting", "standing"}:
            if self._is_awake:
                self._wake_candidate_posture = None
            else:
                if self._wake_candidate_posture != posture:
                    self._wake_candidate_posture = posture
                    self._wake_candidate_since = now
                elif (now - self._wake_candidate_since) >= self._wake_min_duration:
                    trace_id = uuid.uuid4().hex[:8]
                    duration = now - self._wake_candidate_since
                    logging.info(
                        "Trace %s – Wake detected (posture=%s, duration=%.1fs, %s, knee=%s, leg=%s)",
                        trace_id,
                        posture,
                        duration,
                        torso_text,
                        knee_text,
                        leg_text,
                    )
                    payload = {
                        "trace_id": trace_id,
                        "label": "wake",
                        "description": f"wake ({posture})",
                        "extras": {
                            **extras,
                            "posture": posture,
                            "duration": duration,
                        },
                    }
                    if self._register_event(payload, now):
                        events.append(payload)
                        self._is_awake = True
                        self._wake_candidate_posture = None
        else:
            self._wake_candidate_posture = None
            self._wake_candidate_since = 0.0
            self._is_awake = False

        self._current_posture = posture
        return events

    async def _reset(self) -> None:
        if self._video_task:
            self._video_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._video_task
            self._video_task = None
        if self._audio_task:
            self._audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._audio_task
            self._audio_task = None
        if self._pc:
            await self._pc.close()
            self._pc = None
        self._current_posture = None
        self._audio_analyzer.close()
        logging.info("Client state reset.")

    async def _attempt_rejoin(self, reason: str) -> None:
        if self._ws is None or self._ws.closed:
            logging.info(
                "WebSocket closed, main loop will handle reconnection (%s)",
                reason,
            )
            return
        if self._rejoin_lock.locked():
            logging.info("Reconnection already in progress, ignoring (%s)", reason)
            return
        async with self._rejoin_lock:
            logging.warning("Connection lost (%s) – attempting to rejoin", reason)
            await self._reset()
            try:
                await self._ws.send(
                    json.dumps(
                        {"type": "join", "room": self.config.room, "role": "viewer"}
                    )
                )
                await self._setup_peer_connection()
            except websockets.exceptions.ConnectionClosed:
                logging.warning("Cannot re-send join: WS already closed")

    async def close(self) -> None:
        self._stop_requested = True
        await self._reset()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._pose_analyzer.close()
        self._audio_analyzer.close()

    def _save_snapshot(self, frame_bgr, observation, event: dict) -> None:
        if observation.pose_landmarks is None:
            return
        annotated = self._pose_analyzer.annotate_frame(frame_bgr, observation.pose_landmarks)
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        label = event.get("label", "event").replace(" ", "_")
        trace_id = event.get("trace_id", "trace")
        filename = self._snapshot_dir / f"snapshot_{timestamp}_{label}_{trace_id}.jpg"
        try:
            import cv2

            cv2.imwrite(str(filename), annotated)
            desc = event.get("description") or event.get("label")
            logging.info(
                "Trace %s – Annotated snapshot (%s) saved: %s",
                trace_id,
                desc,
                filename,
            )
        except Exception:
            logging.exception(
                "Trace %s – Failed to save annotated snapshot", trace_id
            )

    def _register_event(self, event: dict, timestamp: float) -> bool:
        label = event.get("label")
        if not label:
            return False
        if (
            self._last_event_label == label
            and (timestamp - self._last_event_ts) < self._event_cooldown
        ):
            return False
        self._last_event_label = label
        self._last_event_ts = timestamp
        event.setdefault("extras", {})["event_timestamp"] = timestamp
        return True
