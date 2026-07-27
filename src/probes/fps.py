import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger

logger = get_logger(__name__)


class FpsProbe:
    def __init__(self, label: str, interval_sec: float = 5.0):
        self.label = label
        self.interval_sec = interval_sec
        self.count = 0
        self.start = time.monotonic()

    def __call__(self, _pad, _info):
        self.count += 1
        elapsed = time.monotonic() - self.start
        if elapsed >= self.interval_sec:
            fps = self.count / elapsed
            logger.info("[%s] %.1f fps (%d frames / %.1fs)", self.label, fps, self.count, elapsed)
            self.count = 0
            self.start = time.monotonic()
        return Gst.PadProbeReturn.OK


def add_fps_probe(pad: Gst.Pad, label: str, interval_sec: float = 5.0) -> FpsProbe:
    probe = FpsProbe(label, interval_sec)
    pad.add_probe(Gst.PadProbeType.BUFFER, probe)
    return probe
