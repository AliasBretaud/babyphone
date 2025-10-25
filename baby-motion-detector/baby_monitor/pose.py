from __future__ import annotations

import math
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python.vision import (
    PoseLandmarker,
    PoseLandmarkerOptions,
    PoseLandmarkerResult,
    RunningMode,
)

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)
MODEL_NAME = "pose_landmarker_full.task"

# Landmark indices in MediaPipe pose model (33 landmarks).
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_HIP = 23
RIGHT_HIP = 24
LEFT_KNEE = 25
RIGHT_KNEE = 26
LEFT_ANKLE = 27
RIGHT_ANKLE = 28

# Connections for drawing (subset sufficient for skeleton overlay).
POSE_CONNECTIONS = [
    (11, 12), (11, 23), (12, 24), (23, 24),  # torso
    (11, 13), (13, 15), (12, 14), (14, 16),  # arms
    (23, 25), (25, 27), (24, 26), (26, 28),  # legs
]


@dataclass
class PoseObservation:
    timestamp: float
    posture: str
    movement_score: float
    movement_detected: bool
    pose_landmarks: Optional[list] = None
    extras: Dict[str, float] = field(default_factory=dict)


class PoseAnalyzer:
    """Pose analyzer based on MediaPipe Tasks PoseLandmarker."""

    def __init__(
        self,
        movement_threshold: float = 0.08,
        smoothing: float = 0.6,
        visibility_threshold: float = 0.5,
        standing_angle: float = 35.0,
        lying_angle: float = 65.0,
        lying_forward_ratio: float = 0.45,
        standing_forward_ratio: float = 0.35,
        standing_knee_angle: float = 150.0,
        sitting_knee_min: float = 70.0,
        sitting_knee_max: float = 150.0,
        standing_leg_extension_min: float = 0.22,
        sitting_leg_extension_max: float = 0.18,
    ) -> None:
        model_path = self._ensure_model()
        base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
        options = PoseLandmarkerOptions(
            base_options=base_options,
            running_mode=RunningMode.IMAGE,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = PoseLandmarker.create_from_options(options)
        self._movement_threshold = movement_threshold
        self._angle_smoothing = smoothing
        self._previous_angle: Optional[float] = None
        self._prev_world_landmarks: Optional[np.ndarray] = None
        self._visibility_threshold = visibility_threshold
        self._standing_angle = standing_angle
        self._lying_angle = lying_angle
        self._lying_forward_ratio = lying_forward_ratio
        self._standing_forward_ratio = standing_forward_ratio
        self._standing_knee_angle = standing_knee_angle
        self._sitting_knee_min = sitting_knee_min
        self._sitting_knee_max = sitting_knee_max
        self._standing_leg_extension_min = standing_leg_extension_min
        self._sitting_leg_extension_max = sitting_leg_extension_max

    def close(self) -> None:
        self._landmarker.close()

    def process_frame(self, frame_bgr: np.ndarray) -> Optional[PoseObservation]:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
        result: PoseLandmarkerResult = self._landmarker.detect(mp_image)
        if not result.pose_landmarks or not result.pose_world_landmarks:
            self._invalidate_state()
            return None

        pose_landmarks = result.pose_landmarks[0]
        world_landmarks = np.array(
            [[lm.x, lm.y, lm.z] for lm in result.pose_world_landmarks[0]],
            dtype=np.float32,
        )
        visibility = np.array([lm.visibility for lm in pose_landmarks], dtype=np.float32)
        visibility_mask = visibility >= self._visibility_threshold

        torso_vector = self._torso_vector(world_landmarks)
        torso_norm = np.linalg.norm(torso_vector)
        if torso_norm < 1e-6:
            self._invalidate_state()
            return None

        angle_deg = self._angle_with_vertical(torso_vector)
        smoothed_angle = self._smooth_angle(angle_deg)
        forward_component = abs(torso_vector[2]) / torso_norm
        avg_knee_angle, left_knee_angle, right_knee_angle = self._compute_knee_angles(
            world_landmarks, visibility_mask
        )
        leg_extension = self._leg_extension(pose_landmarks)
        hip_height = self._mean_y(pose_landmarks, [LEFT_HIP, RIGHT_HIP])
        knee_height = self._mean_y(pose_landmarks, [LEFT_KNEE, RIGHT_KNEE])
        ankle_height = self._mean_y(pose_landmarks, [LEFT_ANKLE, RIGHT_ANKLE])
        leg_span = None
        knee_span = None
        if hip_height is not None and ankle_height is not None:
            leg_span = float(ankle_height - hip_height)
        if hip_height is not None and knee_height is not None:
            knee_span = float(knee_height - hip_height)

        posture = self._classify_posture(
            smoothed_angle,
            forward_component,
            avg_knee_angle,
            leg_extension,
            leg_span,
            knee_span,
        )

        movement_score, movement_detected = self._movement_metric(world_landmarks, visibility_mask)
        extras = {
            "torso_angle": float(smoothed_angle),
            "forward_component": forward_component,
            "movement_score": movement_score,
            "posture": posture,
            "avg_knee_angle": float(avg_knee_angle) if avg_knee_angle is not None else None,
            "left_knee_angle": float(left_knee_angle) if left_knee_angle is not None else None,
            "right_knee_angle": float(right_knee_angle) if right_knee_angle is not None else None,
            "leg_extension": leg_extension,
            "leg_span": leg_span,
            "knee_span": knee_span,
            "hip_height": hip_height,
            "knee_height": knee_height,
            "ankle_height": ankle_height,
        }

        extras_clean = {k: v for k, v in extras.items() if v is not None}

        return PoseObservation(
            timestamp=time.time(),
            posture=posture,
            movement_score=movement_score,
            movement_detected=movement_detected,
            pose_landmarks=pose_landmarks,
            extras=extras_clean,
        )

    def annotate_frame(self, frame_bgr: np.ndarray, pose_landmarks) -> np.ndarray:
        if not pose_landmarks:
            return frame_bgr
        annotated = frame_bgr.copy()
        height, width = frame_bgr.shape[:2]
        points = []
        for lm in pose_landmarks:
            x = int(lm.x * width)
            y = int(lm.y * height)
            vis = getattr(lm, "visibility", 1.0)
            points.append((x, y, vis))
            if vis >= self._visibility_threshold:
                cv2.circle(annotated, (x, y), 4, (0, 255, 0), -1)

        for start, end in POSE_CONNECTIONS:
            x1, y1, v1 = points[start]
            x2, y2, v2 = points[end]
            if v1 >= self._visibility_threshold and v2 >= self._visibility_threshold:
                cv2.line(annotated, (x1, y1), (x2, y2), (0, 255, 255), 2)
        return annotated

    def _movement_metric(
        self, world_landmarks: np.ndarray, visibility_mask: np.ndarray
    ) -> Tuple[float, bool]:
        landmarks = world_landmarks.copy()
        landmarks[~visibility_mask] = np.nan
        if self._prev_world_landmarks is None:
            self._prev_world_landmarks = landmarks
            return 0.0, False
        prev = self._prev_world_landmarks
        valid = visibility_mask & np.isfinite(prev[:, 0])
        if not np.any(valid):
            self._prev_world_landmarks = landmarks
            return 0.0, False
        diffs = np.linalg.norm(landmarks[valid] - prev[valid], axis=1)
        score = float(np.mean(diffs))
        self._prev_world_landmarks = landmarks
        return score, score > self._movement_threshold

    def _torso_vector(self, world_landmarks: np.ndarray) -> np.ndarray:
        left_shoulder = world_landmarks[LEFT_SHOULDER]
        right_shoulder = world_landmarks[RIGHT_SHOULDER]
        left_hip = world_landmarks[LEFT_HIP]
        right_hip = world_landmarks[RIGHT_HIP]
        shoulder_center = (left_shoulder + right_shoulder) / 2.0
        hip_center = (left_hip + right_hip) / 2.0
        return shoulder_center - hip_center

    def _angle_with_vertical(self, vector: np.ndarray) -> float:
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

    def _classify_posture(
        self,
        angle_deg: float,
        forward_component: float,
        avg_knee_angle: Optional[float],
        leg_extension: Optional[float],
        leg_span: Optional[float],
        knee_span: Optional[float],
    ) -> str:
        lying_forward_threshold = self._lying_forward_ratio + 0.08
        lying_angle_threshold = self._lying_angle + 5.0
        if angle_deg >= lying_angle_threshold or forward_component >= lying_forward_threshold:
            return "lying"

        standing_score = 0
        sitting_score = 0

        if avg_knee_angle is not None:
            if avg_knee_angle >= self._standing_knee_angle:
                standing_score += 1
            if self._sitting_knee_min <= avg_knee_angle <= self._sitting_knee_max:
                sitting_score += 1

        if leg_extension is not None:
            if leg_extension >= self._standing_leg_extension_min:
                standing_score += 1
            if leg_extension <= self._sitting_leg_extension_max:
                sitting_score += 1

        if leg_span is not None:
            if leg_span >= 0.22:
                standing_score += 1
            if leg_span < 0.18:
                sitting_score += 1

        if knee_span is not None:
            if knee_span >= 0.12:
                standing_score += 1
            if knee_span < 0.10:
                sitting_score += 1

        if angle_deg <= (self._standing_angle + 5.0) and forward_component <= (self._standing_forward_ratio + 0.05):
            standing_score += 1
        elif angle_deg <= (self._lying_angle + 8.0):
            sitting_score += 1

        if standing_score >= max(sitting_score, 2):
            return "standing"
        if sitting_score >= max(standing_score, 2):
            return "sitting"
        # fallback heuristics
        if angle_deg <= (self._lying_angle + 8.0):
            return "sitting"
        if angle_deg <= (self._standing_angle + 6.0):
            return "standing"
        return "lying"

    def _compute_knee_angles(
        self, world_landmarks: np.ndarray, visibility_mask: np.ndarray
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        left = right = None
        if self._landmarks_visible(visibility_mask, (LEFT_HIP, LEFT_KNEE, LEFT_ANKLE)):
            left = self._joint_angle(
                world_landmarks[LEFT_HIP],
                world_landmarks[LEFT_KNEE],
                world_landmarks[LEFT_ANKLE],
            )
        if self._landmarks_visible(visibility_mask, (RIGHT_HIP, RIGHT_KNEE, RIGHT_ANKLE)):
            right = self._joint_angle(
                world_landmarks[RIGHT_HIP],
                world_landmarks[RIGHT_KNEE],
                world_landmarks[RIGHT_ANKLE],
            )
        available = [angle for angle in (left, right) if angle is not None]
        avg = float(np.mean(available)) if available else None
        return avg, left, right

    def _joint_angle(self, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
        ba = a - b
        bc = c - b
        norm_ba = np.linalg.norm(ba)
        norm_bc = np.linalg.norm(bc)
        if norm_ba < 1e-6 or norm_bc < 1e-6:
            return 0.0
        cos_angle = np.dot(ba, bc) / (norm_ba * norm_bc)
        cos_angle = float(np.clip(cos_angle, -1.0, 1.0))
        return math.degrees(math.acos(cos_angle))

    def _leg_extension(self, pose_landmarks) -> Optional[float]:
        left_hip = pose_landmarks[LEFT_HIP]
        right_hip = pose_landmarks[RIGHT_HIP]
        left_ankle = pose_landmarks[LEFT_ANKLE]
        right_ankle = pose_landmarks[RIGHT_ANKLE]
        hips = [lm for lm in (left_hip, right_hip) if lm.visibility >= self._visibility_threshold]
        ankles = [lm for lm in (left_ankle, right_ankle) if lm.visibility >= self._visibility_threshold]
        if not hips or not ankles:
            return None
        hip_y = np.mean([lm.y for lm in hips])
        ankle_y = np.mean([lm.y for lm in ankles])
        return float(ankle_y - hip_y)

    def _landmarks_visible(
        self, visibility_mask: np.ndarray, indices: Tuple[int, ...]
    ) -> bool:
        return all(visibility_mask[idx] for idx in indices)

    def _mean_y(self, pose_landmarks, indices: list[int]) -> Optional[float]:
        values = []
        for idx in indices:
            lm = pose_landmarks[idx]
            vis = getattr(lm, "visibility", 1.0)
            if vis >= self._visibility_threshold:
                values.append(lm.y)
        if not values:
            return None
        return float(np.mean(values))

    def _ensure_model(self) -> Path:
        override = os.getenv("POSE_MODEL_PATH")
        if override:
            return Path(override)
        models_dir = Path(__file__).resolve().parents[1] / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        model_path = models_dir / MODEL_NAME
        if not model_path.exists():
            try:
                urllib.request.urlretrieve(MODEL_URL, model_path)
            except Exception as exc:
                raise RuntimeError(
                    "Unable to download pose landmarker model. "
                    "Set POSE_MODEL_PATH to a local .task file."
                ) from exc
        return model_path

    def _invalidate_state(self) -> None:
        self._previous_angle = None
        self._prev_world_landmarks = None
