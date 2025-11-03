from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import sys

import websockets
from websockets import WebSocketClientProtocol

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.rtcconfiguration import RTCIceServer
from aiortc.sdp import candidate_from_sdp

try:
    from websockets.protocol import State as WSState
except Exception:  # pragma: no cover - version-dependent import
    WSState = None

if sys.platform.startswith("linux"):
    DEFAULT_VIDEO_DEVICE = "/dev/video0"
    DEFAULT_VIDEO_FORMAT = "v4l2"
    DEFAULT_AUDIO_DEVICE = "default"
    DEFAULT_AUDIO_FORMAT = "alsa"
elif sys.platform == "darwin":
    DEFAULT_VIDEO_DEVICE = None  # e.g. "0:" or "default:none" for avfoundation
    DEFAULT_VIDEO_FORMAT = None  # set explicitly via CLI if needed
    DEFAULT_AUDIO_DEVICE = None  # e.g. ":0" for avfoundation
    DEFAULT_AUDIO_FORMAT = None
else:
    DEFAULT_VIDEO_DEVICE = None  # e.g. "video=Integrated Camera" for dshow
    DEFAULT_VIDEO_FORMAT = None
    DEFAULT_AUDIO_DEVICE = None  # e.g. "audio=Microphone (Realtek...)"
    DEFAULT_AUDIO_FORMAT = None


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _ws_is_closed(ws: Optional[WebSocketClientProtocol]) -> bool:
    if ws is None:
        return True
    closed_attr = getattr(ws, "closed", None)
    if isinstance(closed_attr, bool):
        return closed_attr
    if callable(closed_attr):
        try:
            result = closed_attr()
        except TypeError:
            result = None
        if isinstance(result, bool):
            return result
    state = getattr(ws, "state", None)
    if state is not None:
        if WSState is not None and isinstance(state, WSState):
            return state in (WSState.CLOSING, WSState.CLOSED)
        state_name = getattr(state, "name", None)
        if isinstance(state_name, str):
            return state_name.upper() in {"CLOSING", "CLOSED"}
        if isinstance(state, str):
            return state.upper() in {"CLOSING", "CLOSED"}
    return False


@dataclass
class BroadcasterConfig:
    signaling_url: str = "wss://localhost:3443/ws"
    room: str = "baby"
    stun_servers: List[str] = field(
        default_factory=lambda: ["stun:stun.l.google.com:19302"]
    )
    disable_ssl_verify: bool = True
    log_level: str = "INFO"

    video_enabled: bool = True
    video_device: Optional[str] = DEFAULT_VIDEO_DEVICE
    video_format: Optional[str] = DEFAULT_VIDEO_FORMAT
    video_resolution: str = "1280x720"
    video_fps: int = 30

    audio_enabled: bool = True
    audio_device: Optional[str] = DEFAULT_AUDIO_DEVICE
    audio_format: Optional[str] = DEFAULT_AUDIO_FORMAT
    audio_sample_rate: int = 48000
    audio_channels: int = 1

    @classmethod
    def from_env(cls) -> "BroadcasterConfig":
        cfg = cls()
        cfg.signaling_url = os.getenv("BROADCASTER_SIGNALING", cfg.signaling_url)
        cfg.room = os.getenv("BROADCASTER_ROOM", cfg.room)
        stun_raw = os.getenv("BROADCASTER_STUN", "")
        if stun_raw:
            cfg.stun_servers = [
                server.strip() for server in stun_raw.split(",") if server.strip()
            ]
        cfg.disable_ssl_verify = not _parse_bool(
            os.getenv("BROADCASTER_SSL_VERIFY"),
            default=not cfg.disable_ssl_verify,
        )
        cfg.log_level = os.getenv("LOG_LEVEL", cfg.log_level).upper()

        cfg.video_device = os.getenv("BROADCASTER_VIDEO_DEVICE", cfg.video_device)
        cfg.video_format = os.getenv("BROADCASTER_VIDEO_FORMAT", cfg.video_format)
        cfg.video_resolution = os.getenv(
            "BROADCASTER_VIDEO_RESOLUTION", cfg.video_resolution
        )
        cfg.video_fps = int(
            os.getenv("BROADCASTER_VIDEO_FPS", cfg.video_fps) or cfg.video_fps
        )
        cfg.video_enabled = _parse_bool(
            os.getenv("BROADCASTER_VIDEO_ENABLED"),
            default=cfg.video_enabled and bool(cfg.video_device),
        )

        cfg.audio_device = os.getenv("BROADCASTER_AUDIO_DEVICE", cfg.audio_device)
        cfg.audio_format = os.getenv("BROADCASTER_AUDIO_FORMAT", cfg.audio_format)
        cfg.audio_sample_rate = int(
            os.getenv("BROADCASTER_AUDIO_SAMPLE_RATE", cfg.audio_sample_rate)
            or cfg.audio_sample_rate
        )
        cfg.audio_channels = int(
            os.getenv("BROADCASTER_AUDIO_CHANNELS", cfg.audio_channels)
            or cfg.audio_channels
        )
        cfg.audio_enabled = _parse_bool(
            os.getenv("BROADCASTER_AUDIO_ENABLED"),
            default=cfg.audio_enabled and bool(cfg.audio_device),
        )
        return cfg

    @classmethod
    def from_args(
        cls, argv: Iterable[str] | None = None
    ) -> "BroadcasterConfig":
        env_cfg = cls.from_env()
        parser = argparse.ArgumentParser(
            description="Headless BabyPhone WebRTC broadcaster"
        )
        parser.add_argument(
            "--signaling",
            default=env_cfg.signaling_url,
            help="Signaling WebSocket URL (wss://...)",
        )
        parser.add_argument(
            "--room",
            default=env_cfg.room,
            help="Room to join (default: baby)",
        )
        parser.add_argument(
            "--stun",
            nargs="*",
            default=env_cfg.stun_servers,
            help="Space-separated list of STUN servers",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--ssl-verify",
            dest="disable_ssl_verify",
            action="store_false",
            help="Enable strict TLS verification",
        )
        group.add_argument(
            "--no-ssl-verify",
            dest="disable_ssl_verify",
            action="store_true",
            help="Disable TLS verification (for self-signed certs)",
        )
        parser.set_defaults(disable_ssl_verify=env_cfg.disable_ssl_verify)
        parser.add_argument(
            "--log-level",
            default=env_cfg.log_level,
            help="Logging level (DEBUG, INFO, WARNING, ...)",
        )

        parser.add_argument(
            "--video-device",
            default=env_cfg.video_device or "",
            help="Video capture device/input (FFmpeg syntax). Use 'none' to disable.",
        )
        parser.add_argument(
            "--video-format",
            default=env_cfg.video_format or "",
            help="FFmpeg input format for the video device (e.g., v4l2, avfoundation).",
        )
        parser.add_argument(
            "--video-resolution",
            default=env_cfg.video_resolution,
            help="Capture resolution WIDTHxHEIGHT (default: 1280x720).",
        )
        parser.add_argument(
            "--video-fps",
            type=int,
            default=env_cfg.video_fps,
            help="Capture frame rate (default: 30).",
        )
        video_toggle = parser.add_mutually_exclusive_group()
        video_toggle.add_argument(
            "--no-video",
            dest="video_enabled",
            action="store_false",
            help="Disable video capture.",
        )
        video_toggle.add_argument(
            "--with-video",
            dest="video_enabled",
            action="store_true",
            help="Enable video capture (default).",
        )
        parser.set_defaults(video_enabled=env_cfg.video_enabled)

        parser.add_argument(
            "--audio-device",
            default=env_cfg.audio_device or "",
            help="Audio capture device/input (FFmpeg syntax). Use 'none' to disable.",
        )
        parser.add_argument(
            "--audio-format",
            default=env_cfg.audio_format or "",
            help="FFmpeg input format for the audio device (e.g., alsa, avfoundation).",
        )
        parser.add_argument(
            "--audio-sample-rate",
            type=int,
            default=env_cfg.audio_sample_rate,
            help="Audio sample rate in Hz (default: 48000).",
        )
        parser.add_argument(
            "--audio-channels",
            type=int,
            default=env_cfg.audio_channels,
            help="Number of audio channels (default: 1).",
        )
        audio_toggle = parser.add_mutually_exclusive_group()
        audio_toggle.add_argument(
            "--no-audio",
            dest="audio_enabled",
            action="store_false",
            help="Disable audio capture.",
        )
        audio_toggle.add_argument(
            "--with-audio",
            dest="audio_enabled",
            action="store_true",
            help="Enable audio capture (default).",
        )
        parser.set_defaults(audio_enabled=env_cfg.audio_enabled)

        args = parser.parse_args(argv)

        video_device = args.video_device.strip()
        if not video_device or video_device.lower() == "none":
            args.video_enabled = False
            video_device = None

        audio_device = args.audio_device.strip()
        if not audio_device or audio_device.lower() == "none":
            args.audio_enabled = False
            audio_device = None

        return cls(
            signaling_url=args.signaling,
            room=args.room,
            stun_servers=list(args.stun),
            disable_ssl_verify=args.disable_ssl_verify,
            log_level=args.log_level.upper(),
            video_enabled=args.video_enabled,
            video_device=video_device,
            video_format=args.video_format.strip() or None,
            video_resolution=args.video_resolution,
            video_fps=args.video_fps,
            audio_enabled=args.audio_enabled,
            audio_device=audio_device,
            audio_format=args.audio_format.strip() or None,
            audio_sample_rate=args.audio_sample_rate,
            audio_channels=args.audio_channels,
        )


class HeadlessBroadcaster:
    """Headless broadcaster that captures local devices and serves viewers."""

    def __init__(self, config: BroadcasterConfig) -> None:
        self.config = config
        self._ws: Optional[WebSocketClientProtocol] = None
        self._peers: Dict[str, RTCPeerConnection] = {}
        self._video_player: Optional[MediaPlayer] = None
        self._audio_player: Optional[MediaPlayer] = None
        self._relay = MediaRelay()
        self._stop_requested = False
        self._reconnect_delay = 2.0

    async def run(self) -> None:
        await self._prepare_media()
        ssl_context = None
        if self.config.signaling_url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.config.disable_ssl_verify:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        while not self._stop_requested:
            try:
                async with websockets.connect(
                    self.config.signaling_url, ssl=ssl_context
                ) as ws:
                    self._ws = ws
                    logging.info(
                        "Connected to signaling server %s (room=%s)",
                        self.config.signaling_url,
                        self.config.room,
                    )
                    await self._send(
                        {"type": "join", "room": self.config.room, "role": "broadcaster"}
                    )
                    await self._signaling_loop(ws)
            except websockets.exceptions.ConnectionClosedError as exc:
                if self._stop_requested:
                    break
                logging.warning(
                    "WebSocket connection closed (%s), retrying in %.0fs",
                    exc,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
            except Exception:
                if self._stop_requested:
                    break
                logging.exception("Broadcaster error, retrying in %.0fs", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
            finally:
                self._ws = None
                await self._close_all_peers()

        await self._shutdown_media()

    async def close(self) -> None:
        self._stop_requested = True
        if self._ws:
            await self._ws.close()
        await self._close_all_peers()
        await self._shutdown_media()

    async def _signaling_loop(self, ws: WebSocketClientProtocol) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logging.warning("Ignoring non-JSON message: %s", raw)
                continue
            msg_type = message.get("type")
            if msg_type == "viewer-joined":
                viewer_id = message.get("viewerId")
                if viewer_id:
                    await self._handle_viewer_joined(viewer_id)
            elif msg_type == "answer":
                await self._handle_answer(message)
            elif msg_type == "candidate":
                await self._handle_remote_candidate(message)
            elif msg_type == "peer-left":
                await self._handle_peer_left(message)

    async def _handle_viewer_joined(self, viewer_id: str) -> None:
        await self._close_peer(viewer_id)
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=[RTCIceServer(urls=self.config.stun_servers)]
            )
        )
        self._peers[viewer_id] = pc

        if self.config.video_enabled and self._video_player and self._video_player.video:
            pc.addTrack(self._relay.subscribe(self._video_player.video))
        if self.config.audio_enabled and self._audio_player and self._audio_player.audio:
            pc.addTrack(self._relay.subscribe(self._audio_player.audio))

        @pc.on("icecandidate")
        async def on_icecandidate(event) -> None:
            if _ws_is_closed(self._ws):
                return
            candidate = event.candidate
            payload = {
                "type": "candidate",
                "targetId": viewer_id,
                "candidate": None,
            }
            if candidate:
                payload["candidate"] = {
                    "candidate": candidate.to_sdp(),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                }
            await self._send(payload)

        @pc.on("connectionstatechange")
        async def on_state_change() -> None:
            state = pc.connectionState
            logging.info("Viewer %s connection state: %s", viewer_id, state)
            if state in {"failed", "closed"}:
                await self._close_peer(viewer_id)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await self._send(
            {
                "type": "offer",
                "targetId": viewer_id,
                "offer": {
                    "type": pc.localDescription.type,
                    "sdp": pc.localDescription.sdp,
                },
            }
        )
        logging.info("Offer sent to viewer %s", viewer_id)

    async def _handle_answer(self, message: dict) -> None:
        viewer_id = message.get("fromId") or message.get("targetId")
        pc = self._peers.get(viewer_id or "")
        if not pc:
            logging.warning("Received answer for unknown viewer %s", viewer_id)
            return
        answer = message.get("answer")
        if not answer:
            logging.warning("Malformed answer from viewer %s: %s", viewer_id, message)
            return
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer.get("sdp"), type=answer.get("type"))
        )
        logging.info("Answer applied for viewer %s", viewer_id)

    async def _handle_remote_candidate(self, message: dict) -> None:
        viewer_id = message.get("fromId") or message.get("targetId")
        pc = self._peers.get(viewer_id or "")
        if not pc:
            return
        candidate_dict = message.get("candidate")
        if candidate_dict is None:
            await pc.addIceCandidate(None)
            return
        candidate_sdp = candidate_dict.get("candidate", "")
        if not candidate_sdp:
            return
        try:
            candidate = candidate_from_sdp(candidate_sdp)
            candidate.sdpMid = candidate_dict.get("sdpMid")
            candidate.sdpMLineIndex = candidate_dict.get("sdpMLineIndex")
            await pc.addIceCandidate(candidate)
        except Exception:
            logging.exception("Failed to add ICE candidate from %s", viewer_id)

    async def _handle_peer_left(self, message: dict) -> None:
        peer_id = message.get("peerId")
        if peer_id:
            await self._close_peer(peer_id)

    async def _send(self, payload: dict) -> None:
        if _ws_is_closed(self._ws):
            logging.debug("Dropping WS payload (socket closed): %s", payload)
            return
        await self._ws.send(json.dumps(payload))

    async def _prepare_media(self) -> None:
        if self.config.video_enabled and self.config.video_device:
            video_options = {}
            if self.config.video_resolution:
                video_options["video_size"] = self.config.video_resolution
            if self.config.video_fps:
                video_options["framerate"] = str(self.config.video_fps)
            logging.info(
                "Opening video device %s (format=%s, options=%s)",
                self.config.video_device,
                self.config.video_format,
                video_options,
            )
            self._video_player = MediaPlayer(
                self.config.video_device,
                format=self.config.video_format,
                options=video_options,
            )
        if self.config.audio_enabled and self.config.audio_device:
            audio_options = {}
            if self.config.audio_sample_rate:
                audio_options["sample_rate"] = str(self.config.audio_sample_rate)
            if self.config.audio_channels:
                audio_options["channels"] = str(self.config.audio_channels)
            logging.info(
                "Opening audio device %s (format=%s, options=%s)",
                self.config.audio_device,
                self.config.audio_format,
                audio_options,
            )
            try:
                self._audio_player = MediaPlayer(
                    self.config.audio_device,
                    format=self.config.audio_format,
                    options=audio_options,
                )
            except Exception:
                logging.error(
                    "Unable to open audio device %s. "
                    "Check `arecord -L` for valid ALSA names or launch with --no-audio.",
                    self.config.audio_device,
                    exc_info=True,
                )
                raise

    async def _shutdown_media(self) -> None:
        players = [self._video_player, self._audio_player]
        for player in players:
            if not player:
                continue
            try:
                closer = getattr(player, "stop", None) or getattr(player, "close", None)
                if callable(closer):
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:
                logging.warning("Failed to stop media player cleanly", exc_info=True)
        self._video_player = None
        self._audio_player = None

    async def _close_peer(self, viewer_id: str) -> None:
        pc = self._peers.pop(viewer_id, None)
        if not pc:
            return
        logging.info("Closing connection with viewer %s", viewer_id)
        await pc.close()

    async def _close_all_peers(self) -> None:
        if not self._peers:
            return
        peers = list(self._peers.keys())
        for viewer_id in peers:
            await self._close_peer(viewer_id)
