"""파이프라인이 빌드된 뒤 필요한 프로브를 전부 붙이는 단일 진입점.

main.py는 어떤 프로브가 어느 pad에 왜 붙는지 몰라도 된다 — build_pipeline()이 돌려준
element들과 cfg만 넘기면 이 함수가 알아서 배선한다. main.py는 lifecycle(빌드 → 루프 →
bus → 종료)만 신경 쓰면 된다."""

from .detections import add_detection_probe
from .fps import add_fps_probe
from .zone import add_zone_probes


def attach_all_probes(pgies: dict, tracker, tiler, sink, cfg: dict) -> None:
    add_fps_probe(sink.get_static_pad("sink"), label="tiled")

    # PGIE 체인 끝(트래커 전) vs tracker 직후(트래커 후) 검출 수를 비교하려고 둘 다 붙인다 —
    # 숫자가 다르면 트래커가 (예: probationAge 등으로) 일부를 걸러내고 있다는 뜻.
    # 체인 마지막 PGIE의 src pad엔 forklift+person 검출이 모두 모여 있고, 프로브가 클래스별로
    # 나눠 로그하므로 사람 임계값 완화 효과도 여기서 확인된다.
    last_pgie = list(pgies.values())[-1]
    add_detection_probe(last_pgie.get_static_pad("src"), label="pgie")
    add_detection_probe(tracker.get_static_pad("src"), label="tracker")

    # 그리기(ZoneDrawProbe)와 침입 감지(ZoneIntrusionProbe)는 관심사가 달라 내부적으로 별도
    # probe로 분리되어 있지만, 같은 pad에 같이 붙이는 호출부는 하나로 유지된다.
    # tiler_cols/resize_width/resize_height는 BEV 호모그래피가 합성 캔버스 좌표를 소스별 로컬
    # 좌표로 되돌리는 데 필요하다 (zone.py의 _tile_offset 참고).
    add_zone_probes(
        tiler.get_static_pad("src"),
        pgies,
        cfg["pipeline"]["zone"],
        cfg["input"]["sources"],
        cfg["pipeline"]["tiler"]["columns"],
        cfg["input"]["resize"]["width"],
        cfg["input"]["resize"]["height"],
    )
