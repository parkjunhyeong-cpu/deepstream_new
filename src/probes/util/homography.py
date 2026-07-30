"""이미지 좌표 <-> 지면(BEV, bird's-eye-view) 좌표 변환.

카메라별 캘리브레이션 대응점(image_points[i] <-> ground_points[i], 4쌍 이상)으로 호모그래피
행렬을 한 번만 계산해 재사용한다 — cv2.findHomography는 프레임마다 다시 풀 만큼 싸지 않다."""

import cv2
import numpy as np

from logger import get_logger

logger = get_logger(__name__)

MIN_CALIBRATION_POINTS = 4


class Homography:
    def __init__(self, image_points: list[dict], ground_points: list[dict]):
        if len(image_points) < MIN_CALIBRATION_POINTS or len(image_points) != len(ground_points):
            raise ValueError(
                f"호모그래피는 image_points/ground_points가 각각 {MIN_CALIBRATION_POINTS}쌍 이상, "
                "개수가 같아야 한다"
            )
        src = np.array([[p["x"], p["y"]] for p in image_points], dtype=np.float32)
        dst = np.array([[p["x"], p["y"]] for p in ground_points], dtype=np.float32)
        matrix, _ = cv2.findHomography(src, dst)
        if matrix is None:
            raise ValueError("호모그래피 계산 실패 — 대응점이 일직선상에 있는 등 배치를 확인하라")
        self.image_to_ground = matrix
        self.ground_to_image = np.linalg.inv(matrix)

    def to_ground(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return _transform(points, self.image_to_ground)

    def to_image(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return _transform(points, self.ground_to_image)


def _transform(points: list[tuple[float, float]], matrix: np.ndarray) -> list[tuple[float, float]]:
    arr = np.array(points, dtype=np.float32).reshape(-1, 1, 2)
    out = cv2.perspectiveTransform(arr, matrix).reshape(-1, 2)
    return [(float(x), float(y)) for x, y in out]


def build_homographies(sources_cfg: list[dict]) -> list[Homography | None]:
    """control-api의 input.sources 순서(=frame_meta.source_id로 가정)대로 Homography 리스트를
    만든다. 아직 캘리브레이션 안 된 소스(image_points/ground_points 비어있음)는 None — zone
    probe가 그 소스는 원 그리기/침입 감지를 건너뛴다."""
    homographies: list[Homography | None] = []
    for src in sources_cfg:
        image_points = src.get("image_points") or []
        ground_points = src.get("ground_points") or []
        if not image_points or not ground_points:
            logger.warning(
                "source '%s': 호모그래피 캘리브레이션 없음 — zone 기능 건너뜀", src.get("name")
            )
            homographies.append(None)
            continue
        homographies.append(Homography(image_points, ground_points))
    return homographies
