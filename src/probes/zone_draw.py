import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

from .util.homography import Homography
from .util.zone_math import clip_segment, ellipse_from_ground_circle, ground_contact_point, tile_offset

logger = get_logger(__name__)

# NvDsDisplayMeta 하나가 담을 수 있는 선(line) 최대 개수 — pyds/NvDsDisplayMeta 풀 제약.
MAX_LINES_PER_DISPLAY_META = 16

# zone_math.ellipse_from_ground_circle이 구한 정확한 타원 곡선을 몇 각형으로 그릴지.
# MAX_LINES_PER_DISPLAY_META와 맞춰서 forklift 1개당 display_meta 1개로 끝나게 한다
# (청크 로직은 있지만 굳이 여러 개 쓸 이유가 없다). 근사가 남는 지점은 "타원을 몇 각형으로
# 그릴지"뿐 — 타원 자체(중심/장단축/회전각)는 conic 행렬로 정확히 구해져 있다.
GROUND_CIRCLE_SEGMENTS = 16


def _draw_ring(
    batch_meta,
    frame_meta,
    display_meta,
    points: list[tuple[float, float]],
    bounds: tuple[float, float, float, float],
):
    """points를 순서대로 이어 닫힌 다각형(고리)을 선분으로 그린다. 각 선분은 이 소스가 속한
    타일 영역(bounds = (xmin, ymin, xmax, ymax))으로 클리핑한다 — 안 그러면 호모그래피로
    역투영한 점이 화면 밖으로 나갈 때 도형이 일그러지거나, 옆 채널의 타일 영역까지 선이
    새어 들어갈 수 있다. 완전히 타일 밖인 선분은 건너뛴다. display_meta 하나가 다 못 담으면
    새로 하나 더 뽑아 이어붙인다. 마지막으로 쓰던 display_meta를 돌려준다 — 호출자가 프레임
    끝에서 add_display_meta_to_frame 해야 한다."""
    xmin, ymin, xmax, ymax = bounds
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        clipped = clip_segment(x1, y1, x2, y2, xmin, ymin, xmax, ymax)
        if clipped is None:
            continue
        cx1, cy1, cx2, cy2 = clipped

        if display_meta is None or display_meta.num_lines >= MAX_LINES_PER_DISPLAY_META:
            if display_meta is not None:
                pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            display_meta.num_lines = 0
        line = display_meta.line_params[display_meta.num_lines]
        line.x1, line.y1, line.x2, line.y2 = int(cx1), int(cy1), int(cx2), int(cy2)
        line.line_width = 2
        line.line_color.set(1.0, 0.0, 0.0, 0.6)
        display_meta.num_lines += 1
    return display_meta


class ZoneDrawProbe:
    """forklift의 지면 위치(호모그래피로 계산) 중심에 실측 반경(radius_m)의 원을 그린다. 화면은
    원근 투영이라 지면 원을 그대로 그리면 픽셀상 정확하지 않다 — zone_math.ellipse_from_ground_circle이
    conic 행렬 변환으로 이미지 평면에서의 정확한 타원(중심/장단축/회전각)을 닫힌 형태로 구하고,
    그 타원 곡선을 다각형 선분으로 그린다. 카메라 거리/각도에 관계없이 실제 반경이 정확하게
    표현된다.

    좌표/기하 계산(zone_math.py, pyds 비의존)과 pyds OSD 그리기·probe 배선(이 파일)을 나눴다
    — 전자는 이 프로젝트에서 드물게 컨테이너 밖에서도 단위 테스트가 가능한 부분이라 분리할
    가치가 있었다.

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
                offset_x, offset_y = tile_offset(
                    source_id, self.tiler_cols, self.resize_width, self.resize_height
                )
                # 이 소스가 합성 캔버스에서 차지하는 타일 사각형 — 선분 클리핑 경계로 쓴다.
                bounds = (offset_x, offset_y, offset_x + self.resize_width, offset_y + self.resize_height)
                display_meta = None

                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    if (
                        obj_meta.unique_component_id == self.forklift_gie_id
                        and obj_meta.class_id == self.class_id
                    ):
                        lx, ly = ground_contact_point(obj_meta)
                        local_point = (lx - offset_x, ly - offset_y)
                        ground_center = homography.to_ground([local_point])[0]

                        ring_local = ellipse_from_ground_circle(
                            homography, ground_center, self.radius_m, GROUND_CIRCLE_SEGMENTS
                        )
                        if ring_local:
                            ring_tile = [(x + offset_x, y + offset_y) for x, y in ring_local]
                            display_meta = _draw_ring(
                                batch_meta, frame_meta, display_meta, ring_tile, bounds
                            )

                    l_obj = l_obj.next

                if display_meta is not None:
                    pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
