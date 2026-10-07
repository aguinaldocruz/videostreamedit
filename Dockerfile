# Build the PGS -> VobSub -> SRT toolchain separately from the application.
# VobSub2SRT needs a maintained fork for Tesseract 5 compatibility.
FROM debian:bookworm-slim AS ocr-toolchain

ARG VOBSUB2SRT_REV=149b34b969a2b287f4df4044e4a1cb5dc6b52c3d
ARG BDSUP2SUB_SHA256=13ef67e6e45a4033d82e065e350ca62ed593f337cda2a714c4f3bd4fdcd4ad89

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential cmake git pkg-config libavutil-dev libtesseract-dev libtiff-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/leonard-slass/VobSub2SRT.git /src/VobSub2SRT \
    && cd /src/VobSub2SRT \
    && git checkout --detach "$VOBSUB2SRT_REV" \
    && sed -i '1i#include <climits>' src/vobsub2srt.c++ \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS_RELEASE='-O2 -DNDEBUG' \
    && cmake --build build --parallel 2 \
    && install -Dm755 build/bin/vobsub2srt /opt/ocr/bin/vobsub2srt \
    && mkdir -p /opt/ocr/lib \
    && curl -fsSL https://repo.maven.apache.org/maven2/com/github/riccardove/easyjasub/bdsup2sub/5.2.0/bdsup2sub-5.2.0-jar-with-dependencies.jar -o /opt/ocr/lib/bdsup2sub.jar \
    && echo "$BDSUP2SUB_SHA256  /opt/ocr/lib/bdsup2sub.jar" | sha256sum -c -

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo \
    CONFIG_DIR=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg gosu mkvtoolnix ca-certificates curl gnupg \
       openjdk-17-jre-headless tesseract-ocr tesseract-ocr-eng tesseract-ocr-por \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ocr-toolchain /opt/ocr/bin/vobsub2srt /usr/local/bin/vobsub2srt
COPY --from=ocr-toolchain /opt/ocr/lib/bdsup2sub.jar /opt/bdsup2sub.jar

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY db ./db
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /config /data \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin videostreamedit

EXPOSE 8080
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.v86:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "90"]
