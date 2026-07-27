"""
파이프라인 element 조립 전담. 여기서는 element 생성/연결만 하고,
Gst.init / GLib 루프 / bus 처리 등 실행 관련 로직은 main.py가 담당한다.
"""

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from config import resolve
from logger import get_logger
from sources import create_source_bin, on_pad_added

logger = get_logger(__name__)

# DeepStream 컨테이너에 이미 번들로 들어있는 트래커 라이브러리 — 프로젝트가 제공하는 게 아니다.
TRACKER_LIB_PATH = "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so"


def _make(factory_name: str, elem_name: str) -> Gst.Element:
    """생성 실패를 여기서 바로 잡아서 원인을 알 수 있는 에러로 죽인다.
    체크 없이 넘기면 몇 줄 뒤 set_property에서 훨씬 헷갈리는 AttributeError로 죽는다."""
    element = Gst.ElementFactory.make(factory_name, elem_name)
    if element is None:
        raise RuntimeError(
            f"{factory_name} 생성 실패 ({elem_name}) — 플러그인이 없거나 DeepStream 컨테이너 밖에서 실행 중인지 확인"
        )
    return element


def _build_streammux(num_sources: int, width: int, height: int, batched_push_timeout: int) -> Gst.Element:
    streammux = _make("nvstreammux", "streammux")
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
    tiler = _make("nvmultistreamtiler", "tiler")
    tiler.set_property("rows", tiler_cfg["rows"])
    tiler.set_property("columns", tiler_cfg["columns"])
    tiler.set_property("width", tiler_cfg["width"])
    tiler.set_property("height", tiler_cfg["height"])

    logger.info(
        "tiler: %dx%d 격자, 출력 %dx%d",
        tiler_cfg["rows"], tiler_cfg["columns"], tiler_cfg["width"], tiler_cfg["height"],
    )
    return tiler


def _build_pgie(model_cfg: dict, num_sources: int) -> Gst.Element:
    """1차 추론(PGIE). config-file-path를 세팅하는 순간 nvinfer가 그 txt를 즉시 파싱해서
    process-mode/네트워크 shape 등 내부 속성을 채운다. gie-unique-id는 절대 하드코딩하지 않고
    세팅 후 get_property로 다시 읽어서 검증한다 (가이드 4.4, 커밋 e2737dd가 고친 버그)."""
    pgie = _make("nvinfer", "pgie")
    pgie.set_property("config-file-path", resolve(model_cfg["config"]))
    pgie.set_property("batch-size", num_sources)  # nvdspreprocess 없이 바로 붙이므로 streammux와 맞춘다

    process_mode = pgie.get_property("process-mode")
    unique_id = pgie.get_property("unique-id")
    if process_mode != 1:
        logger.warning(
            "%s의 process-mode=%d — PGIE(1)가 아닙니다. config 파일을 확인하세요",
            model_cfg["config"], process_mode,
        )

    logger.info("pgie: %s (unique-id=%d)", model_cfg["config"], unique_id)
    return pgie


def _build_tracker(tracker_cfg: dict) -> Gst.Element:
    """PGIE가 찾은 박스들에 프레임을 넘나드는 고유 object-id를 붙인다. 좌표는 안 바꾸고
    identity만 유지시켜서 '같은 사람'을 프레임마다 이어서 볼 수 있게 한다 (예: 중복 카운트 방지).
    ll-lib-file은 DeepStream 번들 라이브러리, ll-config-file만 프로젝트가 제공한다."""
    tracker = _make("nvtracker", "tracker")
    tracker.set_property("tracker-width", tracker_cfg["width"])
    tracker.set_property("tracker-height", tracker_cfg["height"])
    tracker.set_property("gpu-id", 0)
    tracker.set_property("ll-lib-file", TRACKER_LIB_PATH)
    tracker.set_property("ll-config-file", resolve(tracker_cfg["config"]))

    logger.info("tracker: %s (%dx%d)", tracker_cfg["config"], tracker_cfg["width"], tracker_cfg["height"])
    return tracker


def _build_osd() -> Gst.Element:
    """추론 메타데이터(bbox/label)를 실제 프레임 픽셀에 그려 넣는다.
    nvinfer는 메타데이터만 채울 뿐 화면은 안 바꾼다 — nvdsosd가 그걸 읽어서 렌더링해야 보인다.
    tiler가 이미 좌표를 합성 캔버스 기준으로 바꿔주므로 osd는 tiler 뒤에 붙인다
    (레퍼런스 앱 deepstream-app과 동일한 순서, 가이드 4.1)."""
    return _make("nvdsosd", "osd")


def _link(src: Gst.Element, dst: Gst.Element) -> None:
    if not src.link(dst):
        raise RuntimeError(f"{src.get_name()} -> {dst.get_name()} 링크 실패")


def build_encode(pipeline: Gst.Pipeline, jpeg_quality: int = 75) -> tuple[Gst.Element, Gst.Element]:
    """nvvideoconvert -> capsfilter -> jpegenc 를 만들어 pipeline에 추가하고
    (체인 첫 element, 마지막 element)를 반환한다. sink 연결은 호출자 몫이다."""
    conv = _make("nvvideoconvert", "conv")

    # nvjpegenc는 있으면 쓰고 없으면 폴백하는 "탐색"이라 _make(실패 시 raise)를 안 쓴다.
    jpegenc = Gst.ElementFactory.make("nvjpegenc", "jpegenc")
    if jpegenc is not None:
        caps = Gst.Caps.from_string("video/x-raw(memory:NVMM), format=I420")
    else:
        logger.warning("nvjpegenc 없음 — CPU jpegenc로 폴백")
        jpegenc = _make("jpegenc", "jpegenc")  # 이것도 없으면 진짜 에러
        caps = Gst.Caps.from_string("video/x-raw, format=I420")

    capsfilter = _make("capsfilter", "jpeg-caps")
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
    appsink = _make("appsink", "sink")
    appsink.set_property("emit-signals", True)
    appsink.set_property("max-buffers", 1)
    appsink.set_property("drop", True)
    appsink.set_property("sync", False)

    pipeline.add(appsink)
    return appsink


def build_fakesink(pipeline: Gst.Pipeline) -> Gst.Element:
    """인코딩 없이 버리는 싱크. 소스 연결/FPS 확인용."""
    fakesink = _make("fakesink", "sink")
    fakesink.set_property("sync", False)

    pipeline.add(fakesink)
    return fakesink


def build_pipeline(cfg: dict, encode: bool = True) -> tuple[Gst.Pipeline, Gst.Element, Gst.Element]:
    """source_bin*N -> streammux -> pgie(human) -> tracker -> tiler -> osd
    -> (nvvideoconvert -> jpegenc -> appsink | fakesink).

    SGIE(얼굴)는 안 하기로 함.

    소스가 1개든 N개든 같은 경로를 탄다 — N=1이면 tiler가 1x1이 될 뿐이다.
    encode=False면 인코딩 없이 fakesink로 받아 소스 연결/FPS만 확인한다.
    (pipeline, pgie, 마지막 sink element) 반환 — 이미 만든 element를 그대로 넘겨주는 것뿐이라
    호출자가 이름으로 다시 찾을(get_by_name) 필요가 없다.
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

    pgie = _build_pgie(cfg["pipeline"]["inference"]["human"], inp["num_sources"])
    pipeline.add(pgie)

    tracker = _build_tracker(cfg["pipeline"]["tracker"])
    pipeline.add(tracker)

    tiler = _build_tiler(cfg["pipeline"]["tiler"])
    pipeline.add(tiler)

    osd = _build_osd()
    pipeline.add(osd)

    if encode:
        tail_head, encode_tail = build_encode(pipeline, cfg["output"]["web"]["jpeg_quality"])
        sink = build_appsink(pipeline)
        _link(encode_tail, sink)
    else:
        sink = build_fakesink(pipeline)
        tail_head = sink

    _link(streammux, pgie)
    _link(pgie, tracker)
    _link(tracker, tiler)
    _link(tiler, osd)
    _link(osd, tail_head)

    return pipeline, pgie, sink
