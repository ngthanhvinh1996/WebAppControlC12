"""Server-side thermal colorization.

**This is the fallback, not the default path.** The C12 colorizes on its own via
the ``IMG`` command (11 palettes, see the registry) — letting the camera do it
costs no CPU here, and the stills written to the SD card then match what is on
screen.

This path exists for two cases: phase 1 finding that ``IMG`` does not answer, and
future overlays that need the original grayscale image to work from.

Uses ``cv2.applyColorMap`` rather than ffmpeg's ``pseudocolor`` filter: a
continuous 256-step LUT, so no banding.
"""

from __future__ import annotations

import cv2
import numpy as np

#: Name → OpenCV constant. ``white_hot``/``black_hot`` are handled separately
#: because they are not colormaps but plain and inverted grayscale.
COLORMAPS: dict[str, int | None] = {
    "white_hot": None,
    "black_hot": None,
    "ironbow": cv2.COLORMAP_INFERNO,
    "rainbow": cv2.COLORMAP_JET,
    "turbo": getattr(cv2, "COLORMAP_TURBO", cv2.COLORMAP_JET),
    "hot": cv2.COLORMAP_HOT,
    "medical": cv2.COLORMAP_BONE,
    "night": cv2.COLORMAP_OCEAN,
    "sepia": cv2.COLORMAP_PINK,
}

DEFAULT = "white_hot"


def available() -> list[str]:
    return list(COLORMAPS)


def apply(image: np.ndarray, name: str = DEFAULT) -> np.ndarray:
    """Colorize a grayscale frame. Multi-channel images are converted first.

    An unrecognized name returns plain grayscale — better to show the true
    original than to raise in the middle of a video stream.
    """
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    if name == "black_hot":
        return cv2.cvtColor(cv2.bitwise_not(gray), cv2.COLOR_GRAY2BGR)
    code = COLORMAPS.get(name)
    if code is None:
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    return cv2.applyColorMap(gray, code)


def upscale(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Scale up with ``INTER_NEAREST`` — keeps pixels crisp.

    Right for the C12's 384×288 source: smooth interpolation only blurs data
    that was already sparse.
    """
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_NEAREST)
