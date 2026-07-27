"""
파이프라인 element 조립 전담. 여기서는 element 생성/연결만 하고,
Gst.init / GLib 루프 / bus 처리 등 실행 관련 로직은 main.py가 담당한다.
"""

import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from logger import get_logger
from sources import create_source_bin, on_pad_added

logger = get_logger(__name__)


def _build_streammux(num_sources: int, width: int, height: int, batched_push_timeout: int) -> Gst.Element:
    streammux = Gst.ElementFactory.make("nvstreammux", "streammux")
    streammux.set_property("width", width)
    streammux.set_property("height", height)
    streammux.set_property("batch-size", num_sources)
    streammux.set_property("batched-push-timeout", batched_push_timeout)
    streammux.set_property("live-source", True)
    streammux.set_property("drop-pipeline-eos", True)  # 소스 1개 EOS로 전체 죽는 것 방지
    streammux.set_property("cache-buffer-timeout", batched_push_timeout * 2)
    return streammux


def _build_tiler(num_sources: int, tile_width: int, tile_height: int) -> Gst.Element:
    cols = math.ceil(math.sqrt(num_sources))
    rows = math.ceil(num_sources / cols)

    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
    tiler.set_property("rows", rows)
    tiler.set_property("columns", cols)
    tiler.set_property("width", tile_width * cols)
    tiler.set_property("height", tile_height * rows)
    return tiler


def build_encode_tail(pipeline: Gst.Pipeline) -> tuple[Gst.Element, Gst.Element]:
    """nvvideoconvert -> capsfilter -> jpegenc -> appsink 를 만들어 pipeline에 추가하고
    (체인 시작 element, appsink)를 반환한다. 호출자가 앞단을 conv에 링크하면 된다."""
    conv = Gst.ElementFactory.make("nvvideoconvert", "conv")

    jpegenc = Gst.ElementFactory.make("nvjpegenc", "jpegenc")
    if jpegenc is not None:
        caps = Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420")
    else:
        logger.warning("nvjpegenc 없음 — CPU jpegenc로 폴백")
        jpegenc = Gst.ElementFactory.make("jpegenc", "jpegenc")
        caps = Gst.Caps.from_string("video/x-raw, format=I420")

    capsfilter = Gst.ElementFactory.make("capsfilter", "jpeg-caps")
    capsfilter.set_property("caps", caps)

    appsink = Gst.ElementFactory.make("appsink", "sink")
    appsink.set_property("emit-signals", True)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)
    appsink.set_property("sync", False)

    for el in (conv, capsfilter, jpegenc, appsink):
        pipeline.add(el)

    if not conv.link(capsfilter):
        raise RuntimeError("conv -> capsfilter 링크 실패")
    if not capsfilter.link(jpegenc):
        raise RuntimeError("capsfilter -> jpegenc 링크 실패")
    if not jpegenc.link(appsink):
        raise RuntimeError("jpegenc -> appsink 링크 실패")

    return conv, appsink


def build_pipeline(
    uris: list[str],
    resize_width: int = 960,
    resize_height: int = 540,
    target_fps: int = 10,
) -> tuple[Gst.Pipeline, Gst.Element]:
    """source_bin*N -> streammux -> tiler -> nvvideoconvert -> jpegenc -> appsink.
    추론/트래커/OSD 없는 최소 체인. (pipeline, appsink) 반환."""
    pipeline = Gst.Pipeline.new("basic-pipeline")

    batched_push_timeout = 1_000_000 // target_fps
    streammux = _build_streammux(len(uris), resize_width, resize_height, batched_push_timeout)
    pipeline.add(streammux)

    for i, uri in enumerate(uris):
        source_bin = create_source_bin(i, uri)
        pipeline.add(source_bin)
        sink_pad = streammux.request_pad_simple(f"sink_{i}")  # pad-added 오기 전에 미리 요청
        source_bin.connect("pad-added", on_pad_added, sink_pad, i)

    tiler = _build_tiler(len(uris), resize_width, resize_height)
    pipeline.add(tiler)

    conv, appsink = build_encode_tail(pipeline)

    if not streammux.link(tiler):
        raise RuntimeError("streammux -> tiler 링크 실패")
    if not tiler.link(conv):
        raise RuntimeError("tiler -> conv 링크 실패")

    return pipeline, appsink
