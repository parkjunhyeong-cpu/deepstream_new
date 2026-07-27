FROM nvcr.io/nvidia/deepstream:8.0-triton-multiarch

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY configs/ ./configs/
COPY models/ ./models/
COPY plugins/ ./plugins/

# YOLO 커스텀 bbox 파서 빌드 — configs/pgie_human.txt의 custom-lib-path가 이 .so를 참조한다.
# CUDA_VER은 베이스 이미지의 CUDA 버전(12.8)과 맞춰야 Makefile이 /usr/local/cuda-12.8을 찾는다.
RUN CUDA_VER=12.8 make -C plugins/yolo-custom/nvdsinfer_custom_impl_Yolo

COPY public/ ./public/

# 현재 src/main.py는 소스 연결 확인용 임시 엔트리포인트 (인자로 RTSP/HTTP URI 필요)
# 3단계(파이프라인 구성)에서 config.yaml 기반 정식 엔트리포인트로 교체 예정
EXPOSE 8810

CMD ["python3", "src/main.py"]
