import os
import re
import time
from typing import Optional

import gradio as gr
from huggingface_hub import InferenceClient
from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

PRODUCT_MODE = os.environ.get("PRODUCT_MODE", "api").strip().lower()
if PRODUCT_MODE not in ("api", "local"):
    raise ValueError(f"PRODUCT_MODE must be 'api' or 'local', got {PRODUCT_MODE!r}")

LOCAL_MODEL = "Qwen/Qwen3-0.6B"
REMOTE_MODEL = "openai/gpt-oss-20b"
LOCAL_PRODUCT_URL = os.environ.get("LOCAL_PRODUCT_URL", "http://gateway/local/")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "8000"))
APP_PORT = int(os.environ.get("APP_PORT", "7860"))
MOCK_OAUTH_TOKEN = "mock-oauth-token-for-local-dev"

LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 45, 60, 90, 120, 180)

APP_INFO = Info("blackjack_app", "Static information about this product")
REQUESTS = Counter(
    "blackjack_requests_total",
    "Chat requests by backend that answered and outcome",
    ["product", "backend", "status"],
)
ERRORS = Counter(
    "blackjack_errors_total",
    "Failed generations by backend and exception type",
    ["product", "backend", "error_type"],
)
FAILOVERS = Counter(
    "blackjack_failovers_total",
    "Requests that fell back from the remote API to the local model",
    ["product"],
)
IN_PROGRESS = Gauge(
    "blackjack_requests_in_progress",
    "Chat requests currently being generated",
    ["product"],
)
LATENCY = Histogram(
    "blackjack_request_latency_seconds",
    "End-to-end time to produce the full answer",
    ["product", "backend"],
    buckets=LATENCY_BUCKETS,
)
TTFT = Histogram(
    "blackjack_time_to_first_token_seconds",
    "Time until the user sees the first piece of the answer",
    ["product", "backend"],
    buckets=LATENCY_BUCKETS,
)
OUTPUT_TOKENS = Counter(
    "blackjack_output_tokens_total",
    "Tokens generated (remote: streamed chunks, local: tokenizer count)",
    ["product", "backend"],
)
CONVERSATION_TURNS = Histogram(
    "blackjack_conversation_turns",
    "Number of earlier messages sent along with each request",
    ["product"],
    buckets=(0, 1, 2, 4, 6, 8, 12, 16, 24, 32, 64),
)
MODEL_LOAD_SECONDS = Gauge(
    "blackjack_model_load_seconds",
    "Seconds spent loading the local model at startup (0 if not loaded here)",
    ["product"],
)

APP_INFO.info({"product": PRODUCT_MODE, "remote_model": REMOTE_MODEL, "local_model": LOCAL_MODEL})
MODEL_LOAD_SECONDS.labels(PRODUCT_MODE).set(0)
IN_PROGRESS.labels(PRODUCT_MODE).set(0)
FAILOVERS.labels(PRODUCT_MODE)
for _backend in ("remote", "local"):
    for _status in ("success", "error"):
        REQUESTS.labels(PRODUCT_MODE, _backend, _status)
REQUESTS.labels(PRODUCT_MODE, "remote", "no_token")

pipe = None
if PRODUCT_MODE == "local":
    import torch
    from transformers import pipeline

    _t0 = time.perf_counter()
    pipe = pipeline(
        "text-generation",
        model=LOCAL_MODEL,
        dtype="auto",
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    MODEL_LOAD_SECONDS.labels(PRODUCT_MODE).set(time.perf_counter() - _t0)

# Qwen3 thinks before answering by default, which takes minutes on a CPU.
# /no_think turns that off; any leftover <think> block is stripped.
THINK_RE = re.compile(r"<think>.*?(</think>\s*|$)", re.DOTALL)

fancy_css = """
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&display=swap');

:root, .dark {
    --body-background-fill: radial-gradient(ellipse at top, #145a32 0%, #0b3d2e 55%, #062017 100%);
    --background-fill-primary: #0f4a33;
    --background-fill-secondary: #0b3d2e;
    --border-color-primary: #d4af37;
    --border-color-accent: #d4af37;
    --button-primary-background-fill: #d4af37;
    --button-primary-background-fill-hover: #e8c766;
    --button-primary-text-color: #1a1006;
    --color-accent: #d4af37;
    --body-text-color: #f5f0e1;
    --link-text-color: #f4d160;
}

.gradio-container {
    width: 96% !important;
    max-width: none !important;
}
#app-title {
    text-align: center;
    margin-bottom: 4px;
    font-family: "Playfair Display", Georgia, serif;
    font-size: 2.4em;
    color: #d4af37;
    text-shadow: 0 2px 6px rgba(0, 0, 0, 0.5);
    letter-spacing: 1px;
}
#app-subtitle {
    text-align: center;
    color: #e8dcb5;
    font-style: italic;
    margin-bottom: 24px;
}
#chat-container {
    width: 100%;
    border: 2px solid #d4af37;
    border-radius: 16px;
    padding: 16px;
    background: repeating-linear-gradient(135deg, #0b3d2e, #0b3d2e 24px, #0e4534 24px, #0e4534 48px);
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4), inset 0 0 40px rgba(0, 0, 0, 0.25);
}
#model-note {
    font-size: 0.9em;
    color: #e8dcb5;
    margin-top: 8px;
}
@media (max-width: 768px) {
    .gradio-container {
        width: 98% !important;
    }
    #chat-container {
        padding: 8px;
    }
    #app-title {
        font-size: 1.8em;
    }
}
"""


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text(part) for part in content)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return "" if content is None else str(content)


def build_messages(message, history, system_message):
    messages = [{"role": "system", "content": system_message}]
    for item in history or []:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
            messages.append({"role": item["role"], "content": _text(item.get("content"))})
    messages.append({"role": "user", "content": _text(message)})
    return messages


def generate_on_this_container(
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> str:
    messages = [dict(m) for m in messages]
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] += " /no_think"
    outputs = pipe(
        messages,
        max_new_tokens=int(max_tokens),
        do_sample=True,
        temperature=temperature,
        top_p=top_p,
    )
    text = outputs[0]["generated_text"][-1]["content"]
    OUTPUT_TOKENS.labels(PRODUCT_MODE, "local").inc(
        len(pipe.tokenizer.encode(text, add_special_tokens=False))
    )
    return THINK_RE.sub("", text).strip()


def local_generate(messages, max_tokens, temperature, top_p):
    if pipe is not None:
        return generate_on_this_container(messages, max_tokens, temperature, top_p)
    # The API container has no torch; the model lives in the local container.
    from gradio_client import Client

    return Client(LOCAL_PRODUCT_URL, verbose=False).predict(
        messages, int(max_tokens), float(temperature), float(top_p), api_name="/local_generate"
    )


_real_mocked_oauth_info = gr.oauth._get_mocked_oauth_info


def _mocked_oauth_info_without_token():
    # Outside a Space, Gradio logs users in as the owner of HF_TOKEN and stores
    # the token in the (signed, not encrypted) session cookie. Keep only a
    # placeholder in the cookie and swap in the real token on the server.
    info = _real_mocked_oauth_info()
    info["access_token"] = MOCK_OAUTH_TOKEN
    return info


gr.oauth._get_mocked_oauth_info = _mocked_oauth_info_without_token


def _resolve_token(hf_token):
    token = getattr(hf_token, "token", None)
    if token == MOCK_OAUTH_TOKEN:
        token = os.environ.get("HF_TOKEN")
    return token


def _record_error(backend, exc):
    REQUESTS.labels(PRODUCT_MODE, backend, "error").inc()
    ERRORS.labels(PRODUCT_MODE, backend, type(exc).__name__).inc()


def _run_local(messages, max_tokens, temperature, top_p):
    start = time.perf_counter()
    try:
        response = local_generate(messages, max_tokens, temperature, top_p)
    except Exception as exc:
        _record_error("local", exc)
        raise
    elapsed = time.perf_counter() - start
    TTFT.labels(PRODUCT_MODE, "local").observe(elapsed)
    LATENCY.labels(PRODUCT_MODE, "local").observe(elapsed)
    REQUESTS.labels(PRODUCT_MODE, "local", "success").inc()
    return response


def respond(
    message,
    history: list[dict[str, str]],
    system_message,
    max_tokens,
    temperature,
    top_p,
    use_local_model,
    simulate_remote_outage,
    hf_token: Optional[gr.OAuthToken],
):
    messages = build_messages(message, history, system_message)
    CONVERSATION_TURNS.labels(PRODUCT_MODE).observe(len(history or []))

    with IN_PROGRESS.labels(PRODUCT_MODE).track_inprogress():
        if use_local_model:
            print("[MODE] local (manual)")

            response = _run_local(messages, max_tokens, temperature, top_p)

            yield f"🖥️ **[Local Model — {LOCAL_MODEL}]**\n\n{response}"
            return

        token = _resolve_token(hf_token)
        if not token:
            REQUESTS.labels(PRODUCT_MODE, "remote", "no_token").inc()
            yield "⚠️ Please log in with your Hugging Face account first."
            return

        print("[MODE] api (with automatic failover)")

        start = time.perf_counter()
        first_token_at = None
        n_chunks = 0
        try:
            if simulate_remote_outage:
                raise RuntimeError("Simulated remote outage (demo toggle enabled)")

            client = InferenceClient(
                token=token,
                model=REMOTE_MODEL,
                timeout=15,
            )

            response = ""

            for chunk in client.chat_completion(
                messages,
                max_tokens=max_tokens,
                stream=True,
                temperature=temperature,
                top_p=top_p,
                # gpt-oss reasons before answering and that reasoning shares
                # max_tokens; at the default effort it can use all of it.
                extra_body={"reasoning_effort": "low"},
            ):
                choices = chunk.choices
                token = ""

                if len(choices) and choices[0].delta.content:
                    token = choices[0].delta.content

                if token and first_token_at is None:
                    first_token_at = time.perf_counter()
                    TTFT.labels(PRODUCT_MODE, "remote").observe(first_token_at - start)
                n_chunks += bool(token)

                response += token
                yield f"🌐 **[Remote API — {REMOTE_MODEL}]**\n\n{response}"

            LATENCY.labels(PRODUCT_MODE, "remote").observe(time.perf_counter() - start)
            OUTPUT_TOKENS.labels(PRODUCT_MODE, "remote").inc(n_chunks)
            REQUESTS.labels(PRODUCT_MODE, "remote", "success").inc()
            return
        except Exception as exc:
            _record_error("remote", exc)
            print(f"[FAILOVER] Remote API unavailable ({exc!r}); falling back to local model")

        FAILOVERS.labels(PRODUCT_MODE).inc()
        response = _run_local(messages, max_tokens, temperature, top_p)
        yield (
            "⚠️ **Remote API unavailable — automatically switched to the local model.**\n\n"
            f"🖥️ **[Local Model — {LOCAL_MODEL}, fallback]**\n\n{response}"
        )


def build_demo():
    chatbot = gr.ChatInterface(
        fn=respond,
        additional_inputs=[
            gr.Textbox(
                value=(
                    "You are a friendly, patient Blackjack tutor. For every hand, state the "
                    "mathematically optimal basic-strategy play (hit, stand, double down, split, "
                    "or surrender) and explain *why* it's correct in terms of the dealer's "
                    "up-card and the house edge. Adapt your explanations to the player's stated "
                    "skill level, and encourage responsible bankroll management."
                ),
                label="System message",
            ),
            gr.Slider(
                minimum=1,
                maximum=2048,
                value=512,
                step=1,
                label="Max new tokens",
            ),
            gr.Slider(
                minimum=0.1,
                maximum=2.0,
                value=0.7,
                step=0.1,
                label="Temperature",
            ),
            gr.Slider(
                minimum=0.1,
                maximum=1.0,
                value=0.95,
                step=0.05,
                label="Top-p (nucleus sampling)",
            ),
            gr.Checkbox(
                label="Use Local Model",
                value=PRODUCT_MODE == "local",
            ),
            gr.Checkbox(
                label="Simulate Remote Outage (Demo Failover)",
                value=False,
            ),
        ],
    )

    with gr.Blocks(css=fancy_css) as demo:
        with gr.Sidebar():
            gr.LoginButton()

        gr.Markdown(
            "# ♠️ Blackjack Tutor ♥️",
            elem_id="app-title",
        )

        gr.Markdown(
            "Master basic strategy, one hand at a time.",
            elem_id="app-subtitle",
        )

        with gr.Column(elem_id="chat-container"):
            chatbot.render()

            gr.Markdown(
                "By default, requests go to the remote API and automatically fail over to the "
                "local model if it's unavailable. Use **Additional inputs** to force the local "
                "model, or simulate a remote outage to see the failover in action.",
                elem_id="model-note",
            )

        if pipe is not None:
            gr.api(generate_on_this_container, api_name="local_generate")

    return demo


if __name__ == "__main__":
    start_http_server(METRICS_PORT)
    print(f"[START] product={PRODUCT_MODE} metrics=:{METRICS_PORT}", flush=True)
    build_demo().launch(server_name="0.0.0.0", server_port=APP_PORT)
