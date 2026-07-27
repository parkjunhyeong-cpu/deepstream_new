import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger

logger = get_logger(__name__)


def add_fps_probe(pad: Gst.Pad, label: str, interval_sec: float = 5.0) -> None:
    state = {"count": 0, "start": time.monotonic()}

    def _probe(_pad, _info):
        state["count"] += 1
        elapsed = time.monotonic() - state["start"]
        if elapsed >= interval_sec:
            fps = state["count"] / elapsed
            logger.info("[%s] %.1f fps (%d frames / %.1fs)", label, fps, state["count"], elapsed)
            state["count"] = 0
            state["start"] = time.monotonic()
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.BUFFER, _probe)
