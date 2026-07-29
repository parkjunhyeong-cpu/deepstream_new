import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

import pyds
from logger import get_logger

logger = get_logger(__name__)


class _ClassStat:
    """한 클래스(예: forklift, person)의 누적 검출 수와 신뢰도 범위."""

    def __init__(self):
        self.count = 0
        self.conf_min = None
        self.conf_max = None

    def add(self, conf: float) -> None:
        self.count += 1
        self.conf_min = conf if self.conf_min is None else min(self.conf_min, conf)
        self.conf_max = conf if self.conf_max is None else max(self.conf_max, conf)


class DetectionProbe:
    """nvinfer가 채운 NvDsBatchMeta를 읽어서 클래스별 검출 수/신뢰도를 주기적으로 로그.
    forklift/person을 obj_label 기준으로 따로 세서, 각 클래스가 실제로 잡히는지(특히 임계값을
    낮춘 person이 제대로 넘어오는지) 눈으로 박스를 세지 않고 숫자로 검증하기 위한 용도."""

    def __init__(self, label: str, interval_sec: float = 5.0):
        self.label = label
        self.interval_sec = interval_sec
        self.frame_count = 0
        self.stats: dict[str, _ClassStat] = {}  # obj_label -> 누적 통계
        self.start = time.monotonic()

    def _reset(self) -> None:
        self.frame_count = 0
        self.stats = {}
        self.start = time.monotonic()

    def _log(self) -> None:
        if not self.stats:
            logger.info("[%s] %d프레임, 검출 0개", self.label, self.frame_count)
            return
        for name, stat in sorted(self.stats.items()):
            per_frame = stat.count / self.frame_count if self.frame_count else 0.0
            conf_range = (
                f"{stat.conf_min:.2f}~{stat.conf_max:.2f}" if stat.conf_min is not None else "N/A"
            )
            logger.info(
                "[%s] %s: %d프레임 중 %d개 (프레임당 %.2f개), 신뢰도 %s",
                self.label, name, self.frame_count, stat.count, per_frame, conf_range,
            )

    def __call__(self, _pad, info):
        gst_buffer = info.get_buffer()
        if gst_buffer is None:
            return Gst.PadProbeReturn.OK

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
        l_frame = batch_meta.frame_meta_list

        while l_frame is not None:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            self.frame_count += 1

            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                # nvinfer가 labels 파일 기준으로 채워둔 라벨. 비어 있으면 class_id로 폴백.
                name = obj_meta.obj_label or f"class_{obj_meta.class_id}"
                stat = self.stats.get(name)
                if stat is None:
                    stat = self.stats[name] = _ClassStat()
                stat.add(obj_meta.confidence)
                l_obj = l_obj.next

            l_frame = l_frame.next

        elapsed = time.monotonic() - self.start
        if elapsed >= self.interval_sec:
            self._log()
            self._reset()

        return Gst.PadProbeReturn.OK


def add_detection_probe(pad: Gst.Pad, label: str, interval_sec: float = 5.0) -> DetectionProbe:
    probe = DetectionProbe(label, interval_sec)
    pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    return probe
