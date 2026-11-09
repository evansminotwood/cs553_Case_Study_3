#!/usr/bin/env python3
"""Summarise Prometheus data and ask an LLM to analyse it.

  python3 scripts/analyze_metrics.py --prom http://localhost:2006 --window 1h

Writes reports/llm_prompt.md, and reports/llm_response.md if WPI_LLM_API_KEY is set.
"""

import argparse
import json
import os
import pathlib
import urllib.parse
import urllib.request

QUERIES = {
    "requests_per_min": "sum by (product, backend, status) (rate(blackjack_requests_total[5m])) * 60",
    "total_requests": "sum by (product, backend, status) (blackjack_requests_total)",
    "errors_by_type_total": "sum by (product, backend, error_type) (blackjack_errors_total)",
    "failovers_total": "sum by (product) (blackjack_failovers_total)",
    "latency_p50_s": "histogram_quantile(0.5, sum by (le, product, backend) (rate(blackjack_request_latency_seconds_bucket[15m])))",
    "latency_p95_s": "histogram_quantile(0.95, sum by (le, product, backend) (rate(blackjack_request_latency_seconds_bucket[15m])))",
    "ttft_p95_s": "histogram_quantile(0.95, sum by (le, product, backend) (rate(blackjack_time_to_first_token_seconds_bucket[15m])))",
    "output_tokens_per_min": "sum by (product, backend) (rate(blackjack_output_tokens_total[5m])) * 60",
    "requests_in_progress": "blackjack_requests_in_progress",
    "model_load_seconds": "blackjack_model_load_seconds",
    "avg_conversation_turns": "sum by (product) (rate(blackjack_conversation_turns_sum[30m])) / sum by (product) (rate(blackjack_conversation_turns_count[30m]))",
    "app_rss_bytes": 'process_resident_memory_bytes{job="blackjack_app"}',
    "app_cpu_cores": 'rate(process_cpu_seconds_total{job="blackjack_app"}[5m])',
    "target_up": "up",
    "vm_cpu_busy_pct": '100 - avg by (product) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
    "vm_mem_used_pct": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
    "vm_load1": "node_load1",
    "vm_cpu_count": 'count by (product) (node_cpu_seconds_total{mode="idle"})',
    "container_net_rx_Bps": 'rate(node_network_receive_bytes_total{device="eth0"}[5m])',
    "container_net_tx_Bps": 'rate(node_network_transmit_bytes_total{device="eth0"}[5m])',
}

PROMPT = """You are an SRE reviewing monitoring data for a small ML product.

Product: "Blackjack Tutor", a Gradio chatbot deployed as two Docker containers on a
shared university VM (other student groups run containers on the same VM):
  * product=api   -> streams answers from openai/gpt-oss-20b via the Hugging Face Inference API;
                     if that fails it automatically fails over to the local model
  * product=local -> runs Qwen/Qwen3-0.6B on the container's CPU (no GPU)
The backend label says which model answered (remote or local).
Each container exposes prometheus_client app metrics (:8000) and prometheus-node-exporter
(:9100). Note: node exporter inside a container reads the host's /proc, so CPU, memory
and load describe the whole shared VM; network (eth0) and process_* metrics are per container.

Below are Prometheus results over the last {window} (min / mean / max / last per series,
step {step}s). Please:
1. Summarise the health of the system and of each product.
2. Point out anomalies, bottlenecks or risks (latency, errors, resource pressure, capacity).
3. Recommend concrete actions, ranked by priority, and say if "no action" is reasonable.
Be specific and cite the numbers you rely on.

{data}
"""


def prom_get(base, path, params):
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(body)
    return body["data"]["result"]


def parse_window(w):
    units = {"m": 60, "h": 3600, "d": 86400}
    return int(float(w[:-1]) * units[w[-1]])


def summarise(base, window_s):
    now = float(prom_get(base, "/api/v1/query", {"query": "time()"})[1])
    step = max(15, window_s // 40)
    lines = []
    for name, q in QUERIES.items():
        try:
            series = prom_get(base, "/api/v1/query_range",
                              {"query": q, "start": now - window_s, "end": now, "step": step})
        except Exception as exc:
            lines.append(f"- {name}: query failed ({exc})")
            continue
        if not series:
            lines.append(f"- {name}: no data")
            continue
        for s in series:
            labels = ",".join(f"{k}={v}" for k, v in sorted(s["metric"].items())
                              if k not in ("__name__", "instance"))
            vals = [float(v) for _, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")]
            if not vals:
                lines.append(f"- {name}{{{labels}}}: NaN (no traffic in window)")
                continue
            lines.append(
                f"- {name}{{{labels}}}: min={min(vals):.3g} mean={sum(vals)/len(vals):.3g} "
                f"max={max(vals):.3g} last={vals[-1]:.3g} (n={len(vals)})"
            )
    return "\n".join(lines), step


def ask_llm(prompt, model, base_url, key):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.2}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--window", default="6h")
    ap.add_argument("--model", default="minimax-m2.5")
    ap.add_argument("--llm-url", default="https://ggpt-llm-p-u02.int.wpi.edu/v1")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    data, step = summarise(args.prom, parse_window(args.window))
    prompt = PROMPT.format(window=args.window, step=step, data=data)
    out = pathlib.Path(__file__).resolve().parent.parent / "reports"
    out.mkdir(exist_ok=True)
    (out / "llm_prompt.md").write_text(prompt)
    print(f"wrote {out / 'llm_prompt.md'}")

    key = os.environ.get("WPI_LLM_API_KEY")
    if args.no_llm or not key:
        print("No WPI_LLM_API_KEY (or --no-llm): paste reports/llm_prompt.md into any LLM.")
        return
    answer = ask_llm(prompt, args.model, args.llm_url, key)
    (out / "llm_response.md").write_text(f"Model: {args.model}\n\n{answer}\n")
    print(f"wrote {out / 'llm_response.md'}\n\n{answer}")


if __name__ == "__main__":
    main()
