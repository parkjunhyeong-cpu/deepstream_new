import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

from .homography import Homography

logger = get_logger(__name__)


def _ground_contact_point(obj_meta) -> tuple[float, float]:
    """bbox 하단 중앙 — 카메라가 내려다보는 각도에서 물체가 지면에 닿는 지점의 근사치.
    bbox 중심(centroid)을 쓰면 물체 높이만큼 지면에서 떠 있는 점이 되어 호모그래피 투영이
    어긋난다 (BEV/IPM에서 흔히 쓰는 표준 근사). zone_draw.py에도 같은 함수가 있다 —
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


class ZoneIntrusionProbe:
    """person의 지면 위치가 forklift 지면 위치에서 실측 반경(radius_m) 안이면 경고 로그를
    남긴다. 픽셀 거리 대신 호모그래피로 변환한 지면 좌표 거리를 비교하므로, 카메라에서 멀리
    있는 forklift든 가까이 있는 forklift든 동일한 실제 반경 기준으로 판정된다.

    순수 감지/알림 담당 — 나중에 로그가 webhook/카프카 알림 등으로 바뀌어도 그리기
    (zone_draw.ZoneDrawProbe)는 건드릴 필요가 없도록 파일까지 분리했다."""

    def __init__(
        self,
        class_id: int,
        radius_m: float,
        forklift_gie_id: int,
        person_gie_id: int,
        homographies: list[Homography | None],
        tiler_cols: int,
        resize_width: int,
        resize_height: int,
    ):
        self.class_id = class_id
        self.radius_m = radius_m
        self.forklift_gie_id = forklift_gie_id
        self.person_gie_id = person_gie_id
        self.homographies = homographies
        self.tiler_cols = tiler_cols
        self.resize_width = resize_width
        self.resize_height = resize_height
        # (source_id, tracker object_id) 중 지난 프레임까지 반경 안에 있던 사람들. 진입 "순간"에만
        # 로그를 남기기 위한 상태 — 매 프레임 다시 계산하면 서 있는 동안 로그가 계속 찍힌다.
        self._inside: set[tuple[int, int]] = set()

    def __call__(self, _pad, info):
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list
        still_inside: set[tuple[int, int]] = set()

        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            source_id = frame_meta.source_id
            homography = self.homographies[source_id] if source_id < len(self.homographies) else None

            if homography is not None:
                offset_x, offset_y = _tile_offset(
                    source_id, self.tiler_cols, self.resize_width, self.resize_height
                )
                forklift_local = []
                person_entries = []  # [(object_id, local_point), ...]

                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    px, py = _ground_contact_point(obj_meta)
                    local_point = (px - offset_x, py - offset_y)
                    if (
                        obj_meta.unique_component_id == self.forklift_gie_id
                        and obj_meta.class_id == self.class_id
                    ):
                        forklift_local.append(local_point)
                    elif obj_meta.unique_component_id == self.person_gie_id:
                        person_entries.append((obj_meta.object_id, local_point))
                    l_obj = l_obj.next

                if forklift_local and person_entries:
                    forklift_ground = homography.to_ground(forklift_local)
                    person_ground = homography.to_ground([p for _, p in person_entries])
                    for (object_id, _), (gx, gy) in zip(person_entries, person_ground):
                        key = (source_id, object_id)
                        inside_now = any(
                            math.hypot(gx - fx, gy - fy) <= self.radius_m for fx, fy in forklift_ground
                        )
                        if inside_now:
                            still_inside.add(key)
                            if key not in self._inside:
                                logger.warning(
                                    "사람(track %d)이 forklift zone 안에 진입 (source=%d)",
                                    object_id, source_id,
                                )

            l_frame = l_frame.next

        self._inside = still_inside
        return Gst.PadProbeReturn.OK
