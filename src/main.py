"""
3단계: 고정 config 기반 파이프라인 실행.
소스/해상도/fps는 config.py의 CONFIG에서만 온다 — 실행 인자로 받지 않는다.
element 조립은 sources.py / pipeline.py가 담당하고, 여기서는 실행(Gst.init, GLib 루프, bus)만 한다.

실행 (DeepStream 컨테이너 안에서):
    python3 src/main.py                     # http://<host>:8810 에서 영상 확인
    python3 src/main.py --fakesink          # 인코딩/웹 없이 소스 연결과 FPS만 확인
"""

import argparse
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from config import PROJECT_ROOT, get_config
from logger import get_logger
from pipeline import build_pipeline
from probes import add_detection_probe, add_fps_probe
from webview import WebView

logger = get_logger("main")


def bus_call(_bus, message, loop) -> bool:
    t = message.type
    if t == Gst.MessageType.EOS:
        logger.info("EOS")
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        logger.error("%s: %s", err, debug)
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        logger.warning("%s: %s", err, debug)
    return True


class FramePublisher:
    """appsink가 뽑은 JPEG를 뷰어 서버의 FrameBuffer로 넘긴다."""

    def __init__(self, frames):
        self.frames = frames

    def __call__(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        self.frames.put(buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK


class SigintHandler:
    def __init__(self, loop: GLib.MainLoop):
        self.loop = loop

    def __call__(self, *_args):
        logger.info("종료 신호 수신")
        self.loop.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="고정 config 기반 파이프라인 실행")
    parser.add_argument(
        "--fakesink", action="store_true", help="인코딩/웹 없이 소스 연결과 FPS만 확인"
    )
    args = parser.parse_args()

    cfg = get_config()

    Gst.init(None)
    pipeline, sink = build_pipeline(cfg, encode=not args.fakesink)

    webview = None
    add_fps_probe(sink.get_static_pad("sink"), label="tiled")

    pgie = pipeline.get_by_name("pgie")
    add_detection_probe(pgie.get_static_pad("src"), label="pgie-human")

    if not args.fakesink:
        webview = WebView(cfg["output"]["web"], PROJECT_ROOT / "public" / "index.html")
        sink.connect("new-sample", FramePublisher(webview.frames))

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    signal.signal(signal.SIGINT, SigintHandler(loop))

    if webview is not None:
        webview.start()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        if webview is not None:
            webview.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
