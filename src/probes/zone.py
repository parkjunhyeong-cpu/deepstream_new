"""forklift BEV zone의 그리기(zone_draw.ZoneDrawProbe)와 침입 감지(zone_intrusion.ZoneIntrusionProbe)를
같은 pad에 붙이는 조립 지점. 두 probe는 바뀌는 이유가 달라(표현 방식 vs 안전 판정 로직) 파일도
분리했고, 이 모듈은 pgies/cfg에서 gie-unique-id·호모그래피를 뽑아 둘에 나눠주는 역할만 한다."""

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger

from .util.homography import build_homographies
from .zone_draw import ZoneDrawProbe
from .zone_intrusion import ZoneIntrusionProbe

logger = get_logger(__name__)


def add_zone_probes(
    pad: Gst.Pad,
    pgies: dict[str, Gst.Element],
    zone_cfg: dict,
    sources_cfg: list[dict],
    tiler_cols: int,
    resize_width: int,
    resize_height: int,
) -> tuple[ZoneDrawProbe, ZoneIntrusionProbe | None]:
    """pgies에서 forklift/person의 gie-unique-id를 이름으로 찾고, sources_cfg(control-api의
    input.sources, 소스별 image_points/ground_points 포함)로 소스별 호모그래피를 한 번만
    계산해 두 probe에 공유한다. 캘리브레이션이 없는 소스는 자동으로 건너뛴다
    (build_homographies가 그 자리에 None을 넣어둠)."""
    forklift_pgie = pgies.get("forklift")
    if forklift_pgie is None:
        raise RuntimeError("zone은 forklift PGIE를 전제로 한다 — inference 맵에 forklift가 없다")
    forklift_gie_id = forklift_pgie.get_property("unique-id")

    homographies = build_homographies(sources_cfg)
    if all(h is None for h in homographies):
        logger.warning("모든 소스에 호모그래피 캘리브레이션이 없다 — zone probe가 아무것도 그리지 않는다")

    # source_id(정수) -> 채널명. sources_cfg 순서가 source_id와 대응한다는 전제는 homography와
    # 동일 (control-api의 input.sources 순서 = streammux sink_i 요청 순서) — 이 전제 자체가
    # 맞는지 확인하려고 draw/intrusion 둘 다 로그에 source_id 대신 이 이름을 찍게 한다.
    source_names = [src.get("name", f"source_{i}") for i, src in enumerate(sources_cfg)]

    draw_probe = ZoneDrawProbe(
        zone_cfg["class_id"], zone_cfg["radius_m"], forklift_gie_id, homographies,
        tiler_cols, resize_width, resize_height, source_names,
    )
    pad.add_probe(Gst.PadProbeType.BUFFER, draw_probe)
    logger.info(
        "zone draw probe 등록: forklift(class_id=%d, gie=%d), radius=%.2fm, 캘리브레이션된 소스=%d/%d",
        zone_cfg["class_id"], forklift_gie_id, zone_cfg["radius_m"],
        sum(1 for h in homographies if h is not None), len(homographies),
    )

    person_pgie = pgies.get("person")
    if person_pgie is None:
        logger.info("person PGIE 없음 — zone intrusion probe는 등록하지 않음")
        return draw_probe, None

    person_gie_id = person_pgie.get_property("unique-id")
    intrusion_probe = ZoneIntrusionProbe(
        zone_cfg["class_id"], zone_cfg["radius_m"], forklift_gie_id, person_gie_id, homographies,
        tiler_cols, resize_width, resize_height, source_names,
    )
    pad.add_probe(Gst.PadProbeType.BUFFER, intrusion_probe)
    logger.info("zone intrusion probe 등록: person(gie=%d)", person_gie_id)

    return draw_probe, intrusion_probe
