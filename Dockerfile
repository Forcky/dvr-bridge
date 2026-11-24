# syntax=docker/dockerfile:1
# Pinned: python 3.13 (bookworm slim, by digest), go2rtc 1.9.15 (sha256-verified), static ffmpeg 7.1.
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base

ADD --checksum=sha256:20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712 --chmod=0755 \
    https://github.com/AlexxIT/go2rtc/releases/download/v1.9.15/go2rtc_linux_amd64 /usr/local/bin/go2rtc
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /usr/local/bin/ffmpeg

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
USER 10001:10001
VOLUME /data
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "--factory", "dvrbridge.main:create_app", "--host", "0.0.0.0", "--port", "8099", "--no-access-log"]
