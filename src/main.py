"""
3단계: control-api(별도 컨테이너)에서 받은 설정으로 파이프라인 실행.

시작 시 control_api.fetch_config()로 초기 설정을 받고, ConfigWatcher로 이후 변경을
구독한다. 변경이 오면 파이프라인을 그 자리에서 고치지 않고 cold restart한다 —
GLib 루프를 끝내고 비정상 종료 코드로 죽어서, 외부(docker restart policy)가
다시 띄우면 그때 fetch_config()가 새 값을 받아온다. NVIDIA 플러그인이
set_state(NULL) 전환 시 종종 segfault 나는 문제(가이드 알려진 함정)를 이렇게 피한다.

실행 (DeepStream 컨테이너 안에서, control-api가 먼저 떠 있어야 함):
    python3 src/main.py                     # http://<host>:8810 에서 영상 확인
    python3 src/main.py --fakesink          # 인코딩/웹 없이 소스 연결과 FPS만 확인
    python3 src/main.py --no-control-api    # 임시: control-api 없이 DEFAULT_CONFIG로 실행
"""

import argparse
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import control_api
import state
from config import PROJECT_ROOT
from logger import get_logger
from pipeline import build_pipeline
from probes import add_detection_probe, add_fps_probe, add_zone_probe
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


class ConfigChangeHandler:
    """ConfigWatcher가 변경을 감지하면 호출된다 — state에 반영하고 재시작을 요청한다."""

    def __init__(self, loop: GLib.MainLoop):
        self.loop = loop

    def __call__(self, new_input: dict) -> None:
        state.apply_input_config(new_input)
        state.request_restart()
        self.loop.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="control-api 기반 파이프라인 실행")
    parser.add_argument(
        "--fakesink", action="store_true", help="인코딩/웹 없이 소스 연결과 FPS만 확인"
    )
    parser.add_argument(
        "--no-control-api",
        action="store_true",
        help="임시: control-api 없이 config.py의 DEFAULT_CONFIG로 바로 실행 (WatchConfig 구독도 안 함)",
    )
    args = parser.parse_args()

    if args.no_control_api:
        logger.warning("--no-control-api: control-api 안 붙고 DEFAULT_CONFIG로 실행")
    else:
        initial_input = control_api.fetch_config()
        state.apply_input_config(initial_input)
    cfg = state.get_config()

    Gst.init(None)
    pipeline, pgie, tracker, tiler, sink = build_pipeline(cfg, encode=not args.fakesink)

    webview = None
    add_fps_probe(sink.get_static_pad("sink"), label="tiled")
    # pgie 직후(트래커 전) vs tracker 직후(트래커 후) 검출 수를 비교하려고 둘 다 붙인다 —
    # 숫자가 다르면 트래커가 (예: probationAge 등으로) 일부를 걸러내고 있다는 뜻.
    add_detection_probe(pgie.get_static_pad("src"), label="pgie-forklift")
    add_detection_probe(tracker.get_static_pad("src"), label="tracker-forklift")

    add_zone_probe(tiler.get_static_pad("src"), cfg["pipeline"]["zone"])

    if not args.fakesink:
        webview = WebView(cfg["output"]["web"], PROJECT_ROOT / "public" / "index.html")
        sink.connect("new-sample", FramePublisher(webview.frames))

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    signal.signal(signal.SIGINT, SigintHandler(loop))

    if not args.no_control_api:
        watcher = control_api.ConfigWatcher(ConfigChangeHandler(loop))
        watcher.start()

    if webview is not None:
        webview.start()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        if webview is not None:
            webview.stop()

    if state.restart_requested():
        logger.info("설정 변경으로 cold restart — 비정상 종료 코드로 죽어서 외부가 재기동하게 함")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
