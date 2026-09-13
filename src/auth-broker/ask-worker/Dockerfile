# Isolated ask worker. Build on the VPS; do not reuse the owner Hermes process or HOME.
# Tools/memory are disabled in config.yaml and worker_main.py (Hermes AIAgent).
FROM python:3.12-slim

RUN useradd --system --uid 65532 --home-dir /isolated/home --create-home ask \
    && mkdir -p /isolated/workspace /opt/ask-worker/style \
    && chown -R ask:ask /isolated

COPY worker_main.py /opt/ask-worker/worker_main.py
COPY config.yaml /opt/ask-worker/config.yaml
COPY style/SOUL.md /opt/ask-worker/style/SOUL.md

# Optional: vendor a local hermes-agent checkout (Nous Research). Tests never build this.
ARG HERMES_SRC=""
RUN if [ -n "$HERMES_SRC" ]; then echo "HERMES_SRC must be provided as a build context mount by the operator"; fi

USER 65532:65532
WORKDIR /isolated
ENTRYPOINT ["python3", "/opt/ask-worker/worker_main.py"]
