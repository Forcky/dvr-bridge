# syntax=docker/dockerfile:1
# Builds for linux/amd64 and linux/arm64. Everything third-party is pinned: python 3.13 (bookworm
# slim) and static ffmpeg 7.1 by multi-arch index digest, go2rtc 1.9.15 by per-architecture sha256.
ARG TARGETARCH

FROM scratch AS go2rtc-amd64
ADD --checksum=sha256:20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712 --chmod=0755 \
    https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_amd64 /go2rtc

FROM scratch AS go2rtc-arm64
ADD --checksum=sha256:af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763 --chmod=0755 \
    https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_arm64 /go2rtc

FROM go2rtc-${TARGETARCH} AS go2rtc

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base
COPY --from=go2rtc /go2rtc /usr/local/bin/go2rtc
COPY --from=mwader/static-ffmpeg:7.1@sha256:a8090df5f5608daef387e1b2e93b98aaacb4d92153ad904e7d715c725724fca4 /ffmpeg /usr/local/bin/ffmpeg

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DVRBRIDGE_DATA_DIR=/data
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin dvrbridge \
 && install -d -o 10001 -g 10001 -m 0700 /data
WORKDIR /srv
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY dvrbridge/ ./dvrbridge/

# `docker build --target test .` runs the test suite; the runtime image has no tests or pytest.
FROM base AS test
COPY requirements-dev.txt pytest.ini config.example.yaml ./
RUN pip install -r requirements-dev.txt
COPY tests/ ./tests/
RUN pytest -q

FROM base AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="DVR Bridge" \
      org.opencontainers.image.description="Modern browser UI for older RaySharp-family DVRs: live view, recordings, MP4 export" \
      org.opencontainers.image.source="https://github.com/Forcky/dvr-bridge" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV DVRBRIDGE_BUILD_REVISION=${REVISION}
USER 10001:10001
VOLUME /data
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3).status == 200 else 1)"]
# --no-proxy-headers: client addresses come from auth.trusted_proxies only (dvrbridge/auth/netutil.py).
CMD ["uvicorn", "--factory", "dvrbridge.server:create_server", "--host", "0.0.0.0", "--port", "8099", "--no-access-log", "--no-proxy-headers"]
