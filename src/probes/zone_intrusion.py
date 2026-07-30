import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

from .util.homography import Homography

logger = get_logger(__name__)


# zone_draw.py의 FOOT_POINT_BLEND와 반드시 같은 값이어야 한다 — 그리기와 침입 판정이 같은
# 지면 접점을 기준으로 삼아야 시각적으로 보이는 원과 실제 판정이 어긋나지 않는다.
FOOT_POINT_BLEND = 0.25


def _ground_contact_point(obj_meta) -> tuple[float, float]:
    """bbox 하단 중앙에서 top-center 쪽으로 FOOT_POINT_BLEND만큼 당긴 지점 — "cuboid 중심"의
    근사치. 자세한 이유는 zone_draw.py의 동일 함수 docstring 참고. zone_draw.py에도 같은
    함수가 있다 — 몇 줄짜리 순수 함수라 별도 공유 모듈을 두는 것보다 각자 갖는 쪽을 택했다."""
    rect = obj_meta.rect_params
    x = rect.left + rect.width / 2
    bottom_y = rect.top + rect.height
    y = bottom_y - FOOT_POINT_BLEND * rect.height
    return x, y


class ZoneIntrusionProbe:
    """person의 지면 위치가 forklift 지면 위치에서 실측 반경(radius_m) 안이면 경고 로그를
    남긴다. 픽셀 거리 대신 호모그래피로 변환한 지면 좌표 거리를 비교하므로, 카메라에서 멀리
    있는 forklift든 가까이 있는 forklift든 동일한 실제 반경 기준으로 판정된다.

    zone_draw.ZoneDrawProbe와 마찬가지로 tiler의 SINK pad(합성 전, tracker 직후)에 붙는다 —
    frame_meta가 소스별로 정상 분리돼 있고 obj_meta.rect_params가 이미 그 소스의 원본 리사이즈
    좌표라 타일 오프셋 계산이 필요 없다.

    순수 감지/알림 담당 — 나중에 로그가 webhook/카프카 알림 등으로 바뀌어도 그리기
    (zone_draw.ZoneDrawProbe)는 건드릴 필요가 없도록 파일까지 분리했다."""

    def __init__(
        self,
        class_id: int,
        radius_m: float,
        forklift_gie_id: int,
        person_gie_id: int,
        homographies: list[Homography | None],
        source_names: list[str],
    ):
        self.class_id = class_id
        self.radius_m = radius_m
        self.forklift_gie_id = forklift_gie_id
        self.person_gie_id = person_gie_id
        self.homographies = homographies
        # source_id(정수) -> 채널명(control-api의 input.sources[].name, 예: "ch00"). 로그에
        # 숫자 대신 실제 채널을 남기기 위한 것.
        self.source_names = source_names
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
                forklift_local = []
                person_entries = []  # [(object_id, local_point), ...]

                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    local_point = _ground_contact_point(obj_meta)
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
                                channel = (
                                    self.source_names[source_id]
                                    if source_id < len(self.source_names)
                                    else f"source_{source_id}"
                                )
                                logger.warning(
                                    "사람(track %d)이 forklift zone 안에 진입 (channel=%s, source=%d)",
                                    object_id, channel, source_id,
                                )

            l_frame = l_frame.next

        self._inside = still_inside
        return Gst.PadProbeReturn.OK
