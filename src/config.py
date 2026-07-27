"""고정 설정.

3단계까지는 config.yaml 로딩 없이 이 파일의 CONFIG 하나만 쓴다.
스키마는 NEW_PIPELINE_GUIDE.md 3장과 동일하게 맞춰뒀으므로,
나중에 YAML을 받아야 하면 _load()만 교체하고 나머지 코드는 그대로 둔다.
"""

import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- 사용자가 건드리는 값 -------------------------------------------------

CONFIG = {
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


def _compute_derived(cfg: dict) -> dict:
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


CONFIG = _compute_derived(CONFIG)


# --- 접근 헬퍼 ------------------------------------------------------------


def get_config() -> dict:
    """파이프라인 전체가 공유하는 단일 설정 dict."""
    return CONFIG


def source_uris() -> list[str]:
    return [src["url"] for src in CONFIG["input"]["sources"]]


def resolve(rel_path: str) -> str:
    """PROJECT_ROOT 기준 상대경로를 절대경로 문자열로."""
    return str(PROJECT_ROOT / rel_path)
