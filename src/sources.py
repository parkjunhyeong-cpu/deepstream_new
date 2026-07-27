import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger

logger = get_logger(__name__)


def create_source_bin(index: int, uri: str, reconnect_sec: int = 10) -> Gst.Element:
    name = f"source-bin-{index:02d}"
    source_bin = Gst.ElementFactory.make("nvurisrcbin", name)
    if source_bin is None:
        raise RuntimeError(f"nvurisrcbin 생성 실패 ({name}) — DeepStream 컨테이너 안에서 실행 중인지 확인")

    source_bin.set_property("uri", uri)
    source_bin.set_property("select-rtp-protocol", 4)  # TCP
    source_bin.set_property("latency", 200)
    source_bin.set_property("rtsp-reconnect-interval", reconnect_sec)
    source_bin.set_property("rtsp-reconnect-attempts", 1)
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
