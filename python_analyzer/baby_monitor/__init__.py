"""
Baby monitor analyzer package.

Provides WebRTC client components to consume the BabyPhone stream and
run video/audio analytics for movement and cry detection.
"""

from .analyzer import AnalyzerClient  # noqa: F401
from .config import AnalyzerConfig  # noqa: F401
