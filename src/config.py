"""derived 계산 로직 + 경로 헬퍼.

실제 설정값(input/pipeline/output 전체)은 control-api가 유일한 출처다 — 여기엔 기본값을
두지 않는다. control-api가 준 dict을 compute_derived()로 처리해서 state.py가 들고 있는다.
"""

import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
