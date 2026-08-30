"""Tô màu ảnh nhiệt phía server.

**Đây là phương án dự phòng, không phải đường mặc định.** C12 tự tô màu được qua
lệnh ``IMG`` (11 palette, xem registry) — để camera làm thì không tốn CPU nào, và
ảnh ghi ra thẻ SD có màu giống hệt màn hình.

Giữ đường này cho hai trường hợp: pha 1 phát hiện ``IMG`` không phản hồi, và các
overlay sau này cần ảnh xám gốc để xử lý.

Dùng ``cv2.applyColorMap`` thay vì filter ``pseudocolor`` của ffmpeg: LUT 256 bậc
liên tục, không bị banding.
"""

from __future__ import annotations

import cv2
import numpy as np

#: Tên → hằng số OpenCV. ``white_hot``/``black_hot`` xử lý riêng vì chúng không
#: phải colormap mà là ảnh xám thuận/nghịch.
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
    """Tô màu một khung xám. Ảnh nhiều kênh được chuyển xám trước.

    Tên không nhận ra thì trả ảnh xám thuận — thà hiển thị đúng ảnh gốc còn hơn
    ném lỗi giữa luồng video.
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
    """Phóng to bằng ``INTER_NEAREST`` — giữ pixel sắc nét.

    Hợp với nguồn 384×288 của C12: nội suy mượt chỉ làm nhoè dữ liệu vốn đã ít.
    """
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_NEAREST)
