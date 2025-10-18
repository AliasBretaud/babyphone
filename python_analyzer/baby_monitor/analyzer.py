from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Optional

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
    """Client WebRTC qui consomme la vidéo/audio et déclenche les analyses."""

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
        self._last_movement_ts = 0.0
        self._rejoin_lock = asyncio.Lock()
        self._stop_requested = False
        self._snapshot_interval = config.snapshot_interval
        self._snapshot_dir = Path(config.snapshot_dir)
        if self._snapshot_interval > 0:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_snapshot_ts = 0.0

    async def run(self) -> None:
        """Démarre la boucle de signalisation et reste actif jusqu'à fermeture."""
        ssl_context = None
        if self.config.signaling_url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if self.config.disable_ssl_verify:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        logging.info(
            "Connexion au serveur de signalisation %s (room=%s)",
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
                    logging.info("WebSocket fermé lors de l'arrêt: %s", exc)
                    break
                logging.warning("WebSocket fermé (%s), tentative de reconnexion...", exc)
                await asyncio.sleep(2)
                continue
            except Exception:
                if self._stop_requested:
                    break
                logging.exception("Erreur inattendue côté client, tentative de reconnexion")
                await asyncio.sleep(2)
                continue
            finally:
                self._ws = None
            if not self._stop_requested:
                logging.warning("Boucle principale terminée, reconnexion dans 2s...")
                await asyncio.sleep(2)

        logging.info("Boucle principale arrêtée")

    async def _setup_peer_connection(self) -> None:
        ice_servers = [RTCIceServer(urls=self.config.stun_servers)]
        self._pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        # Préparer des transceivers recvonly pour refléter le comportement du viewer web
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
                    "Candidate locale envoyée (mid=%s, index=%s)",
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
            logging.info("PeerConnection état: %s", state)
            if state == "failed":
                await self._attempt_rejoin("peer connection failed")

        @self._pc.on("track")
        def on_track(track) -> None:
            logging.info("Flux %s reçu", track.kind)
            if track.kind == "video":
                self._video_task = asyncio.create_task(self._consume_video(track))
            elif track.kind == "audio":
                self._audio_task = asyncio.create_task(self._consume_audio(track))

    async def _signaling_loop(self) -> None:
        assert self._ws is not None

        logging.info("Boucle de signalisation démarrée.")
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    logging.warning("Message JSON invalide: %s", raw)
                    continue

                msg_type = message.get("type")
                try:
                    if msg_type == "offer":
                        await self._handle_offer(message)
                    elif msg_type == "candidate":
                        await self._handle_remote_candidate(message)
                    elif msg_type == "peer-left":
                        logging.info("Broadcaster déconnecté (%s)", message.get("peerId"))
                        await self._reset()
                    elif msg_type == "viewer-joined":
                        logging.info(
                            "Notification viewer-joined reçue (id=%s)",
                            message.get("viewerId"),
                        )
                except Exception:
                    logging.exception("Erreur lors du traitement du message WS: %s", message)
        except websockets.exceptions.ConnectionClosed as exc:
            logging.warning("WebSocket interrompu (%s) – sortie de la boucle", exc)
            raise
        finally:
            logging.info("Boucle de signalisation terminée.")

    async def _handle_offer(self, message: dict) -> None:
        if self._pc is None:
            await self._setup_peer_connection()
        assert self._pc is not None and self._ws is not None
        offer = message.get("offer")
        if not offer:
            logging.warning("Offer manquant dans le message: %s", message)
            return

        logging.info("Offer reçu – création de la réponse locale.")
        rtc_offer = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        await self._pc.setRemoteDescription(rtc_offer)
        logging.info("Offer SDP reçu (%d octets)", len(rtc_offer.sdp or ""))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        logging.info(
            "Answer SDP générée (%d octets)",
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
        logging.info("Réponse envoyée au broadcaster (targetId=%s)", message.get("fromId"))

    async def _handle_remote_candidate(self, message: dict) -> None:
        assert self._pc is not None
        candidate_dict = message.get("candidate")
        if candidate_dict is None:
            await self._pc.addIceCandidate(None)
            return
        candidate_sdp = candidate_dict.get("candidate", "") or ""
        if not candidate_sdp.strip():
            logging.info(
                "Candidate distant vide ignoré (mid=%s, index=%s)",
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
                "Candidate distant ajouté (mid=%s, index=%s)",
                candidate_dict.get("sdpMid"),
                candidate_dict.get("sdpMLineIndex"),
            )
        except Exception:
            logging.exception("Échec addIceCandidate avec %s", candidate_dict)

    async def _consume_video(self, track) -> None:
        frame_count = 0
        first_frame_logged = False
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError as exc:  # pragma: no cover - dépend de aiortc
                logging.info("Flux vidéo terminé (%s)", exc)
                break
            except Exception:
                logging.exception("Erreur lors de la réception vidéo")
                break

            frame_count += 1
            if not first_frame_logged:
                logging.info(
                    "Flux vidéo: première frame reçue (pts=%s, time=%s)",
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
                logging.exception("Erreur analyse pose")
                continue
            if not observation:
                continue

            self._maybe_save_snapshot(frame_bgr, observation)
            self._handle_pose_observation(observation)

    async def _consume_audio(self, track) -> None:
        frame_count = 0
        first_frame_logged = False
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError as exc:  # pragma: no cover
                logging.info("Flux audio terminé (%s)", exc)
                break
            except Exception:
                logging.exception("Erreur lors de la réception audio")
                break

            frame_count += 1
            if not first_frame_logged:
                logging.info(
                    "Flux audio: premier paquet reçu (pts=%s, samples=%s)",
                    frame.pts,
                    frame.samples,
                )
                first_frame_logged = True

            try:
                event = self._audio_analyzer.process_frame(frame)
            except Exception:
                logging.exception("Erreur analyse audio")
                continue
            if event:
                logging.info(
                    "Détection pleurs bébé – énergie %.3f, ratio %.2f",
                    event.energy,
                    event.ratio_mid_band,
                )

    def _handle_pose_observation(self, observation) -> None:
        posture = observation.posture
        if posture in {"standing", "sitting"} and posture != self._current_posture:
            label = "debout" if posture == "standing" else "assis"
            logging.info(
                "Détection posture %s (angle=%.1f°)",
                label,
                observation.angle_deg,
            )
            self._current_posture = posture

        if (
            posture == "lying"
            and observation.movement_detected
            and (time.time() - self._last_movement_ts) > 2.0
        ):
            logging.info(
                "Détection mouvement (score=%.3f, angle=%.1f°)",
                observation.movement_score,
                observation.angle_deg,
            )
            self._last_movement_ts = time.time()
            self._current_posture = "lying"
        elif posture == "lying":
            if self._current_posture != "lying":
                logging.info(
                    "Posture détectée: bébé couché (angle=%.1f°)",
                    observation.angle_deg,
                )
            self._current_posture = "lying"

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
        logging.info("État du client réinitialisé.")

    async def _attempt_rejoin(self, reason: str) -> None:
        if self._ws is None or self._ws.closed:
            logging.info(
                "WebSocket fermé, la boucle principale gérera la reconnexion (%s)",
                reason,
            )
            return
        if self._rejoin_lock.locked():
            logging.info("Reconnexion déjà en cours, ignore (%s)", reason)
            return
        async with self._rejoin_lock:
            logging.warning("Perte de connexion (%s) – tentative de reconnexion", reason)
            await self._reset()
            try:
                await self._ws.send(
                    json.dumps(
                        {"type": "join", "room": self.config.room, "role": "viewer"}
                    )
                )
                await self._setup_peer_connection()
            except websockets.exceptions.ConnectionClosed:
                logging.warning("Impossible de réémettre join: WS fermé")

    async def close(self) -> None:
        self._stop_requested = True
        await self._reset()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._pose_analyzer.close()
        self._audio_analyzer.close()

    def _maybe_save_snapshot(self, frame_bgr, observation) -> None:
        if self._snapshot_interval <= 0:
            return
        if observation.pose_landmarks is None:
            return
        now = time.time()
        if now - self._last_snapshot_ts < self._snapshot_interval:
            return
        self._last_snapshot_ts = now
        annotated = self._pose_analyzer.annotate_frame(frame_bgr, observation.pose_landmarks)
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = self._snapshot_dir / f"snapshot_{timestamp}.jpg"
        try:
            import cv2

            cv2.imwrite(str(filename), annotated)
            logging.info("Capture annotée enregistrée: %s", filename)
        except Exception:
            logging.exception("Impossible d'enregistrer la capture annotée")
