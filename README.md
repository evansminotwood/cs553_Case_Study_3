# Blackjack Tutor: Case Study 3 (Docker, Prometheus, Grafana, ngrok)

The Case Study 1 **Blackjack Tutor**
([Hugging Face Space](https://huggingface.co/spaces/emwo/CS553_WPI_Fall_2026_Case_Study_1))
running as two Docker containers on the shared course VM, monitored with Prometheus
and Grafana, and published through one ngrok URL.

```
internet ── ngrok ── group10-gateway (nginx :80)
                       ├─ /api/     ► group10-api     :7860 UI  :8000 app metrics  :9100 node exporter
                       ├─ /local/   ► group10-local   :7860 UI  :8000 app metrics  :9100 node exporter
                       └─ /grafana/ ► group10-grafana :3000 ◄── group10-prometheus :9090
```

| Product | URL |
|---|---|
| API product | https://jockstrap-reprimand-populate.ngrok-free.dev/api/ |
| Local product | https://jockstrap-reprimand-populate.ngrok-free.dev/local/ |
| Grafana (read-only) | https://jockstrap-reprimand-populate.ngrok-free.dev/grafana/ |

## Files

| File | Purpose |
|---|---|
| `app.py` | The CS1 app plus Prometheus metrics. `PRODUCT_MODE=api` or `local` selects the product |
| `Dockerfile` | Builds either product (`--build-arg PRODUCT=api\|local`) with `prometheus-node-exporter` installed |
| `entrypoint.sh` | Starts node exporter on :9100, then the app |
| `compose.yaml` | The full stack: both products, Prometheus, Grafana, the nginx gateway and the tunnel |
| `run_stack.sh` | Runs on the VM: computes ports from `PORT_BASE` and starts the stack (Compose, or plain `docker run` when Compose is missing) |
| `deploy.sh` | Runs on a laptop: copies the project to the VM and calls `run_stack.sh` |
| `config.env` | Group name, port range, VM address, tunnel provider |
| `secrets.env.example` | Template for `secrets.env` (HF token, ngrok token, Grafana password) |
| `gateway/` `prometheus/` `grafana/` `ngrok/` | nginx routes, scrape config, provisioned datasource and dashboard, tunnel config |
| `scripts/generate_traffic.py` | Sends sample chats so the dashboards have data |
| `scripts/analyze_metrics.py` | Summarises Prometheus data into an LLM prompt |

## Product

Both containers run the same CS1 interface: HF login in the sidebar, the system
message, max tokens, temperature, top-p, **Use Local Model**, and **Simulate Remote
Outage (Demo Failover)**. Remote requests go to `openai/gpt-oss-20b` through the HF
Inference API and fail over to `Qwen/Qwen3-0.6B` automatically, as in CS1.

- **API product** (`group10-api`): remote by default. The image has no torch, so local
  generation (manual or failover) is sent to the local container over the Docker network.
- **Local product** (`group10-local`): Use Local Model is on by default and Qwen runs
  in-process on the CPU.

Changes from CS1, all needed to run outside a GPU Space:

- No `spaces` / `@spaces.GPU`; the device is CUDA when available, otherwise CPU.
- Outside a Space, Gradio replaces the HF OAuth login with a local login as the
  owner of `HF_TOKEN` and stores that token in the session cookie, which is signed
  but not encrypted. `app.py` stores a placeholder in the cookie instead and swaps
  in `HF_TOKEN` on the server, so the token never reaches the browser. The gateway
  sends `/login/...` and `/logout` to the app and returns the user to the product
  they came from.
- The token is typed `Optional[gr.OAuthToken]`, so the local model works without logging in.
- Qwen3 thinking is disabled (`/no_think`) and stripped, because on a CPU it takes minutes.
- `gpt-oss-20b` is called with `reasoning_effort="low"`; at the default effort its
  hidden reasoning could use the whole token budget and return an empty answer.
- Chat history from Gradio 6 is flattened to plain-text messages before generation.

## Deploy

```bash
cp secrets.env.example secrets.env && chmod 600 secrets.env
./deploy.sh                 # copy to the VM, build and start everything
./deploy.sh status          # containers, container IPs, health checks, public URLs
./deploy.sh traffic api 40
./deploy.sh traffic local 6
./deploy.sh tunnel          # Grafana on localhost:3000/grafana/, Prometheus on localhost:9090
./deploy.sh down
```

The VM (`class-docker`) is reached through `melnibone.wpi.edu:2222` and needs the WPI
network or VPN. Every container, image and network is prefixed `group10-`, and host
ports come from the claimed range **2000–2010**:

| Port | Service |
|---|---|
| 2000 / 2001 / 2002 | API product: UI / app metrics / node exporter |
| 2003 / 2004 / 2005 | Local product: UI / app metrics / node exporter |
| 2006 | Prometheus |
| 2007 | Grafana |
| 2008 | ngrok inspector (localhost only) |
| 2009 | Gateway |

ngrok's free plan gives one domain per account, so a single tunnel points at the
gateway, which routes by path. Gradio learns its prefix from `GRADIO_ROOT_PATH` and
Grafana from `GF_SERVER_SERVE_FROM_SUB_PATH`.

## Metrics

`prometheus_client` on :8000. `product` is the container; `backend` is the model that
answered (`remote` or `local`).

| Metric | Type |
|---|---|
| `blackjack_app_info` | Info |
| `blackjack_requests_total{backend,status}` (success / error / no_token) | Counter |
| `blackjack_errors_total{backend,error_type}` | Counter |
| `blackjack_failovers_total` | Counter |
| `blackjack_requests_in_progress` | Gauge |
| `blackjack_request_latency_seconds{backend}` | Histogram |
| `blackjack_time_to_first_token_seconds{backend}` | Histogram |
| `blackjack_output_tokens_total{backend}` | Counter |
| `blackjack_conversation_turns` | Histogram |
| `blackjack_model_load_seconds` | Gauge |

`prometheus-node-exporter` on :9100 provides the `node_*` system metrics. Inside a
container it reads the host's `/proc`, so CPU, memory and load describe the whole VM,
while network counters are per container.

## Tests

```bash
pip install -r requirements-api.txt pytest && pytest -q
```
