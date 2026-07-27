"""HTTP 서버 + MJPEG 스트림 + 뷰어 페이지.

파이프라인의 appsink가 뽑은 JPEG를 FrameBuffer에 넣으면,
접속한 뷰어들에게 multipart/x-mixed-replace로 밀어준다.
Gst에 의존하지 않는다 — 프레임은 bytes로만 받는다.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from logger import get_logger

logger = get_logger(__name__)

BOUNDARY = "frame"


class FrameBuffer:
    """최신 JPEG 한 장만 들고 있는 버퍼. 늦은 뷰어는 중간 프레임을 건너뛴다."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0

    def put(self, data: bytes) -> None:
        with self._cond:
            self._frame = data
            self._seq += 1
            self._cond.notify_all()

    def get(self, last_seq: int, timeout: float = 5.0) -> tuple[int, bytes | None]:
        """last_seq 이후 새 프레임을 기다렸다 (seq, data) 반환. 타임아웃이면 (last_seq, None)."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            if self._seq == last_seq:
                return last_seq, None
            return self._seq, self._frame


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_page()
        elif path == "/stream":
            self._serve_stream()
        else:
            self.send_error(404)

    def _serve_page(self) -> None:
        try:
            body = self.server.index_path.read_bytes()
        except OSError as exc:
            logger.error("뷰어 페이지 읽기 실패: %s", exc)
            self.send_error(500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        frames = self.server.frames
        max_fps = self.server.max_fps
        min_interval = 1.0 / max_fps if max_fps else 0.0
        seq = 0

        logger.info("뷰어 접속: %s", self.address_string())
        try:
            while not self.server.stopping:
                started = time.monotonic()
                seq, data = frames.get(seq)
                if data is None:  # 아직 프레임 없음 / 소스 끊김 — 헤더는 유지한 채 계속 대기
                    continue

                self.wfile.write(
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode()
                )
                self.wfile.write(data)
                self.wfile.write(b"\r\n")

                remaining = min_interval - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        except (BrokenPipeError, ConnectionResetError):
            logger.info("뷰어 연결 종료: %s", self.address_string())

    def log_message(self, fmt: str, *args) -> None:
        """기본 stderr 액세스 로그를 우리 로거로 흡수 (프레임마다 찍히면 시끄럽다)."""
        logger.debug("%s - %s", self.address_string(), fmt % args)


class WebView(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, web_cfg: dict, index_path):
        super().__init__((web_cfg["host"], web_cfg["port"]), _Handler)
        self.frames = FrameBuffer()
        self.max_fps = web_cfg["max_fps"]
        self.index_path = index_path
        self.stopping = False
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        host, port = self.server_address[0], self.server_address[1]
        logger.info("뷰어 서버 시작: http://%s:%d", host, port)

    def stop(self) -> None:
        self.stopping = True
        self.shutdown()
        self.server_close()
        logger.info("뷰어 서버 종료")
