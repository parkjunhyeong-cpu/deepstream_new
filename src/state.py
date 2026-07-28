"""
프로세스 전역 공유 가변 상태. control-api watch 스레드와 GLib 메인 스레드가
동시에 건드리는 값은 여기서만 관리한다.
"""

import copy
import threading

from config import DEFAULT_CONFIG, compute_derived
from logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_config = compute_derived(copy.deepcopy(DEFAULT_CONFIG))
_restart_requested = threading.Event()


def get_config() -> dict:
    """파이프라인 전체가 공유하는 단일 설정 dict."""
    with _lock:
        return _config


def apply_config(new_cfg: dict) -> dict:
    """control-api가 준 설정(input/pipeline/output 전체)으로 통째로 교체하고 derived 값을 다시
    계산한다. control-api가 전체 설정의 단일 출처라 부분 병합이 아니라 항상 전체 교체다."""
    global _config
    with _lock:
        _config = compute_derived(copy.deepcopy(new_cfg))
        logger.info("설정 반영: 소스 %d개", _config["input"]["num_sources"])
        return _config


def request_restart() -> None:
    """설정이 바뀌어서 cold restart가 필요하다고 표시한다. 실제 종료는 main.py가 한다."""
    _restart_requested.set()


def restart_requested() -> bool:
    return _restart_requested.is_set()
