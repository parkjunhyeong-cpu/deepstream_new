# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

This is a from-scratch reimplementation of a DeepStream RTSP inference pipeline (`SOLUTION_DeepStream`'s pipeline core, stripped down). The full target design, config schema, milestone plan, and known pitfalls are written out in **[NEW_PIPELINE_GUIDE.md](NEW_PIPELINE_GUIDE.md) — read it before doing any pipeline work**, it is the authoritative spec, not just background reading.

The repo is currently at an early milestone: `src/main.py` is a temporary CLI for verifying source connection and JPEG encoding, not the real pipeline entrypoint yet. Do not assume `config.yaml`, `pipeline.py`, or `webview.py` exist until they've actually been created — check first.

## Environment

This code only runs inside the DeepStream container (`nvcr.io/nvidia/deepstream:8.0-triton-multiarch`, see [Dockerfile](Dockerfile)). `pyds`, `gi`/`Gst`/`GLib` (PyGObject), and the custom parser `.so` files under `plugins/` are not available outside it — you cannot execute or unit-test pipeline code in this environment. Static review, and asking the user to run/verify on the actual GPU box, is the expected workflow.

## Commands

- Build image: `docker build -t deepstream-new .`
- Run the current temp entrypoint (inside the container):
  - `python3 src/main.py source <rtsp-uri>` — connects a source bin and logs fps only
  - `python3 src/main.py encode <rtsp-uri> --out /tmp/preview.jpg` — adds jpeg encode + appsink, writes a preview frame once/sec
- No lint config or test suite exists yet in this repo — don't assume `pytest`/`ruff`/etc. are wired up; check before referencing them.

## Architecture

Target pipeline chain (see NEW_PIPELINE_GUIDE.md §4 for full detail):

```
source_bin[0..N] → nvstreammux → (nvdspreprocess → nvinfer)×PGIE → nvtracker → nvinfer×SGIE
                                → nvmultistreamtiler → nvdsosd → nvvideoconvert → output
```

Everything is driven by a single fixed YAML config (`input`/`pipeline`/`output` sections); there is no gRPC control-api, hot reload, or model registry in this rebuild — those are explicitly out of scope (guide §0, §7). Output is served directly from the Python process as MJPEG-over-HTTP (`multipart/x-mixed-replace`), not shipped out via SHM/UDS to a separate stream server.

A few non-obvious constraints carried over from the original implementation (guide §9 has the full list):
- PGIE vs SGIE is decided by `process-mode` in the `configs/*.txt` nvinfer config, not hardcoded.
- `gie-unique-id` must be read back at runtime via `get_property("unique-id")`, never hardcoded from config.
- Source bins output NVMM `NV12` only; CPU memory output will be rejected by `nvstreammux`.
- The MJPEG appsink callback must never block on I/O (e.g. socket writes) — it only swaps a shared frame buffer under a lock; a separate thread/HTTP handler does the sending.

## Code conventions specific to this project

- **No nested function definitions.** If a helper is only used inside another function, pull it out to module scope instead of defining it inline (this includes GStreamer/GLib callbacks — define them as top-level or class functions and pass state in explicitly, don't close over locals).
- **Split by responsibility, one concern per module.** Following the guide's target layout (config loading, engine build, preprocess config generation, pipeline assembly, sources, web output, probes are each their own file) — don't fold unrelated responsibilities into one file. If a file is growing past ~300 lines or starts doing two unrelated things, split it.
- **All global/shared mutable state lives in `state.py`.** Don't scatter module-level mutable globals (frame buffers, counters, running flags, etc.) across other files — define and mutate them through `state.py` so there's one place to see what process-wide state exists. Per-function local state (e.g. a probe's own closure state) doesn't need to move there, but if it needs to be nested-closure state, prefer an explicit object/class instance passed around instead (see the no-nested-functions rule above).

## 참고: NVIDIA 공식 deepstream_python_apps와 비교

출처: https://github.com/NVIDIA-AI-IOT/deepstream_python_apps (특히 `apps/deepstream-test3`, `apps/common/bus_call.py`)

**우리가 그대로 따르는 패턴**

- **element는 로컬 변수로 계속 들고 있다가 직접 쓴다.** `pipeline.get_by_name("이름")`으로 나중에 다시 찾지 않는다 — 이름 문자열이 두 군데서 어긋나면 조용히 `None`이 되고 한참 뒤 엉뚱한 곳에서 죽는다. (`pipeline.py`의 `build_pipeline()`이 `pgie`/`sink`를 만든 그 자리에서 바로 리턴하는 이유.)
- **pad probe 콜백 시그니처**: `(pad, info, u_data) -> Gst.PadProbeReturn.OK`, 등록은 `pad.add_probe(Gst.PadProbeType.BUFFER, callback, u_data)`.
- **`NvDsBatchMeta` 순회 패턴** (`probes/detections.py`가 그대로 따름):
  ```python
  batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
  l_frame = batch_meta.frame_meta_list
  while l_frame is not None:
      frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
      l_obj = frame_meta.obj_meta_list
      while l_obj is not None:
          obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
          # ...
          l_obj = l_obj.next
      l_frame = l_frame.next
  ```
- **`bus_call`의 기본형**: EOS → 로그 후 루프 종료, ERROR → 파싱 후 루프 종료, WARNING → 로그만 남기고 계속. 공식 샘플도 소스별 구분 없이 ERROR면 무조건 종료한다 — 그래서 "소스발 에러는 무시하고 계속 돌게" 하려는 우리 장애 대응 작업은 공식 샘플보다 한 단계 더 나간 개선이라는 걸 인지하고 갈 것.

**의도적으로 다르게 가는 부분**

- 공식 샘플은 element 생성 실패 시 `sys.stderr.write()`만 하고 계속 진행한다 — `pgie`가 `None`인 채로 계속 흘러가다 몇 줄 뒤 훨씬 헷갈리는 에러로 죽는다. 우리는 `pipeline.py`의 `_make()`가 실패 즉시 원인이 명확한 `RuntimeError`를 던진다. 이 차이는 유지한다.
- 공식 샘플은 `print()`/`sys.stderr.write()` 기반. 우리는 `logger.py` 기반 로깅으로 통일한다.
