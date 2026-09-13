# Isolated ask worker. Pins Hermes and installs it at build; does not reuse owner HOME.
# Build: docker build -t mathai-ask-worker:local ask-worker
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 65532 --home-dir /isolated/home --create-home ask \
    && mkdir -p /isolated/workspace /isolated/bundled-plugins /opt/ask-worker/style \
    && chown -R ask:ask /isolated

COPY HERMES_PIN install_hermes.sh verify_hermes.py /opt/ask-worker/
RUN chmod +x /opt/ask-worker/install_hermes.sh /opt/ask-worker/verify_hermes.py \
    && bash /opt/ask-worker/install_hermes.sh

COPY worker_main.py /opt/ask-worker/worker_main.py
COPY config.yaml /opt/ask-worker/config.yaml
COPY style/SOUL.md /opt/ask-worker/style/SOUL.md

ENV PYTHONPATH=/opt/hermes-agent
ENV HERMES_ASK_SRC=/opt/hermes-agent
ENV PYTHONNOUSERSITE=1

USER 65532:65532
WORKDIR /isolated
# Command is only /job.json — launch.sh must not pass another python interpreter.
ENTRYPOINT ["python3", "/opt/ask-worker/worker_main.py"]
