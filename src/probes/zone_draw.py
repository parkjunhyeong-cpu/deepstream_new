import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

from .homography import Homography

logger = get_logger(__name__)

# NvDsDisplayMeta 하나가 담을 수 있는 선(line) 최대 개수 — pyds/NvDsDisplayMeta 풀 제약.
MAX_LINES_PER_DISPLAY_META = 16

# BEV 원을 이미지에 그릴 때 몇 각형으로 근사할지. MAX_LINES_PER_DISPLAY_META와 맞춰서 forklift
# 1개당 display_meta 1개로 끝나게 한다(청크 로직은 있지만 굳이 여러 개 쓸 이유가 없다).
GROUND_CIRCLE_SEGMENTS = 16


def _ground_contact_point(obj_meta) -> tuple[float, float]:
    """bbox 하단 중앙 — 카메라가 내려다보는 각도에서 물체가 지면에 닿는 지점의 근사치.
    bbox 중심(centroid)을 쓰면 물체 높이만큼 지면에서 떠 있는 점이 되어 호모그래피 투영이
    어긋난다 (BEV/IPM에서 흔히 쓰는 표준 근사). zone_intrusion.py에도 같은 함수가 있다 —
    3줄짜리 순수 함수라 별도 공유 모듈을 두는 것보다 각자 갖는 쪽을 택했다."""
    rect = obj_meta.rect_params
    return rect.left + rect.width / 2, rect.top + rect.height


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
        line.x1, line.y1, line.x2, line.y2 = int(x1), int(y1), int(x2), int(y2)
        line.line_width = 2
        line.line_color.set(1.0, 0.0, 0.0, 0.6)
        display_meta.num_lines += 1
    return display_meta


class ZoneDrawProbe:
    """forklift의 지면 위치(호모그래피로 계산) 중심에 실측 반경(radius_m)의 원을 그린다. 화면은
    원근 투영이라 지면 원을 그대로 그리면 픽셀상 정확하지 않다 — 지면 원 둘레의 점들을 다시
    이미지 좌표로 역투영해서 다각형으로 이어 그려야 카메라 거리/각도에 관계없이 실제 반경이
    맞게 보인다.

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
                        gcx, gcy = homography.to_ground([local_point])[0]

                        ring_ground = [
                            (
                                gcx + self.radius_m * math.cos(2 * math.pi * i / GROUND_CIRCLE_SEGMENTS),
                                gcy + self.radius_m * math.sin(2 * math.pi * i / GROUND_CIRCLE_SEGMENTS),
                            )
                            for i in range(GROUND_CIRCLE_SEGMENTS)
                        ]
                        ring_local = homography.to_image(ring_ground)
                        ring_tile = [(x + offset_x, y + offset_y) for x, y in ring_local]
                        display_meta = _draw_ring(batch_meta, frame_meta, display_meta, ring_tile)

                    l_obj = l_obj.next

                if display_meta is not None:
                    pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
