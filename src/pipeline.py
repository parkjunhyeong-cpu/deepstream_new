"""
파이프라인 element 조립 전담. 여기서는 element 생성/연결만 하고,
Gst.init / GLib 루프 / bus 처리 등 실행 관련 로직은 main.py가 담당한다.
"""

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

    logger.info(
        "streammux: %dx%d, batch-size=%d, batched-push-timeout=%dus",
        width, height, num_sources, batched_push_timeout,
    )
    return streammux


def _build_tiler(tiler_cfg: dict) -> Gst.Element:
    """격자 계산은 config._compute_derived()가 이미 끝냈으므로 그대로 꽂기만 한다."""
    tiler = Gst.ElementFactory.make("nvmultistreamtiler", "tiler")
    tiler.set_property("rows", tiler_cfg["rows"])
    tiler.set_property("columns", tiler_cfg["columns"])
    tiler.set_property("width", tiler_cfg["width"])
    tiler.set_property("height", tiler_cfg["height"])

    logger.info(
        "tiler: %dx%d 격자, 출력 %dx%d",
        tiler_cfg["rows"], tiler_cfg["columns"], tiler_cfg["width"], tiler_cfg["height"],
    )
    return tiler


def _link(src: Gst.Element, dst: Gst.Element) -> None:
    if not src.link(dst):
        raise RuntimeError(f"{src.get_name()} -> {dst.get_name()} 링크 실패")


def build_encode(pipeline: Gst.Pipeline, jpeg_quality: int = 75) -> tuple[Gst.Element, Gst.Element]:
    """nvvideoconvert -> capsfilter -> jpegenc 를 만들어 pipeline에 추가하고
    (체인 첫 element, 마지막 element)를 반환한다. sink 연결은 호출자 몫이다."""
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

    # nvjpegenc / jpegenc 둘 다 quality를 갖지만 버전에 따라 없을 수 있다.
    if jpegenc.find_property("quality") is not None:
        jpegenc.set_property("quality", jpeg_quality)
    else:
        logger.warning("%s에 quality 속성이 없어 기본값 사용", jpegenc.get_factory().get_name())

    for el in (conv, capsfilter, jpegenc):
        pipeline.add(el)
    _link(conv, capsfilter)
    _link(capsfilter, jpegenc)

    logger.info("encode: %s, quality=%d", jpegenc.get_factory().get_name(), jpeg_quality)
    return conv, jpegenc


def build_appsink(pipeline: Gst.Pipeline) -> Gst.Element:
    """최신 프레임만 유지하는 appsink. 소비자가 느려도 파이프라인이 밀리지 않는다."""
    appsink = Gst.ElementFactory.make("appsink", "sink")
    appsink.set_property("emit-signals", True)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)
    appsink.set_property("sync", False)

    pipeline.add(appsink)
    return appsink


def build_fakesink(pipeline: Gst.Pipeline) -> Gst.Element:
    """인코딩 없이 버리는 싱크. 소스 연결/FPS 확인용."""
    fakesink = Gst.ElementFactory.make("fakesink", "sink")
    fakesink.set_property("sync", False)

    pipeline.add(fakesink)
    return fakesink


def build_pipeline(cfg: dict, encode: bool = True) -> tuple[Gst.Pipeline, Gst.Element]:
    """source_bin*N -> streammux -> tiler -> (nvvideoconvert -> jpegenc -> appsink | fakesink).

    소스가 1개든 N개든 같은 경로를 탄다 — N=1이면 tiler가 1x1이 될 뿐이다.
    encode=False면 인코딩 없이 fakesink로 받아 소스 연결/FPS만 확인한다.
    (pipeline, 마지막 sink element) 반환.
    """
    inp = cfg["input"]
    resize = inp["resize"]
    pipeline = Gst.Pipeline.new("basic-pipeline")

    streammux = _build_streammux(
        inp["num_sources"], resize["width"], resize["height"], inp["batched_push_timeout"]
    )
    pipeline.add(streammux)

    for i, src in enumerate(inp["sources"]):
        source_bin = create_source_bin(i, src["url"], inp["reconnect_sec"], src["name"])
        pipeline.add(source_bin)
        sink_pad = streammux.request_pad_simple(f"sink_{i}")  # pad-added 오기 전에 미리 요청
        source_bin.connect("pad-added", on_pad_added, sink_pad, i)

    tiler = _build_tiler(cfg["pipeline"]["tiler"])
    pipeline.add(tiler)

    if encode:
        tail_head, encode_tail = build_encode(pipeline, cfg["output"]["web"]["jpeg_quality"])
        sink = build_appsink(pipeline)
        _link(encode_tail, sink)
    else:
        sink = build_fakesink(pipeline)
        tail_head = sink

    _link(streammux, tiler)
    _link(tiler, tail_head)

    return pipeline, sink
