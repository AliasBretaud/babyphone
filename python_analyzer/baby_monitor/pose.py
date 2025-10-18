from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List, Optional

import cv2
import numpy as np

from . import protobuf_compat  # noqa: F401  Must load before MediaPipe imports
from mediapipe import solutions as mp_solutions
from mediapipe.python.solutions import drawing_styles, drawing_utils

PoseLandmark = mp_solutions.pose.PoseLandmark


@dataclass
class PoseObservation:
    timestamp: float
    posture: str
    angle_deg: float
    movement_score: float
    landmarks: Optional[List] = None
    movement_detected: bool = False
    pose_landmarks: Optional[object] = None


class PoseAnalyzer:
    """
    Analyse les frames vidéo pour détecter la posture et le mouvement.

    S'appuie sur MediaPipe Pose (modèle pré-entrainé) pour obtenir des
    landmarks 3D indépendants de l'angle caméra (pose_world_landmarks).
    """

    def __init__(
        self,
        movement_threshold: float = 0.08,
        smoothing: float = 0.6,
    ) -> None:
        self._pose = mp_solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            enable_segmentation=False,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._prev_world_landmarks: Optional[np.ndarray] = None
        self._movement_threshold = movement_threshold
        self._angle_smoothing = smoothing
        self._previous_angle = None
        style_fn = getattr(
            drawing_styles, "get_default_pose_landmarks_style", None
        )
        if callable(style_fn):
            self._landmark_style = style_fn()
        else:
            self._landmark_style = drawing_utils.DrawingSpec(
                color=(0, 255, 0), thickness=2, circle_radius=2
            )
        connection_fn = getattr(
            drawing_styles, "get_default_pose_connections_style", None
        )
        if callable(connection_fn):
            self._connection_style = connection_fn()
        else:
            self._connection_style = drawing_utils.DrawingSpec(
                color=(255, 0, 0), thickness=2
            )

    def close(self) -> None:
        self._pose.close()

    def process_frame(self, frame_bgr: np.ndarray) -> Optional[PoseObservation]:
        # MediaPipe attend des images RGB et sans écriture.
        rgb_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        result = self._pose.process(rgb_frame)
        pose_landmarks = result.pose_landmarks
        world_landmarks = result.pose_world_landmarks
        if not world_landmarks:
            return None

        # Convertit en tableau numpy de shape (33, 3).
        landmarks = np.array(
            [
                [lm.x, lm.y, lm.z]
                for lm in world_landmarks.landmark
            ],
            dtype=np.float32,
        )

        hip_center = self._average_landmarks(
            landmarks, PoseLandmark.LEFT_HIP, PoseLandmark.RIGHT_HIP
        )
        shoulder_center = self._average_landmarks(
            landmarks, PoseLandmark.LEFT_SHOULDER, PoseLandmark.RIGHT_SHOULDER
        )

        torso_vector = shoulder_center - hip_center
        angle_deg = self._angle_with_vertical(torso_vector)
        smoothed_angle = self._smooth_angle(angle_deg)
        posture = self._classify_posture(smoothed_angle)

        movement_score, movement_detected = self._detect_movement(landmarks)
        timestamp = time.time()

        return PoseObservation(
            timestamp=timestamp,
            posture=posture,
            angle_deg=smoothed_angle,
            movement_score=movement_score,
            landmarks=landmarks,
            movement_detected=movement_detected,
            pose_landmarks=pose_landmarks,
        )

    def annotate_frame(self, frame_bgr: np.ndarray, pose_landmarks) -> np.ndarray:
        if not pose_landmarks:
            return frame_bgr
        annotated = frame_bgr.copy()
        drawing_utils.draw_landmarks(
            annotated,
            pose_landmarks,
            mp_solutions.pose.POSE_CONNECTIONS,
            landmark_drawing_spec=self._landmark_style,
            connection_drawing_spec=self._connection_style,
        )
        return annotated

    def _average_landmarks(
        self, landmarks: np.ndarray, idx_a: PoseLandmark, idx_b: PoseLandmark
    ) -> np.ndarray:
        return (landmarks[idx_a.value] + landmarks[idx_b.value]) / 2.0

    def _angle_with_vertical(self, vector: np.ndarray) -> float:
        """Retourne l'angle (en degrés) entre le torse et l'axe vertical."""
        vertical = np.array([0.0, -1.0, 0.0], dtype=np.float32)
        norm_v = np.linalg.norm(vector)
        if norm_v < 1e-6:
            return 90.0
        cos_theta = np.dot(vector, vertical) / (norm_v * np.linalg.norm(vertical))
        cos_theta = float(np.clip(cos_theta, -1.0, 1.0))
        return math.degrees(math.acos(cos_theta))

    def _smooth_angle(self, current_angle: float) -> float:
        if self._previous_angle is None:
            self._previous_angle = current_angle
        else:
            self._previous_angle = (
                self._angle_smoothing * self._previous_angle
                + (1.0 - self._angle_smoothing) * current_angle
            )
        return self._previous_angle

    def _classify_posture(self, angle_deg: float) -> str:
        if angle_deg < 25:
            return "standing"
        if angle_deg < 55:
            return "sitting"
        return "lying"

    def _detect_movement(self, landmarks: np.ndarray) -> tuple[float, bool]:
        if self._prev_world_landmarks is None:
            self._prev_world_landmarks = landmarks
            return 0.0, False

        diffs = np.linalg.norm(
            landmarks - self._prev_world_landmarks, axis=1
        )
        score = float(np.mean(diffs))
        self._prev_world_landmarks = landmarks
        return score, score > self._movement_threshold
