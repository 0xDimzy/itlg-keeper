FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ITLG_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 keeper \
    && mkdir -p /data \
    && chown keeper:keeper /data

USER keeper
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s \
    CMD ["itlg-keeper", "status", "--json"]

ENTRYPOINT ["itlg-keeper"]
CMD ["run"]
