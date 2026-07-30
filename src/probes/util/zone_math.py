"""zone_draw.py가 쓰는 순수 좌표/기하 계산. pyds나 Gst에 의존하지 않는다 — 이 프로젝트
대부분은 DeepStream 컨테이너 안에서만 실행/검증 가능한데(CLAUDE.md 참고), 여기만 예외적으로
평범한 파이썬(+numpy)이라 컨테이너 밖에서도 단위 테스트가 가능하다."""

import math

import numpy as np

from logger import get_logger

from .homography import Homography

logger = get_logger(__name__)

# bbox 하단(100%)과 상단(0%) 사이 어디를 "지면 접점"으로 볼지. 0이면 순수 하단 — 카메라와
# 가장 가까운 모서리라 지게차처럼 긴 물체는 실제 밑면 중심보다 카메라 쪽으로 치우친다. top-center를
# "얼마나 멀리/얼마나 큰 물체인지"의 깊이 힌트로 써서 살짝 위로(카메라 반대쪽으로) 보정한다.
# 완전한 3D 복원은 카메라 내부 파라미터(초점거리 등)가 없어 불가능해 휴리스틱이다 — 실제 화면
# 보고 튜닝할 값. zone_intrusion.py에도 같은 이름의 상수/함수가 있다 — 두 파일이 같은 지면 접점
# 기준을 써야 그리기와 침입 판정이 어긋나지 않는데, 이 프로젝트는 draw/intrusion을 딱 두 파일로만
# 나누기로 했었어서(공유 모듈을 또 만들지 않기로) zone_intrusion.py는 이 모듈을 import하지 않고
# 자기 사본을 따로 갖고 있다. 값을 바꿀 땐 두 곳 다 바꿔야 한다.
FOOT_POINT_BLEND = 0.25


def ground_contact_point(obj_meta) -> tuple[float, float]:
    """bbox 하단 중앙에서 top-center 쪽으로 FOOT_POINT_BLEND만큼 당긴 지점 — "cuboid 중심"의
    근사치. bbox 중심(centroid, blend=0.5)을 쓰면 물체 높이의 절반만큼 지면에서 떠 있는 점이
    되어 호모그래피 투영이 크게 어긋나고, 순수 하단(blend=0)은 카메라에 가장 가까운 모서리로
    치우친다 — 그 중간 지점을 씀."""
    rect = obj_meta.rect_params
    x = rect.left + rect.width / 2
    bottom_y = rect.top + rect.height
    y = bottom_y - FOOT_POINT_BLEND * rect.height
    return x, y


def tile_offset(source_id: int, tiler_cols: int, resize_w: int, resize_h: int) -> tuple[int, int]:
    """zone_draw는 tiler 이후(합성 캔버스 좌표) pad에 붙어 있는데, 캘리브레이션(image_points)은
    합성 전 원본 카메라 프레임(input.resize 크기) 기준으로 잡는 게 자연스럽다. 그래서 호모그래피를
    적용하기 전엔 이 오프셋을 빼서 '그 소스만의 로컬 좌표'로 되돌리고, 그린 결과를 다시 합성
    캔버스에 놓기 전엔 더해준다. source_id -> 타일 위치가 소스 순서(streammux sink_i 요청 순서)와
    그대로 대응한다는 전제 — nvmultistreamtiler의 통상적 동작이지만 실제 박스에서 확인이 필요하다."""
    col = source_id % tiler_cols
    row = source_id // tiler_cols
    return col * resize_w, row * resize_h


def clip_segment(
    x1: float, y1: float, x2: float, y2: float, xmin: float, ymin: float, xmax: float, ymax: float
) -> tuple[float, float, float, float] | None:
    """Liang-Barsky 선분 클리핑. 선분 (x1,y1)-(x2,y2) 중 사각형 [xmin,xmax]x[ymin,ymax] 밖으로
    나가는 부분을 잘라내고 보이는 구간만 돌려준다. 완전히 밖이면 None.

    좌표를 그냥 0으로 눌러 담으면(max(0, ...)) 원래 화면 밖으로 나가야 할 점이 (0, y)나
    (x, 0)으로 억지로 끌려와서 실제로는 없는 자리로 선이 이어져 도형 모양 자체가 일그러진다.
    클리핑하면 보이는 부분만 정확한 위치에 그려진다."""
    dx = x2 - x1
    dy = y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, x1 - xmin),
        (dx, xmax - x1),
        (-dy, y1 - ymin),
        (dy, ymax - y1),
    ):
        if p == 0:
            if q < 0:
                return None  # 경계와 평행한데 그 밖에 있음 — 안 보임
            continue
        r = q / p
        if p < 0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r
    if t0 > t1:
        return None
    return x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy


def ellipse_from_ground_circle(
    homography: Homography, center: tuple[float, float], radius: float, segments: int
) -> list[tuple[float, float]]:
    """지면 원(center, radius)이 호모그래피를 통해 이미지 평면에 투영되면 일반적으로 타원이
    된다. 원 둘레를 여러 점으로 나눠 점마다 개별적으로 변환한 뒤 선분으로 이으면, 실제 곡선을
    잘게 쪼갠 현(chord)으로 근사하는 것이라 오차가 남는다 — 특히 원근이 심한 각도에서 두
    샘플점 사이 곡률이 큰 구간은 오차가 커진다.

    대신 원을 conic(2차 곡선) 행렬로 표현하고, 호모그래피를 그 행렬에 직접 대수적으로 적용해서
    이미지 평면의 타원 파라미터(중심/장단축/회전각)를 닫힌 형태로 정확히 구한다. 원의 conic
    행렬 C_ground는 p^T C p = 0 <=> (x-cx)^2+(y-cy)^2=r^2 (p=(x,y,1) 동차좌표)를 만족하고,
    x_ground = H @ x_image (H = homography.image_to_ground)이므로
    x_image^T (H^T C_ground H) x_image = 0 — 즉 C_image = H^T C_ground H가 이미지 평면에서의
    정확한 conic이다. 이후 남는 근사는 "이 정확한 타원을 몇 각형 선분으로 그릴지"뿐이다.

    forklift가 호모그래피의 소실선(vanishing line) 근처에 있으면 conic이 타원이 아니라
    쌍곡선/포물선으로 퇴화할 수 있다 — 이 경우 빈 리스트를 돌려주고 호출자가 그리기를 건너뛴다."""
    gcx, gcy = center
    c_ground = np.array(
        [
            [1.0, 0.0, -gcx],
            [0.0, 1.0, -gcy],
            [-gcx, -gcy, gcx * gcx + gcy * gcy - radius * radius],
        ]
    )
    h = homography.image_to_ground
    c_image = h.T @ c_ground @ h

    quad = c_image[:2, :2]  # [[A, B/2], [B/2, C]]
    lin = c_image[:2, 2]  # [D/2, E/2]
    try:
        center_img = np.linalg.solve(quad, -lin)
    except np.linalg.LinAlgError:
        logger.warning("conic 중심 계산 실패(특이 행렬) — 해당 forklift는 이번 프레임에 그리기 건너뜀")
        return []

    center_h = np.array([center_img[0], center_img[1], 1.0])
    f_prime = float(center_h @ c_image @ center_h)

    eigvals, eigvecs = np.linalg.eigh(quad)
    axes_sq = -f_prime / eigvals
    if np.any(axes_sq <= 0):
        logger.warning(
            "conic이 타원이 아님(퇴화) — forklift가 소실선 근처로 추정, 이번 프레임에 그리기 건너뜀"
        )
        return []
    a, b = np.sqrt(axes_sq)
    theta = math.atan2(eigvecs[1, 0], eigvecs[0, 0])

    cx, cy = center_img
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return [
        (
            cx + a * math.cos(2 * math.pi * i / segments) * cos_t
            - b * math.sin(2 * math.pi * i / segments) * sin_t,
            cy + a * math.cos(2 * math.pi * i / segments) * sin_t
            + b * math.sin(2 * math.pi * i / segments) * cos_t,
        )
        for i in range(segments)
    ]
