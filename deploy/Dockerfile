FROM python:3.12-slim

# zxing-cpp 有 manylinux 轮子，不需要编译工具链
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY qr_api.py payload_codec.py accounts.py server.py ./
COPY web/ ./web/

# 会话文件在运行时挂进来（绝不要打进镜像）
VOLUME ["/app/data"]
ENV SIGN_BASE=https://api.example.com

EXPOSE 8080
CMD ["python", "server.py", "--port", "8080", "--plain"]
