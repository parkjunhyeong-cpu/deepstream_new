"""
control-api 서버(별도 컨테이너)에 붙는 gRPC 클라이언트.

시작 시 fetch_config()로 초기 설정을 한 번 받아오고, ConfigWatcher로 이후 변경을
구독한다 (WatchConfig, server-streaming RPC). 변경이 오면 main.py가 cold restart를
트리거한다 — 이 프로세스 안에서 파이프라인을 직접 다시 만들지 않는다.

pb/control_api_pb2*.py는 proto에서 생성되는 파일이라 저장소에 없다 — 먼저
    python3 -m grpc_tools.protoc -I proto --python_out=src/pb --grpc_python_out=src/pb proto/control_api.proto
로 생성해야 이 모듈이 import된다.
"""

import os
import threading

import grpc

from logger import get_logger
from pb import control_api_pb2, control_api_pb2_grpc

logger = get_logger(__name__)

HOST = os.environ.get("CONTROL_API_HOST", "localhost")
PORT = int(os.environ.get("CONTROL_API_PORT", "50051"))


def _proto_to_input(proto_input: control_api_pb2.InputConfig) -> dict:
    return {
        "sources": [
            {
                "name": src.name,
                "url": src.url,
                "sourceWidth": src.source_width,
                "sourceHeight": src.source_height,
            }
            for src in proto_input.sources
        ],
        "resize": {"width": proto_input.resize_width, "height": proto_input.resize_height},
        "framerate": {"target": proto_input.framerate_target},
        "reconnect_sec": proto_input.reconnect_sec,
    }


def fetch_config() -> dict:
    """시작 시 한 번 호출 — control-api에서 초기 input 설정을 받아온다."""
    with grpc.insecure_channel(f"{HOST}:{PORT}") as channel:
        stub = control_api_pb2_grpc.ControlApiStub(channel)
        response = stub.GetConfig(control_api_pb2.GetConfigRequest())
        logger.info(
            "control-api(%s:%d)에서 설정 수신: 소스 %d개", HOST, PORT, len(response.input.sources)
        )
        return _proto_to_input(response.input)


class ConfigWatcher:
    """백그라운드 스레드에서 WatchConfig 스트림을 구독하다가, 변경 오면 on_change(new_input)를 호출한다."""

    def __init__(self, on_change):
        self.on_change = on_change
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        channel = grpc.insecure_channel(f"{HOST}:{PORT}")
        stub = control_api_pb2_grpc.ControlApiStub(channel)
        try:
            for response in stub.WatchConfig(control_api_pb2.WatchConfigRequest()):
                logger.info("control-api 설정 변경 감지")
                self.on_change(_proto_to_input(response.input))
        except grpc.RpcError as exc:
            logger.error("WatchConfig 연결 끊김: %s", exc)
