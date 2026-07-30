import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger

logger = get_logger(__name__)

# nvurisrcbin(rtspsrc)의 내부 지터버퍼 지연(ms). pipeline.py의 streammux
# cache-buffer-timeout이 이 값보다 작으면, 이 지연 안에 못 들어온 프레임의 소스가
# 배치에서 통째로 빠질 수 있다 — 두 값이 따로 놀지 않게 여기서 상수로 export한다.
RTSP_LATENCY_MS = 200


def create_source_bin(index: int, uri: str, reconnect_sec: int = 10, label: str = "") -> Gst.Element:
    bin_name = f"source-bin-{index:02d}"
    source_bin = Gst.ElementFactory.make("nvurisrcbin", bin_name)
    if source_bin is None:
        raise RuntimeError(f"nvurisrcbin 생성 실패 ({bin_name}) — DeepStream 컨테이너 안에서 실행 중인지 확인")

    source_bin.set_property("uri", uri)
    source_bin.set_property("select-rtp-protocol", 4)  # TCP
    source_bin.set_property("latency", RTSP_LATENCY_MS)
    source_bin.set_property("rtsp-reconnect-interval", reconnect_sec)
    source_bin.set_property("rtsp-reconnect-attempts", -1)

    logger.info("source %d (%s): %s (reconnect %ds)", index, label or bin_name, uri, reconnect_sec)
    return source_bin


def on_pad_added(_source_bin: Gst.Element, new_pad: Gst.Pad, sink_pad: Gst.Pad, index: int) -> None:
    caps = new_pad.get_current_caps() or new_pad.query_caps(None)
    struct_name = caps.get_structure(0).get_name()

    if not struct_name.startswith("video/"):
        logger.debug("source %d: 비디오가 아닌 pad 무시 (%s)", index, struct_name)
        return

    if sink_pad.is_linked():
        logger.warning("source %d: sink pad가 이미 연결됨", index)
        return

    if new_pad.link(sink_pad) != Gst.PadLinkReturn.OK:
        logger.error("source %d: pad 연결 실패", index)
    else:
        logger.info("source %d: 연결됨 (%s)", index, struct_name)
