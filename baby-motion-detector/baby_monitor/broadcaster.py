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

import numpy as np
import websockets
from websockets import WebSocketClientProtocol

from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    RTCRtpSender,
)
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.rtcconfiguration import RTCIceServer
from aiortc.sdp import candidate_from_sdp
from av.audio.frame import AudioFrame

from scipy import signal

try:
    from websockets.protocol import State as WSState
except Exception:  # pragma: no cover - version-dependent import
    WSState = None

# Ensure ALSA configuration is reachable when bundled ffmpeg looks in /tmp/vendor.
if sys.platform.startswith("linux"):
    if "ALSA_CONFIG_PATH" not in os.environ:
        for candidate in (
            "/usr/share/alsa/alsa.conf",
            "/usr/local/share/alsa/alsa.conf",
            "/etc/asound.conf",
        ):
            if os.path.exists(candidate):
                os.environ["ALSA_CONFIG_PATH"] = candidate
                break

    if "ALSA_CONFIG_DIR" not in os.environ:
        for candidate in (
            "/usr/share/alsa",
            "/usr/local/share/alsa",
        ):
            if os.path.isdir(candidate):
                os.environ["ALSA_CONFIG_DIR"] = candidate
                break

    if "ALSA_PLUGIN_DIR" not in os.environ:
        for candidate in (
            "/usr/lib/arm-linux-gnueabihf/alsa-lib",
            "/usr/lib/aarch64-linux-gnu/alsa-lib",
            "/usr/lib64/alsa-lib",
            "/usr/lib/alsa-lib",
        ):
            if os.path.isdir(candidate):
                os.environ["ALSA_PLUGIN_DIR"] = candidate
                break

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
    video_max_bitrate: int = 3_000_000
    video_min_bitrate: Optional[int] = None
    video_preferred_codec: Optional[str] = None
    video_input_format: Optional[str] = None

    audio_enabled: bool = True
    audio_device: Optional[str] = DEFAULT_AUDIO_DEVICE
    audio_format: Optional[str] = DEFAULT_AUDIO_FORMAT
    audio_sample_rate: int = 48000
    audio_channels: int = 1
    audio_gain_db: float = 0.0
    audio_noise_gate_db: Optional[float] = None
    audio_highpass_hz: Optional[float] = None
    audio_lowpass_hz: Optional[float] = None
    audio_ffmpeg_denoise: bool = False
    audio_ffmpeg_denoise_floor: float = -28.0

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
        cfg.video_max_bitrate = int(
            os.getenv("BROADCASTER_VIDEO_MAX_BITRATE", cfg.video_max_bitrate)
            or cfg.video_max_bitrate
        )
        vmin = os.getenv("BROADCASTER_VIDEO_MIN_BITRATE", "")
        if vmin:
            try:
                cfg.video_min_bitrate = int(vmin)
            except ValueError:
                pass
        cfg.video_preferred_codec = os.getenv(
            "BROADCASTER_VIDEO_PREFERRED_CODEC", cfg.video_preferred_codec
        )
        cfg.video_input_format = os.getenv(
            "BROADCASTER_VIDEO_INPUT_FORMAT", cfg.video_input_format
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
        gain_env = os.getenv("BROADCASTER_AUDIO_GAIN_DB")
        if gain_env is not None and gain_env.strip():
            try:
                cfg.audio_gain_db = float(gain_env)
            except ValueError:
                pass
        gate_env = os.getenv("BROADCASTER_AUDIO_NOISE_GATE_DB")
        if gate_env is not None and gate_env.strip():
            if gate_env.strip().lower() in {"none", "off"}:
                cfg.audio_noise_gate_db = None
            else:
                try:
                    cfg.audio_noise_gate_db = float(gate_env)
                except ValueError:
                    pass
        hp_env = os.getenv("BROADCASTER_AUDIO_HIGHPASS_HZ")
        if hp_env is not None and hp_env.strip():
            try:
                hp_val = float(hp_env)
            except ValueError:
                hp_val = cfg.audio_highpass_hz
            else:
                cfg.audio_highpass_hz = hp_val if hp_val > 0 else None
        lp_env = os.getenv("BROADCASTER_AUDIO_LOWPASS_HZ")
        if lp_env is not None and lp_env.strip():
            try:
                lp_val = float(lp_env)
            except ValueError:
                lp_val = cfg.audio_lowpass_hz
            else:
                cfg.audio_lowpass_hz = lp_val if lp_val > 0 else None
        cfg.audio_ffmpeg_denoise = _parse_bool(
            os.getenv("BROADCASTER_AUDIO_DENOISE"),
            default=cfg.audio_ffmpeg_denoise,
        )
        denoise_floor = os.getenv("BROADCASTER_AUDIO_DENOISE_FLOOR")
        if denoise_floor is not None and denoise_floor.strip():
            try:
                cfg.audio_ffmpeg_denoise_floor = float(denoise_floor)
            except ValueError:
                pass
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
        parser.add_argument(
            "--video-max-bitrate",
            type=int,
            default=env_cfg.video_max_bitrate,
            help="Target max video bitrate in bps (default: 3000000).",
        )
        parser.add_argument(
            "--video-min-bitrate",
            type=int,
            default=env_cfg.video_min_bitrate,
            help="Optional minimum video bitrate in bps.",
        )
        parser.add_argument(
            "--video-preferred-codec",
            default=env_cfg.video_preferred_codec or "",
            help="Preferred video codec (e.g., H264, VP8).",
        )
        parser.add_argument(
            "--video-input-format",
            default=env_cfg.video_input_format or "",
            help="FFmpeg input pixel format (e.g., mjpeg, yuyv422) for v4l2 sources.",
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
        parser.add_argument(
            "--audio-gain-db",
            type=float,
            default=env_cfg.audio_gain_db,
            help="Apply additional gain to the audio stream in dB (default: 0).",
        )
        parser.add_argument(
            "--audio-noise-gate-db",
            dest="audio_noise_gate_db",
            type=float,
            default=env_cfg.audio_noise_gate_db,
            help="Noise gate threshold in dBFS (negative). Use --no-audio-noise-gate to disable.",
        )
        parser.add_argument(
            "--no-audio-noise-gate",
            dest="audio_noise_gate_db",
            action="store_const",
            const=None,
            help="Disable the audio noise gate entirely.",
        )
        parser.add_argument(
            "--audio-highpass",
            type=float,
            default=env_cfg.audio_highpass_hz or 0.0,
            help="High-pass filter cutoff in Hz (0 disables the filter).",
        )
        parser.add_argument(
            "--audio-lowpass",
            type=float,
            default=env_cfg.audio_lowpass_hz or 0.0,
            help="Low-pass filter cutoff in Hz (0 disables the filter).",
        )
        parser.add_argument(
            "--audio-denoise",
            dest="audio_ffmpeg_denoise",
            action="store_true",
            help="Enable FFmpeg frequency-domain denoising (afftdn).",
        )
        parser.add_argument(
            "--no-audio-denoise",
            dest="audio_ffmpeg_denoise",
            action="store_false",
            help="Disable FFmpeg denoising (default follows env).",
        )
        parser.set_defaults(audio_ffmpeg_denoise=env_cfg.audio_ffmpeg_denoise)
        parser.add_argument(
            "--audio-denoise-floor",
            type=float,
            default=env_cfg.audio_ffmpeg_denoise_floor,
            help="Noise floor for afftdn filter (dB, default -28).",
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
        audio_highpass = args.audio_highpass if args.audio_highpass and args.audio_highpass > 0 else None
        audio_lowpass = args.audio_lowpass if args.audio_lowpass and args.audio_lowpass > 0 else None

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
            video_max_bitrate=args.video_max_bitrate,
            video_min_bitrate=args.video_min_bitrate,
            video_preferred_codec=args.video_preferred_codec.strip() or None,
            video_input_format=args.video_input_format.strip() or None,
            audio_enabled=args.audio_enabled,
            audio_device=audio_device,
            audio_format=args.audio_format.strip() or None,
            audio_sample_rate=args.audio_sample_rate,
            audio_channels=args.audio_channels,
            audio_gain_db=args.audio_gain_db,
            audio_noise_gate_db=args.audio_noise_gate_db,
            audio_highpass_hz=audio_highpass,
            audio_lowpass_hz=audio_lowpass,
            audio_ffmpeg_denoise=args.audio_ffmpeg_denoise,
            audio_ffmpeg_denoise_floor=args.audio_denoise_floor,
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
            video_sender = pc.addTrack(self._relay.subscribe(self._video_player.video))
            self._configure_video_sender(video_sender, pc)
        if self.config.audio_enabled and self._audio_player and self._audio_player.audio:
            audio_track = self._relay.subscribe(self._audio_player.audio)
            if (
                self.config.audio_gain_db
                or self.config.audio_noise_gate_db is not None
                or (self.config.audio_highpass_hz and self.config.audio_highpass_hz > 0)
            ):
                audio_track = AudioEffectsTrack(
                    audio_track,
                    gain_db=self.config.audio_gain_db,
                    noise_gate_db=self.config.audio_noise_gate_db,
                    highpass_hz=self.config.audio_highpass_hz,
                    lowpass_hz=self.config.audio_lowpass_hz,
                )
            pc.addTrack(audio_track)

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
            if self.config.video_input_format:
                video_options["input_format"] = self.config.video_input_format
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
            filters_chain: List[str] = []
            if self.config.audio_ffmpeg_denoise:
                if self.config.audio_highpass_hz:
                    filters_chain.append(
                        f"highpass=f={float(self.config.audio_highpass_hz):.1f}"
                    )
                if self.config.audio_lowpass_hz:
                    filters_chain.append(
                        f"lowpass=f={float(self.config.audio_lowpass_hz):.1f}"
                    )
                filters_chain.append(
                    f"afftdn=nf={float(self.config.audio_ffmpeg_denoise_floor):.1f}"
                )
            if filters_chain:
                audio_options["audio_filters"] = ",".join(filters_chain)
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

    def _configure_video_sender(
        self, sender: RTCRtpSender, pc: RTCPeerConnection
    ) -> None:
        try:
            params = sender.getParameters()
        except Exception:
            logging.debug("Unable to read sender parameters for video bitrate tuning")
            return

        if not params.encodings:
            params.encodings = [{}]
        encoding = params.encodings[0]
        if self.config.video_max_bitrate:
            encoding["maxBitrate"] = int(self.config.video_max_bitrate)
        if self.config.video_min_bitrate:
            encoding["minBitrate"] = int(self.config.video_min_bitrate)
        if self.config.video_fps:
            encoding["maxFramerate"] = int(self.config.video_fps)

        try:
            sender.setParameters(params)
        except Exception:
            logging.debug("Failed setting video sender parameters", exc_info=True)

        if self.config.video_preferred_codec:
            try:
                codec_name = self.config.video_preferred_codec.lower()
                capabilities = RTCRtpSender.getCapabilities("video")
                preferred = [
                    codec
                    for codec in capabilities.codecs
                    if codec_name in codec.mimeType.lower()
                ]
                if preferred:
                    others = [
                        codec for codec in capabilities.codecs if codec not in preferred
                    ]
                    transceiver = next(
                        (t for t in pc.getTransceivers() if t.sender == sender),
                        None,
                    )
                    if transceiver is not None:
                        transceiver.setCodecPreferences(preferred + others)
            except Exception:
                logging.debug(
                    "Failed applying preferred video codec %s",
                    self.config.video_preferred_codec,
                    exc_info=True,
                )


class AudioEffectsTrack(AudioStreamTrack):
    """Applies gain and simple noise mitigation to an audio source."""

    def __init__(
        self,
        source: AudioStreamTrack,
        *,
        gain_db: float = 0.0,
        noise_gate_db: Optional[float] = None,
        highpass_hz: Optional[float] = None,
        lowpass_hz: Optional[float] = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._gain = 10 ** (gain_db / 20.0) if gain_db else 1.0
        self._noise_gate = (
            10 ** (noise_gate_db / 20.0) if noise_gate_db is not None else None
        )
        self._highpass_hz = highpass_hz if highpass_hz and highpass_hz > 0 else None
        self._lowpass_hz = lowpass_hz if lowpass_hz and lowpass_hz > 0 else None
        self._hp_b: Optional[np.ndarray] = None
        self._hp_a: Optional[np.ndarray] = None
        self._hp_state: Optional[List[np.ndarray]] = None
        self._lp_b: Optional[np.ndarray] = None
        self._lp_a: Optional[np.ndarray] = None
        self._lp_state: Optional[List[np.ndarray]] = None
        self._gate_level: float = 1.0
        self._gate_prev: float = 1.0
        self._gate_floor: float = 0.02
        self._gate_attack = 0.25
        self._gate_release = 0.08

    def _ensure_filters(self, sample_rate: int, channels: int) -> None:
        nyquist = sample_rate / 2.0
        if self._highpass_hz:
            cutoff_hp = min(self._highpass_hz, nyquist * 0.95)
            if (
                self._hp_b is None
                or self._hp_state is None
                or len(self._hp_state) != channels
            ):
                self._hp_b, self._hp_a = signal.butter(
                    2, cutoff_hp / nyquist, btype="highpass"
                )
                self._hp_state = [
                    signal.lfilter_zi(self._hp_b, self._hp_a).astype(np.float32)
                    for _ in range(channels)
                ]
        else:
            self._hp_b = self._hp_a = None
            self._hp_state = None

        if self._lowpass_hz:
            cutoff_lp = min(self._lowpass_hz, nyquist * 0.95)
            if (
                self._lp_b is None
                or self._lp_state is None
                or len(self._lp_state) != channels
            ):
                self._lp_b, self._lp_a = signal.butter(
                    3, cutoff_lp / nyquist, btype="lowpass"
                )
                self._lp_state = [
                    signal.lfilter_zi(self._lp_b, self._lp_a).astype(np.float32)
                    for _ in range(channels)
                ]
        else:
            self._lp_b = self._lp_a = None
            self._lp_state = None

    async def recv(self) -> AudioFrame:
        frame = await self._source.recv()
        samples = frame.to_ndarray()
        reshape = False
        if samples.ndim == 1:
            samples = samples.reshape(1, -1)
            reshape = True

        floats = samples.astype(np.float32) / 32768.0
        channels = floats.shape[0]

        self._ensure_filters(frame.sample_rate, channels)
        if self._hp_b is not None and self._hp_state is not None:
            for idx in range(channels):
                floats[idx], self._hp_state[idx] = signal.lfilter(
                    self._hp_b,
                    self._hp_a,
                    floats[idx],
                    zi=self._hp_state[idx],
                )

        if self._lp_b is not None and self._lp_state is not None:
            for idx in range(channels):
                floats[idx], self._lp_state[idx] = signal.lfilter(
                    self._lp_b,
                    self._lp_a,
                    floats[idx],
                    zi=self._lp_state[idx],
                )

        if self._noise_gate is not None:
            rms = np.sqrt(np.mean(floats**2, axis=-1))
            avg_rms = float(np.mean(rms))
            if avg_rms <= 1e-8:
                target = 0.0
            elif avg_rms >= self._noise_gate:
                target = 1.0
            else:
                target = max(self._gate_floor, (avg_rms / self._noise_gate) ** 0.5)
            coeff = self._gate_attack if target > self._gate_level else self._gate_release
            self._gate_level += coeff * (target - self._gate_level)
            envelope = np.linspace(
                self._gate_prev,
                self._gate_level,
                floats.shape[-1],
                dtype=np.float32,
            )
            floats *= envelope
            self._gate_prev = float(envelope[-1])

        if self._gain != 1.0:
            floats *= self._gain

        floats = np.tanh(floats)  # soft clip to avoid harsh saturation
        floats = np.clip(floats, -1.0, 1.0).astype(np.float32)
        int_samples = (floats * 32767.0).astype(np.int16)
        if reshape:
            int_samples = int_samples.reshape(-1)

        out_frame = AudioFrame.from_ndarray(int_samples, layout=frame.layout.name)
        out_frame.sample_rate = frame.sample_rate
        out_frame.time_base = frame.time_base
        out_frame.pts = frame.pts
        return out_frame

    async def stop(self) -> None:
        await super().stop()
        stopper = getattr(self._source, "stop", None)
        if callable(stopper):
            result = stopper()
            if asyncio.iscoroutine(result):
                await result
