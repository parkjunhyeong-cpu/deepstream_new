import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

logger = get_logger(__name__)

# NvDsDisplayMeta 하나가 담을 수 있는 원(circle) 최대 개수 — pyds/NvDsDisplayMeta 풀 제약.
# forklift가 이보다 많이 잡히는 프레임에서는 display_meta를 추가로 하나 더 뽑아 이어붙인다.
MAX_CIRCLES_PER_DISPLAY_META = 16


class ZoneProbe:
    """forklift(class_id) 중심에 고정 반경의 원을 그려 넣는다. tracker 이후 좌표는 소스 프레임
    기준이라, tiler가 타일 합성 캔버스 기준으로 좌표를 바꿔준 뒤(=tiler src pad)에 붙여야
    osd가 그리는 bbox와 같은 좌표계에서 원이 맞게 그려진다."""

    def __init__(self, class_id: int, radius_px: int):
        self.class_id = class_id
        self.radius_px = radius_px

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
                if obj_meta.class_id == self.class_id:
                    if display_meta is None or display_meta.num_circles >= MAX_CIRCLES_PER_DISPLAY_META:
                        if display_meta is not None:
                            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)
                        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
                        display_meta.num_circles = 0

                    rect = obj_meta.rect_params
                    circle = display_meta.circle_params[display_meta.num_circles]
                    circle.xc = int(rect.left + rect.width / 2)
                    circle.yc = int(rect.top + rect.height / 2)
                    circle.radius = self.radius_px
                    circle.circle_color.set(1.0, 0.0, 0.0, 0.4)
                    circle.has_bg_color = 0
                    display_meta.num_circles += 1

                l_obj = l_obj.next

            if display_meta is not None:
                pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK


def add_zone_probe(pad: Gst.Pad, zone_cfg: dict) -> ZoneProbe:
    probe = ZoneProbe(zone_cfg["class_id"], zone_cfg["radius_px"])
    pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    logger.info("zone probe 등록: class_id=%d, radius=%dpx", zone_cfg["class_id"], zone_cfg["radius_px"])
    return probe
