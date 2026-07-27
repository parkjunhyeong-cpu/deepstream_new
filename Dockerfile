FROM nvcr.io/nvidia/deepstream:8.0-triton-multiarch

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY configs/ ./configs/
COPY models/ ./models/
COPY plugins/ ./plugins/
COPY proto/ ./proto/

# YOLO 커스텀 bbox 파서 빌드 — configs/pgie_human.txt의 custom-lib-path가 이 .so를 참조한다.
# CUDA_VER은 베이스 이미지의 CUDA 버전(12.8)과 맞춰야 Makefile이 /usr/local/cuda-12.8을 찾는다.
RUN CUDA_VER=12.8 make -C plugins/yolo-custom/nvdsinfer_custom_impl_Yolo

# proto/control_api.proto에서 gRPC stub 생성 — src/pb/*_pb2*.py는 git에 커밋 안 하고 여기서 만든다.
RUN python3 -m grpc_tools.protoc -I proto --python_out=src/pb --grpc_python_out=src/pb proto/control_api.proto

COPY public/ ./public/

# control-api(별도 컨테이너)의 클라이언트 역할이라 이 컨테이너는 gRPC 포트를 안 연다.
EXPOSE 8810

CMD ["python3", "src/main.py"]
