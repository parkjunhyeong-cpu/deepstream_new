"""기본 설정값 + derived 계산 로직.

이 파일은 "처음 값이 뭔지"와 "derived 값을 어떻게 계산하는지"만 안다.
실행 중 바뀌는 실제 설정값(gRPC SetConfig로 갱신되는 것 포함)은 state.py가 갖고 있다 —
여러 스레드가 건드리는 상태라 CLAUDE.md 규칙대로 한 곳에 모았다.
"""

import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- 시작 시 기본값 (state.py가 이 값을 복사해서 실제 상태로 씀) ----------

DEFAULT_CONFIG = {
    "input": {
        "sources": [
            {
                "name": "ch00",
                "url": "rtsp://localhost:8554/stream0",
                "sourceWidth": 1280,
                "sourceHeight": 720,
            },
        ],
        "resize": {  # streammux 출력 해상도 (채널당)
            "width": 960,
            "height": 540,
        },
        "framerate": {
            "target": 20,  # 소스 실측값과 맞춘다 (batched_push_timeout이 여기서 유도됨)
        },
        "reconnect_sec": 10,
    },
    "pipeline": {
        "network_mode": 2,  # 0=FP32, 1=INT8, 2=FP16
        "inference": {
            "forklift": {
                "enabled": True,
                "config": "configs/pgie_forklift.txt",  # PROJECT_ROOT 기준, resolve()로 절대경로화
                "infer_dim": 640,
                "labels": ["forklift"],
            },
        },
        "tracker": {
            "config": "configs/tracker.yml",  # 알고리즘 설정만 우리가 제공, .so는 DeepStream 번들 사용
            "width": 640,
            "height": 384,
        },
        "osd": {
            "display_bbox": True,
            "display_text": True,
            "display_fps": True,
        },
    },
    "output": {
        "web": {
            "host": "0.0.0.0",
            "port": 8810,
            "jpeg_quality": 75,
            "max_fps": 15,
        },
    },
}


# --- 계산으로 채우는 값 (직접 쓰지 않는다) --------------------------------


def compute_derived(cfg: dict) -> dict:
    inp = cfg["input"]
    resize = inp["resize"]

    num_sources = len(inp["sources"])
    if num_sources < 1:
        raise ValueError("input.sources가 비어 있다")

    inp["num_sources"] = num_sources
    # streammux가 배치를 강제로 밀어내는 주기(us). 프레임 간격과 맞춘다.
    inp["batched_push_timeout"] = 1_000_000 // inp["framerate"]["target"]

    cols = math.ceil(math.sqrt(num_sources))
    rows = math.ceil(num_sources / cols)
    cfg["pipeline"]["tiler"] = {
        "rows": rows,
        "columns": cols,
        "width": resize["width"] * cols,
        "height": resize["height"] * rows,
    }

    return cfg


# --- 접근 헬퍼 ------------------------------------------------------------


def resolve(rel_path: str) -> str:
    """PROJECT_ROOT 기준 상대경로를 절대경로 문자열로."""
    return str(PROJECT_ROOT / rel_path)
