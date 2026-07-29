import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

logger = get_logger(__name__)

# NvDsDisplayMeta 하나가 담을 수 있는 원(circle) 최대 개수 — pyds/NvDsDisplayMeta 풀 제약.
# forklift가 이보다 많이 잡히는 프레임에서는 display_meta를 추가로 하나 더 뽑아 이어붙인다.
MAX_CIRCLES_PER_DISPLAY_META = 16


def _center(obj_meta) -> tuple[float, float]:
    rect = obj_meta.rect_params
    return rect.left + rect.width / 2, rect.top + rect.height / 2


class ZoneDrawProbe:
    """forklift(class_id) 중심에 고정 반경의 원을 그려 넣는다. 순수 시각화 담당 — "이 프레임에
    zone이 있다는 걸 화면에 어떻게 표현할지"만 안다. 침입 감지(ZoneIntrusionProbe)와는 바뀌는
    이유가 다르다(색상/반경 표현 방식 vs 안전 판정 로직)고 판단해 별도 probe로 분리했다.

    forklift와 person이 각각 별도 PGIE(둘 다 단일 클래스)라 obj_meta.class_id만으로는 구분이
    안 된다 — 둘 다 0이다. 그래서 obj_meta.unique_component_id(그 객체를 만든 PGIE의
    gie-unique-id)로 구분한다. forklift_gie_id는 절대 하드코딩하지 않고 pipeline.py에서
    pgie.get_property('unique-id')로 읽은 값을 그대로 받는다 (프로젝트 컨벤션).

    tracker 이후 좌표는 소스 프레임 기준이라, tiler가 타일 합성 캔버스 기준으로 좌표를 바꿔준
    뒤(=tiler src pad)에 붙여야 osd가 그리는 bbox와 같은 좌표계에서 원이 맞게 그려진다."""

    def __init__(self, class_id: int, radius_px: int, forklift_gie_id: int):
        self.class_id = class_id
        self.radius_px = radius_px
        self.forklift_gie_id = forklift_gie_id

    def __call__(self, _pad, info):
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list

        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            display_meta = None

            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                if (
                    obj_meta.unique_component_id == self.forklift_gie_id
                    and obj_meta.class_id == self.class_id
                ):
                    if display_meta is None or display_meta.num_circles >= MAX_CIRCLES_PER_DISPLAY_META:
                        if display_meta is not None:
                            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
                        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
                        display_meta.num_circles = 0

                    xc, yc = _center(obj_meta)
                    circle = display_meta.circle_params[display_meta.num_circles]
                    circle.xc = int(xc)
                    circle.yc = int(yc)
                    circle.radius = self.radius_px
                    circle.circle_color.set(1.0, 0.0, 0.0, 0.4)
                    circle.has_bg_color = 0
                    display_meta.num_circles += 1

                l_obj = l_obj.next

            if display_meta is not None:
                pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK


class ZoneIntrusionProbe:
    """person이 forklift 반경 안에 들어오면 경고 로그를 남긴다. 순수 감지/알림 담당 — 나중에
    로그가 webhook/카프카 알림 등으로 바뀌어도 ZoneDrawProbe(시각화)는 건드릴 필요가 없도록
    분리했다.

    forklift_gie_id/person_gie_id는 ZoneDrawProbe와 동일한 이유로 런타임에 읽은 값을 그대로
    받는다. 좌표 비교는 같은 frame_meta(=같은 소스) 안에서만 한다 — 타일 합성 캔버스에서 서로
    다른 소스의 좌표를 섞어 비교하면 안 되기 때문이다."""

    def __init__(self, class_id: int, radius_px: int, forklift_gie_id: int, person_gie_id: int):
        self.class_id = class_id
        self.radius_px = radius_px
        self.forklift_gie_id = forklift_gie_id
        self.person_gie_id = person_gie_id
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
            forklift_centers = []  # [(xc, yc), ...] — 이 프레임(소스)의 forklift 중심들
            persons = []  # [(object_id, xc, yc), ...] — 이 프레임(소스)의 person들

            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                if (
                    obj_meta.unique_component_id == self.forklift_gie_id
                    and obj_meta.class_id == self.class_id
                ):
                    forklift_centers.append(_center(obj_meta))
                elif obj_meta.unique_component_id == self.person_gie_id:
                    xc, yc = _center(obj_meta)
                    persons.append((obj_meta.object_id, xc, yc))
                l_obj = l_obj.next

            for object_id, px, py in persons:
                key = (source_id, object_id)
                inside_now = any(
                    math.hypot(px - fx, py - fy) <= self.radius_px for fx, fy in forklift_centers
                )
                if inside_now:
                    still_inside.add(key)
                    if key not in self._inside:
                        logger.warning(
                            "사람(track %d)이 forklift zone 안에 진입 (source=%d)", object_id, source_id
                        )

            l_frame = l_frame.next

        self._inside = still_inside
        return Gst.PadProbeReturn.OK


def add_zone_probes(
    pad: Gst.Pad, pgies: dict[str, Gst.Element], zone_cfg: dict
) -> tuple[ZoneDrawProbe, ZoneIntrusionProbe | None]:
    """forklift 원 그리기(ZoneDrawProbe)와 person 침입 감지(ZoneIntrusionProbe)를 같은 pad에
    독립된 두 probe로 붙인다. 서로 다른 이유로 바뀌는 두 관심사(시각화 vs 안전 로직)를 분리해서
    각자 독립적으로 확장 가능하게 하되, 호출부는 한 곳으로 유지한다.

    pgies({모델명: element}, build_pipeline이 만든 그대로)에서 forklift/person을 이름으로 찾아
    gie-unique-id를 읽는 지식도 여기 모아둔다 — main.py는 pgies를 그대로 넘기기만 하면 된다.
    person PGIE가 없으면(inference 맵에 없거나 비활성) 침입 감지 probe는 아예 붙이지 않는다
    (원 그리기는 계속 동작)."""
    forklift_pgie = pgies.get("forklift")
    if forklift_pgie is None:
        raise RuntimeError("zone은 forklift PGIE를 전제로 한다 — inference 맵에 forklift가 없다")
    forklift_gie_id = forklift_pgie.get_property("unique-id")

    draw_probe = ZoneDrawProbe(zone_cfg["class_id"], zone_cfg["radius_px"], forklift_gie_id)
    pad.add_probe(Gst.PadProbeType.BUFFER, draw_probe)
    logger.info(
        "zone draw probe 등록: forklift(class_id=%d, gie=%d), radius=%dpx",
        zone_cfg["class_id"], forklift_gie_id, zone_cfg["radius_px"],
    )

    person_pgie = pgies.get("person")
    if person_pgie is None:
        logger.info("person PGIE 없음 — zone intrusion probe는 등록하지 않음")
        return draw_probe, None

    person_gie_id = person_pgie.get_property("unique-id")
    intrusion_probe = ZoneIntrusionProbe(
        zone_cfg["class_id"], zone_cfg["radius_px"], forklift_gie_id, person_gie_id
    )
    pad.add_probe(Gst.PadProbeType.BUFFER, intrusion_probe)
    logger.info("zone intrusion probe 등록: person(gie=%d)", person_gie_id)

    return draw_probe, intrusion_probe
