ARG BASE_IMAGE=python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
FROM ${BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
COPY vendor /app/vendor
RUN python -m pip install --no-cache-dir --no-index --find-links=/app/vendor --only-binary=:all: --require-hashes -r /app/requirements.lock \
    && rm -rf /app/vendor
COPY minivectordb /app/minivectordb
RUN python -m compileall -q /app/minivectordb && mkdir /data
ENTRYPOINT ["python", "-m", "minivectordb", "--data-dir", "/data"]
