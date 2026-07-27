"""
2단계: 송출 서버 연결 확인용 임시 엔트리포인트.

source_bin -> fakesink 로만 구성해 RTSP/HTTP 소스에 실제로 연결되고
프레임이 흘러오는지(FPS 로그) 확인한다. 3단계에서 streammux/tiler/webview로 교체 예정.

실행 (DeepStream 컨테이너 안에서):
    python3 src/main.py rtsp://localhost:8554/stream0
"""

import argparse
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from logger import get_logger
from probes import add_fps_probe
from sources import create_source_bin, on_pad_added

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


def check_source(uri: str) -> int:
    Gst.init(None)

    pipeline = Gst.Pipeline.new("source-check")
    source_bin = create_source_bin(0, uri)
    fakesink = Gst.ElementFactory.make("fakesink", "sink")
    fakesink.set_property("sync", False)

    pipeline.add(source_bin)
    pipeline.add(fakesink)

    sink_pad = fakesink.get_static_pad("sink")
    add_fps_probe(sink_pad, label="source0")
    source_bin.connect("pad-added", on_pad_added, sink_pad, 0)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    def handle_sigint(*_args):
        logger.info("종료 신호 수신")
        loop.quit()

    signal.signal(signal.SIGINT, handle_sigint)

    logger.info("연결 시도: %s", uri)
    pipeline.set_state(Gst.State.PLAYING)

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="송출 서버 연결 확인")
    parser.add_argument("uri", help="RTSP/HTTP 소스 URI (예: rtsp://localhost:8554/stream0)")
    args = parser.parse_args()
    return check_source(args.uri)


if __name__ == "__main__":
    sys.exit(main())
