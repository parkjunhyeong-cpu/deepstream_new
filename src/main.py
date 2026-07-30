"""
3단계: control-api(별도 컨테이너)에서 받은 설정으로 파이프라인 실행.

시작 시 control_api.fetch_config()로 초기 설정을 받고, ConfigWatcher로 이후 변경을
구독한다. 변경이 오면 파이프라인을 그 자리에서 고치지 않고 cold restart한다 —
GLib 루프를 끝내고 비정상 종료 코드로 죽어서, 외부(docker restart policy)가
다시 띄우면 그때 fetch_config()가 새 값을 받아온다. NVIDIA 플러그인이
set_state(NULL) 전환 시 종종 segfault 나는 문제(가이드 알려진 함정)를 이렇게 피한다.

실행 (DeepStream 컨테이너 안에서, control-api가 먼저 떠 있어야 함 — 설정의 유일한 출처라
로컬 기본값으로 대신할 방법이 없다):
    python3 src/main.py                     # http://<host>:8810 에서 영상 확인
    python3 src/main.py --fakesink          # 인코딩/웹 없이 소스 연결과 FPS만 확인
"""

import argparse
import os
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
from probes import attach_all_probes
from webview import WebView

logger = get_logger("main")

# NVIDIA 플러그인이 set_state(NULL) 전환 중 종종 멈추거나 segfault한다(가이드 알려진 함정) —
# SIGINT를 여러 번 줘도 이 블로킹 때문에 죽지 않는 사례가 있었다. get_state를 무기한 기다리지
# 않고 이 시간 안에 안 끝나면 강제 종료한다.
SHUTDOWN_TIMEOUT_SEC = 5
SHUTDOWN_TIMEOUT_NS = SHUTDOWN_TIMEOUT_SEC * Gst.SECOND


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

    def __call__(self, new_config: dict) -> None:
        state.apply_config(new_config)
        state.request_restart()
        self.loop.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="control-api 기반 파이프라인 실행")
    parser.add_argument(
        "--fakesink", action="store_true", help="인코딩/웹 없이 소스 연결과 FPS만 확인"
    )
    args = parser.parse_args()

    initial_config = control_api.fetch_config()
    state.apply_config(initial_config)
    cfg = state.get_config()

    Gst.init(None)
    pipeline, pgies, tracker, tiler, sink = build_pipeline(cfg, encode=not args.fakesink)

    webview = None
    attach_all_probes(pgies, tracker, tiler, sink, cfg)

    if not args.fakesink:
        webview = WebView(cfg["output"]["web"], PROJECT_ROOT / "public" / "index.html")
        sink.connect("new-sample", FramePublisher(webview.frames))

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    signal.signal(signal.SIGINT, SigintHandler(loop))

    watcher = control_api.ConfigWatcher(ConfigChangeHandler(loop))
    watcher.start()

    if webview is not None:
        webview.start()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        # get_state는 기본적으로 완료될 때까지 무기한 블로킹할 수 있다 — 타임아웃을 줘서 그 안에
        # 안 끝나면 정상 종료를 포기한다. sys.exit()/return은 인터프리터 정리 과정을 거치는데
        # NULL 전환이 멈춰 있으면 그 과정도 같이 멈추므로, os._exit()로 정리 없이 즉시 죽인다.
        ret, _, _ = pipeline.get_state(SHUTDOWN_TIMEOUT_NS)
        if ret != Gst.StateChangeReturn.SUCCESS:
            logger.error(
                "pipeline NULL 전환이 %d초 안에 끝나지 않음(ret=%s) — 강제 종료",
                SHUTDOWN_TIMEOUT_SEC, ret,
            )
            os._exit(1)
        if webview is not None:
            webview.stop()

    if state.restart_requested():
        logger.info("설정 변경으로 cold restart — 비정상 종료 코드로 죽어서 외부가 재기동하게 함")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
