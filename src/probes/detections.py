import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

logger = get_logger(__name__)


class DetectionProbe:
    """nvinfer가 채운 NvDsBatchMeta를 읽어서 프레임당 검출 수/신뢰도를 주기적으로 로그.
    화면에서 눈으로 박스를 세는 대신 숫자로 검출 여부를 검증하기 위한 용도."""

    def __init__(self, label: str, interval_sec: float = 5.0):
        self.label = label
        self.interval_sec = interval_sec
        self.frame_count = 0
        self.obj_count = 0
        self.conf_min = None
        self.conf_max = None
        self.start = time.monotonic()

    def __call__(self, _pad, info):
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        frame_meta_list = batch_meta.frame_meta_list

        while frame_meta_list is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(frame_meta_list.data)
            self.frame_count += 1

            obj_meta_list = frame_meta.obj_meta_list
            while obj_meta_list is not None:
                obj_meta = pyds.NvDsObjectMeta.cast(obj_meta_list.data)
                self.obj_count += 1
                conf = obj_meta.confidence
                self.conf_min = conf if self.conf_min is None else min(self.conf_min, conf)
                self.conf_max = conf if self.conf_max is None else max(self.conf_max, conf)
                obj_meta_list = obj_meta_list.next

            frame_meta_list = frame_meta_list.next

        elapsed = time.monotonic() - self.start
        if elapsed >= self.interval_sec:
            avg_per_frame = self.obj_count / self.frame_count if self.frame_count else 0.0
            conf_range = f"{self.conf_min:.2f}~{self.conf_max:.2f}" if self.conf_min is not None else "N/A"
            logger.info(
                "[%s] %d프레임 중 검출 %d개 (프레임당 %.2f개), 신뢰도 %s",
                self.label, self.frame_count, self.obj_count, avg_per_frame, conf_range,
            )
            self.frame_count = 0
            self.obj_count = 0
            self.conf_min = None
            self.conf_max = None
            self.start = time.monotonic()

        return Gst.PadProbeReturn.OK


def add_detection_probe(pad: Gst.Pad, label: str, interval_sec: float = 5.0) -> DetectionProbe:
    probe = DetectionProbe(label, interval_sec)
    pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    return probe
