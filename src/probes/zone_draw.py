import math

import gi
import numpy as np

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

from .homography import Homography

logger = get_logger(__name__)

# NvDsDisplayMeta 하나가 담을 수 있는 선(line) 최대 개수 — pyds/NvDsDisplayMeta 풀 제약.
MAX_LINES_PER_DISPLAY_META = 16

# 아래서 구한 정확한 타원 곡선을 몇 각형으로 그릴지. MAX_LINES_PER_DISPLAY_META와 맞춰서
# forklift 1개당 display_meta 1개로 끝나게 한다(청크 로직은 있지만 굳이 여러 개 쓸 이유가 없다).
# 근사가 남는 지점은 "타원을 몇 각형으로 그릴지"뿐 — 타원 자체(중심/장단축/회전각)는
# _ellipse_from_ground_circle이 conic 행렬로 정확히 구한다.
GROUND_CIRCLE_SEGMENTS = 16


def _ellipse_from_ground_circle(
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


# bbox 하단(100%)과 상단(0%) 사이 어디를 "지면 접점"으로 볼지. 0이면 순수 하단 — 카메라와
# 가장 가까운 모서리라 지게차처럼 긴 물체는 실제 밑면 중심보다 카메라 쪽으로 치우친다. top-center를
# "얼마나 멀리/얼마나 큰 물체인지"의 깊이 힌트로 써서 살짝 위로(카메라 반대쪽으로) 보정한다.
# 완전한 3D 복원은 카메라 내부 파라미터(초점거리 등)가 없어 불가능해 휴리스틱이다 — 실제 화면
# 보고 튜닝할 값.
FOOT_POINT_BLEND = 0.25


def _ground_contact_point(obj_meta) -> tuple[float, float]:
    """bbox 하단 중앙에서 top-center 쪽으로 FOOT_POINT_BLEND만큼 당긴 지점 — "cuboid 중심"의
    근사치. bbox 중심(centroid, blend=0.5)을 쓰면 물체 높이의 절반만큼 지면에서 떠 있는 점이
    되어 호모그래피 투영이 크게 어긋나고, 순수 하단(blend=0)은 카메라에 가장 가까운 모서리로
    치우친다 — 그 중간 지점을 씀. zone_intrusion.py에도 같은 함수가 있다 — 몇 줄짜리 순수
    함수라 별도 공유 모듈을 두는 것보다 각자 갖는 쪽을 택했다(두 파일에서 값을 반드시 동일하게
    유지해야 그리기와 침입 판정이 같은 지점을 기준으로 삼는다)."""
    rect = obj_meta.rect_params
    x = rect.left + rect.width / 2
    bottom_y = rect.top + rect.height
    y = bottom_y - FOOT_POINT_BLEND * rect.height
    return x, y


def _tile_offset(source_id: int, tiler_cols: int, resize_w: int, resize_h: int) -> tuple[int, int]:
    """이 probe는 tiler 이후(합성 캔버스 좌표) pad에 붙어 있는데, 캘리브레이션(image_points)은
    합성 전 원본 카메라 프레임(input.resize 크기) 기준으로 잡는 게 자연스럽다. 그래서 호모그래피를
    적용하기 전엔 이 오프셋을 빼서 '그 소스만의 로컬 좌표'로 되돌리고, 그린 결과를 다시 합성
    캔버스에 놓기 전엔 더해준다. source_id -> 타일 위치가 소스 순서(streammux sink_i 요청 순서)와
    그대로 대응한다는 전제 — nvmultistreamtiler의 통상적 동작이지만 실제 박스에서 확인이 필요하다."""
    col = source_id % tiler_cols
    row = source_id // tiler_cols
    return col * resize_w, row * resize_h


def _draw_ring(batch_meta, frame_meta, display_meta, points: list[tuple[float, float]]):
    """points를 순서대로 이어 닫힌 다각형(고리)을 선분으로 그린다. display_meta 하나가 다 못
    담으면 새로 하나 더 뽑아 이어붙인다. 마지막으로 쓰던 display_meta를 돌려준다 — 호출자가
    프레임 끝에서 add_display_meta_to_frame 해야 한다."""
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        if display_meta is None or display_meta.num_lines >= MAX_LINES_PER_DISPLAY_META:
            if display_meta is not None:
                pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_lines = 0
        line = display_meta.line_params[display_meta.num_lines]
        # NvOSD_LineParams.x1/y1/x2/y2는 부호 없는 정수(guint)라 음수를 못 받는다. 호모그래피로
        # 역투영한 점은 forklift가 캘리브레이션 경계 근처에 있으면 화면 밖(음수)으로 나갈 수 있다
        # — 정상적인 케이스라 예외 처리 대신 0으로 클램프해서 화면 가장자리에 붙여 그린다.
        line.x1, line.y1, line.x2, line.y2 = (
            max(0, int(x1)), max(0, int(y1)), max(0, int(x2)), max(0, int(y2)),
        )
        line.line_width = 2
        line.line_color.set(1.0, 0.0, 0.0, 0.6)
        display_meta.num_lines += 1
    return display_meta


class ZoneDrawProbe:
    """forklift의 지면 위치(호모그래피로 계산) 중심에 실측 반경(radius_m)의 원을 그린다. 화면은
    원근 투영이라 지면 원을 그대로 그리면 픽셀상 정확하지 않다 — _ellipse_from_ground_circle이
    conic 행렬 변환으로 이미지 평면에서의 정확한 타원(중심/장단축/회전각)을 닫힌 형태로 구하고,
    그 타원 곡선을 다각형 선분으로 그린다. 카메라 거리/각도에 관계없이 실제 반경이 정확하게
    표현된다.

    순수 시각화 담당 — 침입 감지(zone_intrusion.ZoneIntrusionProbe)와는 바뀌는 이유가 다르다
    (표현 방식 vs 안전 판정 로직)고 판단해 파일까지 분리했다.

    forklift와 person이 각각 별도 PGIE(둘 다 단일 클래스)라 obj_meta.class_id만으로는 구분이
    안 된다 — 둘 다 0이다. obj_meta.unique_component_id(그 객체를 만든 PGIE의 gie-unique-id)로
    구분하고, 이 값은 절대 하드코딩하지 않고 pipeline.py에서 get_property('unique-id')로 읽은
    값을 그대로 받는다."""

    def __init__(
        self,
        class_id: int,
        radius_m: float,
        forklift_gie_id: int,
        homographies: list[Homography | None],
        tiler_cols: int,
        resize_width: int,
        resize_height: int,
    ):
        self.class_id = class_id
        self.radius_m = radius_m
        self.forklift_gie_id = forklift_gie_id
        self.homographies = homographies
        self.tiler_cols = tiler_cols
        self.resize_width = resize_width
        self.resize_height = resize_height

    def __call__(self, _pad, info):
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list

        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            source_id = frame_meta.source_id
            homography = self.homographies[source_id] if source_id < len(self.homographies) else None

            if homography is not None:
                offset_x, offset_y = _tile_offset(
                    source_id, self.tiler_cols, self.resize_width, self.resize_height
                )
                display_meta = None

                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    if (
                        obj_meta.unique_component_id == self.forklift_gie_id
                        and obj_meta.class_id == self.class_id
                    ):
                        lx, ly = _ground_contact_point(obj_meta)
                        local_point = (lx - offset_x, ly - offset_y)
                        ground_center = homography.to_ground([local_point])[0]

                        ring_local = _ellipse_from_ground_circle(
                            homography, ground_center, self.radius_m, GROUND_CIRCLE_SEGMENTS
                        )
                        if ring_local:
                            ring_tile = [(x + offset_x, y + offset_y) for x, y in ring_local]
                            display_meta = _draw_ring(batch_meta, frame_meta, display_meta, ring_tile)

                    l_obj = l_obj.next

                if display_meta is not None:
                    pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
