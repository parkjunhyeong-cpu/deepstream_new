"""
control-api 서버(별도 컨테이너)에 붙는 gRPC 클라이언트.

시작 시 fetch_config()로 초기 설정을 한 번 받아오고, ConfigWatcher로 이후 변경을
구독한다 (WatchConfig, server-streaming RPC). 변경이 오면 main.py가 cold restart를
트리거한다 — 이 프로세스 안에서 파이프라인을 직접 다시 만들지 않는다.

pb/control_api_pb2*.py는 proto에서 생성되는 파일이라 저장소에 없다 — 먼저
    python3 -m grpc_tools.protoc -I proto --python_out=src/pb --grpc_python_out=src/pb proto/control_api.proto
로 생성해야 이 모듈이 import된다.
"""

import json
import os
import threading

import grpc

from logger import get_logger
from pb import control_api_pb2, control_api_pb2_grpc

logger = get_logger(__name__)

HOST = os.environ.get("CONTROL_API_HOST", "localhost")
PORT = int(os.environ.get("CONTROL_API_PORT", "50051"))


def _proto_to_config(proto_cfg: control_api_pb2.Config) -> dict:
    """control-api는 input/pipeline/output 전체를 소유한다 — 그 shape 그대로 dict으로 변환한다.
    compute_derived()가 채우는 num_sources/tiler 등은 여기서 만들지 않는다
    (state.py가 이 dict을 받은 뒤에 계산)."""
    inp = proto_cfg.input
    pipeline = proto_cfg.pipeline
    web = proto_cfg.output.web

    return {
        "input": {
            "sources": [
                {
                    "name": src.name,
                    "url": src.url,
                    "sourceWidth": src.source_width,
                    "sourceHeight": src.source_height,
                    # BEV 호모그래피 캘리브레이션. 비어 있으면 zone probe가 이 소스를 건너뛴다
                    # (probes/homography.py의 build_homographies).
                    "image_points": [{"x": p.x, "y": p.y} for p in src.image_points],
                    "ground_points": [{"x": p.x, "y": p.y} for p in src.ground_points],
                }
                for src in inp.sources
            ],
            "resize": {"width": inp.resize.width, "height": inp.resize.height},
            "framerate": {"target": inp.framerate.target},
            "reconnect_sec": inp.reconnect_sec,
        },
        "pipeline": {
            "network_mode": pipeline.network_mode,
            "inference": {
                name: {
                    "enabled": model.enabled,
                    "config": model.config,
                    "infer_dim": model.infer_dim,
                    "labels": list(model.labels),
                }
                for name, model in pipeline.inference.items()
            },
            "tracker": {
                "config": pipeline.tracker.config,
                "width": pipeline.tracker.width,
                "height": pipeline.tracker.height,
            },
            "osd": {
                "display_bbox": pipeline.osd.display_bbox,
                "display_text": pipeline.osd.display_text,
                "display_fps": pipeline.osd.display_fps,
            },
            "zone": {
                "class_id": pipeline.zone.class_id,
                "radius_m": pipeline.zone.radius_m,
            },
        },
        "output": {
            "web": {
                "host": web.host,
                "port": web.port,
                "jpeg_quality": web.jpeg_quality,
                "max_fps": web.max_fps,
            },
        },
    }


def fetch_config() -> dict:
    """시작 시 한 번 호출 — control-api에서 전체 설정(input/pipeline/output)을 받아온다.
    최초 1회뿐이라 전체 내용을 로그로 남겨도 부담이 없다 — WatchConfig로 받는 이후 변경은
    (매번 로그로 남기기엔 너무 잦을 수 있어) 여기서는 남기지 않는다."""
    with grpc.insecure_channel(f"{HOST}:{PORT}") as channel:
        stub = control_api_pb2_grpc.ControlApiStub(channel)
        response = stub.GetConfig(control_api_pb2.GetConfigRequest())
        config = _proto_to_config(response.config)
        logger.info(
            "control-api(%s:%d)에서 설정 수신: 소스 %d개", HOST, PORT, len(config["input"]["sources"])
        )
        logger.info("최초 설정 전체:\n%s", json.dumps(config, ensure_ascii=False, indent=2))
        return config


class ConfigWatcher:
    """백그라운드 스레드에서 WatchConfig 스트림을 구독하다가, 변경 오면 on_change(new_config)를 호출한다."""

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
                self.on_change(_proto_to_config(response.config))
        except grpc.RpcError as exc:
            logger.error("WatchConfig 연결 끊김: %s", exc)
