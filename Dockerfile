# docker build --build-arg PRODUCT=api|local -t group10-<product> .
FROM python:3.10-slim

ARG PRODUCT=api
ENV PRODUCT_MODE=${PRODUCT} \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf-cache

RUN apt-get update && \
    apt-get install -yq --no-install-recommends \
        ca-certificates curl prometheus-node-exporter && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

COPY requirements-api.txt requirements-local.txt ./
RUN pip install --no-cache-dir -r requirements-${PRODUCT}.txt

# Bake the model weights into the local image so the container starts offline.
RUN if [ "$PRODUCT" = "local" ]; then \
        python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-0.6B')"; \
    fi

COPY app.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# 7860 Gradio UI, 8000 prometheus_client, 9100 node exporter
EXPOSE 7860 8000 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD curl -fsS http://localhost:7860/ >/dev/null || exit 1

CMD ["./entrypoint.sh"]
