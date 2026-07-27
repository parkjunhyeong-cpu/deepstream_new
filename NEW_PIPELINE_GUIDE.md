# DeepStream 파이프라인 재구현 가이드 (신규 대화 붙여넣기용)

> 이 문서는 기존 `SOLUTION_DeepStream` 리포에서 **파이프라인 코어만 남긴 최소 서버**를 새로 구현하기 위한 작업 지시서다.
> 새 대화에 이 문서를 통째로 붙여넣고 시작할 것.

---

## 0. 목표

기존 리포(`/home/serdic-web-server/Projects/SOLUTION_DeepStream`)의 `src/main.py`(3,300줄)는
control-api gRPC 설정 수신 + 15종 커스텀 플러그인 + SHM IPC + 벤치마크 + 스마트 레코딩이
한 파일에 뒤섞여 있다. 이걸 **읽고 굴릴 수 있는 파이프라인 서버**로 재구성한다.

변경 3원칙:

| 항목 | 기존 | 신규 |
|---|---|---|
| 설정 소스 | control-api gRPC 스트림 (`grpc_client.py` → `on_config` 콜백) | **로컬 YAML 파일 고정 로드** |
| 출력 | `appsink` → `/dev/shm` + UDS → 별도 stream-server(Node.js)가 웹 송출 | **파이썬 프로세스 내장 HTTP 서버가 직접 영상 페이지 제공** |
| 벤치마크 | `_run_benchmark_mode()`, `_batch_stats_thread()`, trtexec 레이턴시 측정, 배치 히스토그램 | **전부 삭제** |

핫 리로드, 재시작(`os.execv`), 웹훅, 모델 레지스트리, 녹화, BEV/rPPG/process 플러그인도 **1차 범위에서 제외**한다.
목표는 "RTSP N채널 → 추론 → 타일 → OSD → 브라우저에서 보인다"까지.

---

## 1. 실행 환경 (변경 없음)

- 베이스 이미지: `nvcr.io/nvidia/deepstream:8.0-triton-multiarch`
- Python 바인딩: `pyds` (DS 8.0 python apps, `PYTHONPATH`에 이미 등록됨 — `Dockerfile` 참고)
- GStreamer: PyGObject (`gi`), `Gst`, `GLib`
- 커스텀 파서 `.so`:
  - YOLO: `plugins/yolo-custom/nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so`
  - RT-DETR: `plugins/rt-detr/libnvdsinfer_custom_impl_rtdetr.so`
  - **이건 반드시 유지**해야 nvinfer가 detection 출력을 파싱한다. `configs/pgie_*.txt`의
    `custom-lib-path` / `parse-bbox-func-name`가 이걸 가리킨다.
- 트래커 라이브러리: `/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so` + `configs/tracker.yml`

---

## 2. 신규 디렉토리 구조

```
src2/                       # 기존 src/ 는 손대지 말고 병렬로 새로 만든다
  main.py                   # 엔트리포인트: config 로드 → 엔진 빌드 → 파이프라인 → GLib 루프
  config.py                 # YAML 로드 + derived 값 계산 (기존 config.py의 _compute_derived 축소판)
  pipeline.py               # create_pipeline(cfg) — GStreamer 엘리먼트 조립만
  engine.py                 # trtexec ONNX→TensorRT 엔진 빌드 (기존 _build_one_engine 이식)
  preprocess.py             # nvdspreprocess config 동적 생성 (기존 _generate_preprocess_configs 이식)
  sources.py                # create_source_bin / black source / pad-added 핸들러
  webview.py                # ★ 신규: HTTP 서버 + MJPEG 스트림 + 뷰어 페이지
  probes.py                 # fps 프로브, 소스 alive 프로브 (최소한만)
  logger.py                 # 기존 src/logger.py 그대로 복사
config.yaml                 # ★ 신규: 고정 설정 파일
```

각 파일 **300줄 이내**를 목표로 한다. 넘으면 분리.

---

## 3. `config.yaml` 스키마 (고정 설정)

기존 control-api가 gRPC로 msgpack 전송하던 dict를 그대로 YAML로 옮긴 형태.
`src/config.py`의 `_compute_derived()`가 기대하는 키 구조를 따른다.

```yaml
input:
  sources:
    - name: ch00
      url: rtsp://192.168.0.11:554/stream1
      sourceWidth: 1920
      sourceHeight: 1080
      human: true          # 모델 이름을 키로 하는 per-source on/off (아래 4.3 참고)
    - name: ch01
      url: rtsp://192.168.0.12:554/stream1
      sourceWidth: 1920
      sourceHeight: 1080
      human: true
  resize:                  # streammux 출력 해상도 (채널당)
    width: 960
    height: 540
  framerate:
    target: 15
  reconnect_sec: 10

pipeline:
  network_mode: 2          # 0=FP32, 1=INT8, 2=FP16
  inference:
    human:
      enabled: true
      config: configs/pgie_human.txt     # PROJECT_ROOT 기준 상대경로
      infer_dim: 640
      batch_mode: dynamic               # dynamic | fixed
      opt_fraction: "2/3"
      labels: [person]
    pose:
      enabled: false
      config: configs/sgie_pose.txt
      infer_dim: 256
      interval: 0
  tracker:
    enabled: true
  osd:
    display_bbox: true
    display_text: true
    display_fps: true

output:
  mode: web                # ★ 신규 모드 (기존 rtsp | shm 대체)
  web:
    host: 0.0.0.0
    port: 8810
    jpeg_quality: 75
    max_fps: 15
```

**derived(계산으로 채우는) 값** — 사용자가 쓰지 않는다:
- `input.num_sources` = len(sources)
- `input.batched_push_timeout` = `1_000_000 // framerate.target`
- `pipeline.tiler.{rows,columns,width,height}` = `cols=ceil(sqrt(N))`, `rows=ceil(N/cols)`, `width=resize.width*cols`
- 각 모델의 `engine_path`, `onnx_path`, `max_batch`, `_process_mode`, `_active_source_indices`, `_gie_unique_id`

---

## 4. 파이프라인 구조

### 4.1 전체 체인

```
[source_bin_0] ─┐
[source_bin_1] ─┼→ nvstreammux ─→ (nvdspreprocess → nvinfer)×PGIE
[source_bin_N] ─┘                     ↓
                                   nvtracker
                                      ↓
                                   nvinfer ×SGIE
                                      ↓
                             nvmultistreamtiler
                                      ↓
                                   nvdsosd
                                      ↓
                                nvvideoconvert
                                      ↓
                          [출력부 — 아래 5장]
```

기존 `create_pipeline()`(`src/main.py:1466`)에서 analytics / bev / feature_tracking / process /
vital(rPPG) `nvdsvideotemplate` 노드를 **전부 빼고** 위 체인만 남긴다.
`ctypes.CDLL` 심볼 바인딩 블록(`src/main.py:1824-2030`, 약 200줄)도 전부 삭제 대상.

### 4.2 nvstreammux 설정 (그대로 유지 — 실서비스에서 검증된 값)

```python
streammux.set_property("width",  resize["width"])
streammux.set_property("height", resize["height"])
streammux.set_property("batch-size", num_sources)
streammux.set_property("batched-push-timeout", inp["batched_push_timeout"])
streammux.set_property("live-source", True)
streammux.set_property("drop-pipeline-eos", True)       # 소스 1개 EOS로 전체 죽는 것 방지
streammux.set_property("cache-buffer-timeout", inp["batched_push_timeout"] * 2)
```

### 4.3 PGIE / SGIE 구분

`configs/*.txt`의 `[property] process-mode` 값으로 판정한다 (`config.py:_parse_nvinfer_props`).
- `process-mode=1` → **PGIE**: `nvdspreprocess` + `input-tensor-meta=True` 조합.
  `_active_source_indices`(해당 모델이 켜진 소스 인덱스 목록)로 `max_batch`가 결정되고,
  preprocess config의 `src-ids`에 그 인덱스가 들어간다. 소스별로 모델을 껐다 켤 수 있는 핵심 메커니즘.
- `process-mode=2` → **SGIE**: `interval` 속성만 세팅하고 PGIE/tracker 뒤에 붙인다.

**주의**: PGIE가 nvdspreprocess를 쓰면 nvinfer의 `batch-size`는 preprocess의 그룹 배치와 맞아야 한다.
그리고 nvdspreprocess config의 `tensor-name`은 **ONNX 실제 입력 텐서 이름**이어야 한다 —
기존 `_get_onnx_input_name()`(onnx 파일 파싱) + `_update_preprocess_tensor_name()` 로직을 반드시 이식할 것.
이거 틀리면 nvinfer가 조용히 추론을 건너뛴다.

### 4.4 GIE unique-id 하드코딩 금지

최근 커밋 `e2737dd`가 고친 버그다. `gie.get_property("unique-id")`로 **런타임에 읽어서**
`gie_cfg["_gie_unique_id"]`에 저장하고, 이후 라벨↔class_id 매핑에 그 값을 쓴다.
config 파일의 `gie-unique-id`를 코드에 상수로 박지 말 것.

### 4.5 소스 bin

`src/main.py:968 create_source_bin()`을 거의 그대로 이식:
- RTSP/파일 → `nvurisrcbin` + `select-rtp-protocol=4`(TCP), `latency=200`,
  `rtsp-reconnect-interval`, `rtsp-reconnect-attempts=1`
- HTTP MJPEG → `souphttpsrc → multipartdemux → nvjpegdec → nvvideoconvert → NVMM NV12` 수동 bin
- `smart-record` 관련 속성(`smart-rec-*`, `sr-done` 시그널)은 **삭제**

링크 순서 주의 (기존 코드 그대로):
```python
sinkpad = streammux.request_pad_simple(f"sink_{i}")   # pad를 먼저 요청해둬야
source_bin.connect("pad-added", _on_urisrcbin_pad_added, streammux, i)
static_src = source_bin.get_static_pad("src")          # MJPEG bin은 정적 ghost pad라
if static_src is not None:                             # pad-added가 안 와서 수동 링크 필요
    _on_urisrcbin_pad_added(source_bin, static_src, streammux, i)
```

### 4.6 타일러 / OSD

```python
tiler.set_property("rows", cfg["pipeline"]["tiler"]["rows"])
tiler.set_property("columns", ...)
tiler.set_property("width",  resize.width * cols)
tiler.set_property("height", resize.height * rows)

osd = Gst.ElementFactory.make("nvdsosd", "osd")   # display_bbox/display_text 중 하나라도 켜졌을 때만 생성
```

---

## 5. 출력부 — 웹 페이지 송출 (신규 구현 핵심)

### 5.1 권장 방식: MJPEG over HTTP (1차 구현)

가장 적은 코드로 브라우저에서 바로 보인다. JS 라이브러리 불필요, 지연 200ms 내외.

```
nvvideoconvert → capsfilter(video/x-raw,format=I420) → nvjpegenc → appsink
                                                                     ↓
                                              on_new_sample: JPEG 바이트를 전역 버퍼에 저장
                                                                     ↓
                                        HTTP GET /stream → multipart/x-mixed-replace 로 반복 전송
```

구현 포인트:
- `appsink`: `emit-signals=True`, `max-buffers=1`, `drop=True`, `sync=False`
  (기존 `src/main.py:1802` 설정과 동일 — 최신 프레임만 유지, 소비자가 느려도 파이프라인이 안 밀린다)
- `on_new_sample` 콜백에서 `gst_buffer.extract_dup()` 로 바이트를 뜬 뒤 `threading.Lock` 하에 전역 슬롯 교체.
  **콜백 안에서 소켓 write 하지 말 것** — 스트리밍 스레드가 블록되면 GStreamer 스트리밍 스레드가 멈춘다.
- HTTP 서버: 표준 라이브러리 `http.server.ThreadingHTTPServer` 또는 FastAPI+uvicorn.
  의존성 추가를 피하려면 `ThreadingHTTPServer`로 충분하다. 별도 daemon 스레드에서 `serve_forever()`.
- 응답 헤더:
  ```
  Content-Type: multipart/x-mixed-replace; boundary=frame
  ```
  프레임마다 `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<bytes>\r\n`
- `max_fps`로 송출 속도를 제한하고, 새 프레임이 없으면 재전송하지 말고 대기 (`threading.Condition`).

### 5.2 뷰어 페이지

`GET /` 에 인라인 HTML 한 장:
```html
<img src="/stream">
```
타일러가 이미 N채널을 한 장으로 합쳐주므로 `<img>` 하나면 된다.
채널별 개별 스트림이 필요해지면 `nvstreamdemux`를 붙여 소스별 appsink를 만드는 걸로 확장.

### 5.3 대안 (필요해지면)

| 방식 | 지연 | 난이도 | 비고 |
|---|---|---|---|
| MJPEG (권장) | ~0.2s | 낮음 | 대역폭 큼, 오디오 불가 |
| HLS (`hlssink2`) | 3~8s | 중간 | h264 인코더 재사용, hls.js 필요 |
| WebRTC (`webrtcbin`) | ~0.1s | 높음 | 시그널링 서버 직접 구현 필요 |
| RTSP (`gst-rtsp-server`) | 낮음 | 낮음 | **브라우저에서 직접 재생 불가** — 목표에 부적합 |

기존 `start_rtsp_server()`(`src/main.py:2343`)는 참고용으로만 두고 1차에서는 쓰지 않는다.

---

## 6. TensorRT 엔진 빌드 (유지)

`src/main.py:2586 _build_one_engine()`을 `engine.py`로 이식. 벤치마크 관련 부분만 제거.

- 엔진 파일명 규칙: `{name}_{onnx_tag}_{max_batch}_{dynamic|fixed}_{opt_frac}_{gpu_cc}.engine`
  → GPU compute capability(`nvidia-smi --query-gpu=compute_cap`)를 파일명에 넣어 다른 GPU에서 재사용 방지
- 이미 존재하면 skip
- `trtexec --onnx=... --saveEngine=... --minShapes/--optShapes/--maxShapes --fp16`
- dynamic batch: min=1, opt=`max_batch * 2/3`, max=`max_batch`
- 빌드 실패 시 그 모델만 `enabled=False`로 내리고 파이프라인은 계속 (기존 `_on_error_disable` 데코레이터 패턴 유지 — 커밋 `f29f327`에서 정리한 방식)

**삭제 대상**: `_make_benchmark_engine_path`, `_parse_trtexec_latency`, `_run_benchmark_mode`,
`_batch_stats_thread`, `_init_batch_hist`, `_batch_probe`, `_compute_batch_stats`

---

## 7. 삭제/보존 대상 정리

### 삭제
| 대상 | 위치 |
|---|---|
| gRPC 설정 수신 | `src/grpc_client.py`, `src/pipeline_pb2*.py`, `on_config`/`on_scan_cmd`/`on_benchmark_cmd` |
| 핫 리로드 / 콜드 재시작 | `_needs_cold_restart`, `_reapply_all`, `_apply_hot_reload`, `trigger_restart`, `os.execv` 블록 |
| 웹훅 서버 | `src/webhook_server.py` |
| 모델 레지스트리 | `src/model_engine.py`, `src/registry_client.py`, `src/providers/`, `src/nvinfer_generator.py` |
| SHM/UDS IPC | `_init_shm`, `_connect_uds`, `on_new_sample`의 SHM 경로, recon UDS, SBS Inova IPC |
| 벤치마크 | 6장 참고 |
| 스마트 레코딩 | `_start_recording`, `_on_sr_done`, `_cleanup_recordings`, `_schedule_check`, cron 스케줄 파싱 |
| 커스텀 분석 플러그인 | analytics / bev / feature_tracking / process / rppg 노드 + 전체 ctypes 바인딩 |

### 보존 (그대로 이식)
- `nvstreammux` 속성 세트 (4.2)
- `create_source_bin` / MJPEG bin / pad-added 링크 로직 (4.5)
- `_parse_nvinfer_props`, `_get_onnx_input_name`, `_update_preprocess_tensor_name`
- `_generate_preprocess_configs` (nvdspreprocess config 동적 생성)
- `_build_one_engine` (trtexec)
- `fps_probe`, `_source_alive_probe`, `_start_data_watchdog`(선택)
- `bus_call` (ERROR/WARNING 로깅)
- `configs/`, `models/`, `plugins/yolo-custom`, `plugins/rt-detr` 전부

---

## 8. 구현 순서 (마일스톤)

각 단계마다 **실제로 실행해서 확인**하고 다음으로 넘어간다.

**M1 — 뼈대**
`config.yaml` 로드 → `_compute_derived` → 로그로 derived 값 출력. 파이프라인 없음.
검증: `python src2/main.py --dry-run` 이 tiler rows/cols, engine_path, batched_push_timeout을 찍는다.

**M2 — 영상만 흐르게**
source_bin → streammux → tiler → nvvideoconvert → nvjpegenc → appsink → 웹 페이지.
**추론 없이** 브라우저에 타일 영상이 뜨는 것까지.
검증: `http://<host>:8810` 접속 시 N채널 타일이 보인다. `fakesink`로 먼저 파이프라인이
PLAYING까지 가는지 확인 후 웹 붙이는 게 빠르다.

**M3 — 엔진 빌드**
`engine.py`로 ONNX→engine. 이미 `models/`에 엔진이 있으면 skip 되는지 확인.
검증: 로그에 `Engine exists` 또는 `Engine built`.

**M4 — PGIE 1개 + OSD**
`nvdspreprocess` + `nvinfer`(human) + `nvdsosd`. 웹 화면에 bbox가 그려진다.
검증: bbox가 보인다. 안 보이면 → preprocess `tensor-name`, `src-ids`, `custom-lib-path` 순으로 의심.

**M5 — tracker + SGIE**
`nvtracker` + `process-mode=2` 모델 추가. object id가 안정적으로 유지되는지.

**M6 — 다채널 / 소스별 모델 on-off**
`_active_source_indices`가 실제로 반영되는지 (특정 채널만 추론).

**M7 — 견고성**
소스 죽었을 때 검정 프레임 대체(`create_black_source_bin`), 재연결, bus ERROR 로깅.

---

## 9. 알려진 함정 (기존 코드에서 이미 겪은 것들)

1. **teardown segfault** — NVIDIA 플러그인은 `set_state(NULL)` 시 종종 segfault. 기존 코드는
   재시작 시 아예 NULL 전환을 생략하고 `os.execv`로 프로세스를 갈아끼운다. 새 구현에서는
   재시작 기능을 안 만들지만, 종료 시 crash 로그가 나오면 이게 원인이다.
2. **프로브 등록 순서는 LIFO** — 같은 pad에 여러 프로브를 달면 나중에 등록한 게 먼저 실행된다.
3. **`request_pad_simple`은 미리** — 소스 연결이 실패해도 muxer가 채널 슬롯을 인식하게 하려면
   pad를 먼저 요청해둬야 한다.
4. **MJPEG bin은 `pad-added`가 안 온다** — 정적 ghost pad라서 수동 링크 필요 (4.5).
5. **`framerate.target`이 0/None으로 올 수 있다** — `fps.get("target") or 15` 방어.
   `batched_push_timeout` 계산에서 ZeroDivisionError.
6. **`sourceWidth/Height` 0 방어** — 좌표 스케일 계산에서 나눗셈 터진다 (`_normalize_sources`).
7. **nvinfer `batch-size`와 preprocess 그룹 배치 불일치** → 조용히 추론 스킵.
8. **NVMM caps** — 소스 bin 출력은 반드시 `video/x-raw(memory:NVMM), format=NV12`.
   CPU 메모리로 나가면 streammux가 거부한다.
9. **`nvjpegenc` 입력 포맷** — NVMM I420/NV12를 기대. `nvvideoconvert` 뒤에 capsfilter로 명시할 것.
   `nvjpegenc`가 없으면 `nvvideoconvert → videoconvert → jpegenc`(CPU) 폴백.

---

## 10. 작업 규칙

- 기존 `src/`는 **읽기 전용**. 새 코드는 `src2/`에 만든다. 검증 끝나면 그때 교체를 논의한다.
- 커밋은 마일스톤 단위로. 커밋 메시지 초안을 만들라는 요청과 실제 커밋 실행은 별개다 — **커밋은 명시적 지시가 있을 때만**.
- `sudo`/`apt` 명령은 실행하지 말고 사용자에게 넘긴다.
- 실행 확인은 실제 GPU 장비에서. 로그 전문을 근거로 보고할 것.
