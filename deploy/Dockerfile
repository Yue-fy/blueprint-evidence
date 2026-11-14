# syntax=docker/dockerfile:1.7
# Stoa "Make Blueprints Talk" (Team Hannah Family): CPU image, API-only mode (no local GPU model). Owner: P1.
# Build from the REPO ROOT (the context must contain stoa/, web/):
#
#   docker build -f deploy/Dockerfile --target api  -t stoa:api  .   # batch CLI + HTTP API (~1 GB)
#   docker build -f deploy/Dockerfile --target demo -t stoa:demo .   # + web demo behind a shared password
#
# No key, no dataset and no model weights are baked in: GEMINI_API_KEY (and DEMO_PASSWORD) come from the
# environment at run time, the dataset is mounted at /data/stoa. See deploy/README.md for the exact commands.

############################ api: Python package, CLI and FastAPI on CPU ############################
FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv JUNCTION_VENV=/opt/venv PATH=/opt/venv/bin:$PATH PYTHONPATH=/app \
    STOA_MODE=api \
    STOA_DATA=/data/stoa \
    JUNCTION_SHARED=/var/stoa \
    STOA_AGENT=deploy \
    GEMINI_MAX_CALLS_PER_DAY=200 \
    GEMINI_RPM=10

# opencv-python-headless needs glib; libgomp for numpy/opencv threads
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

WORKDIR /app
# dependencies first (cached layer), then the code
COPY pyproject.toml README.md ./
RUN mkdir -p stoa && touch stoa/__init__.py \
    && pip install --no-cache-dir -e . \
    && rm -rf stoa

# Optional: the pretrained CubiCasa5K model's live CPU inference (raster_cc5k). Off by default (~700 MB);
# its weights (CC BY-NC 4.0) are mounted at run time: -e CC5K_WEIGHTS=/models/cc5k/model_best_val_loss_var.pkl
ARG WITH_CC5K=0
RUN if [ "$WITH_CC5K" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu; fi
ENV CC5K_PYTHON=/opt/venv/bin/python

# the pipeline imports stoa.extras.* at run time (room graph, KG QA); they are part of stoa/
COPY stoa ./stoa
COPY docs ./docs
COPY deploy ./deploy
# runtime state the router reads from $JUNCTION_SHARED (the TRAIN split, self-contained): made by deploy/pack_state.py
# (gitignored, dataset-derived: it lives only in this private image). Without it every question fails.
RUN test -f deploy/state/results/splits/train.jsonl \
      || { echo "run: python deploy/pack_state.py (before docker build)" >&2; exit 1; } \
    && mkdir -p /var/stoa /data/stoa && cp -r deploy/state/. /var/stoa/ && rm -rf deploy/state \
    && mkdir -p /var/stoa/logs /var/stoa/locks /var/stoa/artifacts \
    && useradd --create-home --uid 10001 stoa && chown -R stoa /var/stoa /app

USER stoa
EXPOSE 8100
# default: the HTTP API (POST /ask, GET /health ...). Batch: docker run ... stoa:api python -m stoa.cli answer ...
CMD ["sh", "-c", "uvicorn stoa.api:app --host 0.0.0.0 --port ${PORT:-8100}"]

############################ web: build the demo front end ############################
FROM node:22-slim AS web
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

############################ demo: API + web (vite preview with its /api middleware) + Caddy password ############################
FROM api AS demo
USER root
# the node binary below is dynamically linked against libstdc++ (both images are Debian bookworm)
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
# Node runtime for `vite preview` (the /api middleware lives in web/mock/stoaApi.ts) and Caddy for the password
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=caddy:2.8 /usr/bin/caddy /usr/bin/caddy
COPY --from=web /app/web /app/web
COPY deploy/Caddyfile deploy/start-demo.sh /app/deploy/
RUN chmod +x /app/deploy/start-demo.sh && chown -R stoa /app/web
USER stoa
ENV PORT=8080 STOA_API_URL=http://127.0.0.1:8100 STOA_UPLOADS=/tmp/stoa-uploads STOA_TEXT_BACKEND=
EXPOSE 8080
CMD ["/app/deploy/start-demo.sh"]
