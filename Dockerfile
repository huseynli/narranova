# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.12-slim-bookworm AS runtime

ARG NARRANOVA_VERSION=dev
LABEL org.opencontainers.image.title="Narranova" \
      org.opencontainers.image.description="Self-hosted EPUB-to-audiobook production pipeline" \
      org.opencontainers.image.version="${NARRANOVA_VERSION}" \
      org.opencontainers.image.url="https://narranova.app" \
      org.opencontainers.image.source="https://github.com/huseynli/NarraNova"

RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin narranova \
    && mkdir -p /data \
    && chown narranova:narranova /data

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/narranova-*.whl \
    && rm -rf /wheels

ENV HOME=/tmp \
    NARRANOVA_DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /data
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).read()"]

ENTRYPOINT ["narranova"]
CMD ["web", "--host", "0.0.0.0", "--port", "8787"]
