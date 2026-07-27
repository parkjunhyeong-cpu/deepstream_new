"""
2~3단계: 소스 연결 / 인코딩 / 기본 파이프라인 확인용 임시 테스트 엔트리포인트.
element 조립은 sources.py / pipeline.py가 담당하고, 여기서는 실행(Gst.init, GLib 루프, bus)만 한다.

실행 (DeepStream 컨테이너 안에서):
    python3 src/main.py source rtsp://localhost:8554/stream0
    python3 src/main.py encode rtsp://localhost:8554/stream0 --out /tmp/preview.jpg
    python3 src/main.py pipeline rtsp://localhost:8554/stream0 --out /tmp/preview.jpg
"""

import argparse
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from logger import get_logger
from pipeline import build_encode_tail, build_pipeline
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


def build_source_pipeline(uri: str) -> Gst.Pipeline:
    pipeline = Gst.Pipeline.new("source-check")
    source_bin = create_source_bin(0, uri)
    fakesink = Gst.ElementFactory.make("fakesink", "sink")
    fakesink.set_property("sync", False)

    pipeline.add(source_bin)
    pipeline.add(fakesink)

    sink_pad = fakesink.get_static_pad("sink")
    add_fps_probe(sink_pad, label="source0")
    source_bin.connect("pad-added", on_pad_added, sink_pad, 0)

    logger.info("연결 시도: %s", uri)
    return pipeline


class PreviewSink:
    """appsink 새 프레임을 min_interval_sec에 한 번 out_path에 저장."""

    def __init__(self, out_path: str, min_interval_sec: float = 1.0):
        self.out_path = out_path
        self.min_interval_sec = min_interval_sec
        self.last_write = 0.0

    def __call__(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        now = time.monotonic()
        if now - self.last_write >= self.min_interval_sec:
            with open(self.out_path, "wb") as f:
                f.write(data)
            self.last_write = now
            logger.info("preview 저장: %s (%d bytes)", self.out_path, len(data))
        return Gst.FlowReturn.OK


class SigintHandler:
    def __init__(self, loop: GLib.MainLoop):
        self.loop = loop

    def __call__(self, *_args):
        logger.info("종료 신호 수신")
        self.loop.quit()


def _attach_preview_sink(appsink: Gst.Element, out_path: str, label: str) -> None:
    add_fps_probe(appsink.get_static_pad("sink"), label=label)
    appsink.connect("new-sample", PreviewSink(out_path))


def build_encode_pipeline(uri: str, out_path: str) -> Gst.Pipeline:
    """단일 소스 -> nvvideoconvert -> jpegenc -> appsink (streammux 없음)."""
    pipeline = Gst.Pipeline.new("encode-check")
    source_bin = create_source_bin(0, uri)
    pipeline.add(source_bin)

    conv, appsink = build_encode_tail(pipeline)
    source_bin.connect("pad-added", on_pad_added, conv.get_static_pad("sink"), 0)

    _attach_preview_sink(appsink, out_path, label="encoded")

    logger.info("인코딩 테스트 시작: %s -> %s", uri, out_path)
    return pipeline


def build_multi_pipeline(uris: list[str], out_path: str) -> Gst.Pipeline:
    """source_bin*N -> streammux -> tiler -> nvvideoconvert -> jpegenc -> appsink."""
    pipeline, appsink = build_pipeline(uris)
    _attach_preview_sink(appsink, out_path, label="tiled")

    logger.info("기본 파이프라인 테스트 시작: %s -> %s", uris, out_path)
    return pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="소스 연결 / 인코딩 확인 (임시 테스트 도구)")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_source = sub.add_parser("source", help="소스 연결 + fps 확인")
    p_source.add_argument("uri", help="RTSP/HTTP 소스 URI (예: rtsp://localhost:8554/stream0)")

    p_encode = sub.add_parser("encode", help="소스 -> nvjpegenc 인코딩 확인")
    p_encode.add_argument("uri", help="RTSP/HTTP 소스 URI")
    p_encode.add_argument("--out", default="/tmp/preview.jpg", help="저장할 미리보기 jpg 경로")

    p_pipeline = sub.add_parser("pipeline", help="streammux+tiler 포함 기본 파이프라인 확인")
    p_pipeline.add_argument("uris", nargs="+", help="RTSP/HTTP 소스 URI (여러 개 가능)")
    p_pipeline.add_argument("--out", default="/tmp/preview.jpg", help="저장할 미리보기 jpg 경로")

    args = parser.parse_args()

    Gst.init(None)
    if args.mode == "source":
        pipeline = build_source_pipeline(args.uri)
    elif args.mode == "encode":
        pipeline = build_encode_pipeline(args.uri, args.out)
    else:
        pipeline = build_multi_pipeline(args.uris, args.out)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    signal.signal(signal.SIGINT, SigintHandler(loop))

    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    return 0


if __name__ == "__main__":
    sys.exit(main())
