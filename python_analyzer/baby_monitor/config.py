from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from typing import Iterable, List


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AnalyzerConfig:
    """Runtime configuration for the analyzer client."""

    signaling_url: str = "wss://localhost:3443/ws"
    room: str = "baby"
    stun_servers: List[str] = field(
        default_factory=lambda: ["stun:stun.l.google.com:19302"]
    )
    disable_ssl_verify: bool = True
    audio_output_dir: str = "python_analyzer/output"
    record_audio: bool = False
    log_level: str = "INFO"
    snapshot_interval: float = 0.0  # seconds, 0=disabled
    snapshot_dir: str = "python_analyzer/output/snapshots"

    @classmethod
    def from_env(cls) -> "AnalyzerConfig":
        cfg = cls()
        cfg.signaling_url = os.getenv("ANALYZER_SIGNALING", cfg.signaling_url)
        cfg.room = os.getenv("ANALYZER_ROOM", cfg.room)
        stun_raw = os.getenv("ANALYZER_STUN", "")
        if stun_raw:
            cfg.stun_servers = [s.strip() for s in stun_raw.split(",") if s.strip()]
        cfg.disable_ssl_verify = not _parse_bool(
            os.getenv("ANALYZER_SSL_VERIFY"), default=not cfg.disable_ssl_verify
        )
        cfg.audio_output_dir = os.getenv(
            "ANALYZER_AUDIO_DIR", cfg.audio_output_dir
        )
        cfg.record_audio = _parse_bool(
            os.getenv("ANALYZER_AUDIO_RECORD"), default=cfg.record_audio
        )
        cfg.log_level = os.getenv("LOG_LEVEL", cfg.log_level).upper()
        cfg.snapshot_interval = float(
            os.getenv("ANALYZER_SNAPSHOT_INTERVAL", cfg.snapshot_interval)
        )
        cfg.snapshot_dir = os.getenv("ANALYZER_SNAPSHOT_DIR", cfg.snapshot_dir)
        return cfg

    @classmethod
    def from_args(cls, argv: Iterable[str] | None = None) -> "AnalyzerConfig":
        env_cfg = cls.from_env()
        parser = argparse.ArgumentParser(
            description="BabyPhone WebRTC stream analyzer"
        )
        parser.add_argument(
            "--signaling",
            default=env_cfg.signaling_url,
            help="URL WebSocket de signalisation (wss://...)",
        )
        parser.add_argument(
            "--room",
            default=env_cfg.room,
            help="Nom de la salle à rejoindre (défaut: baby)",
        )
        parser.add_argument(
            "--stun",
            nargs="*",
            default=env_cfg.stun_servers,
            help="Liste des serveurs STUN (séparés par des espaces)",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--ssl-verify",
            dest="disable_ssl_verify",
            action="store_false",
            help="Activer la vérification TLS stricte",
        )
        group.add_argument(
            "--no-ssl-verify",
            dest="disable_ssl_verify",
            action="store_true",
            help="Désactiver la vérification TLS (certificats auto-signés)",
        )
        parser.set_defaults(disable_ssl_verify=env_cfg.disable_ssl_verify)
        parser.add_argument(
            "--audio-dir",
            default=env_cfg.audio_output_dir,
            help="Dossier d'enregistrement audio",
        )
        audio_group = parser.add_mutually_exclusive_group()
        audio_group.add_argument(
            "--record-audio",
            dest="record_audio",
            action="store_true",
            help="Enregistrer le flux audio sur disque (WAV)",
        )
        audio_group.add_argument(
            "--no-record-audio",
            dest="record_audio",
            action="store_false",
            help="Ne pas enregistrer l'audio sur disque (par défaut)",
        )
        parser.set_defaults(record_audio=env_cfg.record_audio)
        parser.add_argument(
            "--log-level",
            default=env_cfg.log_level,
            help="Niveau de log (DEBUG, INFO, WARNING...)",
        )
        parser.add_argument(
            "--snapshot-interval",
            type=float,
            default=env_cfg.snapshot_interval,
            help="Intervalle (en secondes) pour enregistrer des captures annotées (0 pour désactiver)",
        )
        parser.add_argument(
            "--snapshot-dir",
            default=env_cfg.snapshot_dir,
            help="Dossier où stocker les captures annotées",
        )
        args = parser.parse_args(argv)
        return cls(
            signaling_url=args.signaling,
            room=args.room,
            stun_servers=list(args.stun),
            disable_ssl_verify=args.disable_ssl_verify,
            audio_output_dir=args.audio_dir,
            record_audio=args.record_audio,
            log_level=args.log_level.upper(),
            snapshot_interval=max(0.0, args.snapshot_interval),
            snapshot_dir=args.snapshot_dir,
        )
