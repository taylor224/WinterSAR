# wintersar image: core toolkit + synthetic smoke data. External engines (ISCE2, SNAPHU,
# MintPy, tophu) are NOT bundled (licence isolation, plan §9); install them in a derived
# image from conda-forge if you need the local processing path.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    WINTERSAR_LANG=ko

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgdal-dev gdal-bin libproj-dev git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY benchmarks ./benchmarks
COPY examples ./examples

RUN uv pip install --system -e .

# Synthetic smoke data is generated at runtime (no large fixtures in the image).
RUN wintersar --json check-install || true

ENTRYPOINT []
CMD ["wintersar", "--help"]
