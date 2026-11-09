"""Send sample chats to a product so the dashboards have data.

Runs inside a product container (gradio_client ships with gradio):
    ./run_stack.sh traffic api 30
    ./run_stack.sh traffic local 5
"""

import os
import random
import sys
import time
import urllib.error
import urllib.request

from gradio_client import Client

QUESTIONS = [
    "I have 16 and the dealer shows a 10. Hit or stand?",
    "Should I ever split 10s?",
    "When should I double down on 11?",
    "Pair of 8s against a dealer ace — what now?",
    "Soft 18 versus a dealer 9?",
    "Is insurance ever worth taking?",
    "12 against a dealer 4?",
    "What's a good bankroll for $10 tables?",
]
SYSTEM = "You are a Blackjack tutor."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def login_cookie(gateway):
    """Sign in the same way the LoginButton does and return the session cookie."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"{gateway}/login/callback?_target_url=/")
    except urllib.error.HTTPError as redirect:
        return redirect.headers["set-cookie"].split(";")[0]
    raise RuntimeError("login did not return a session cookie")


product = os.environ.get("PRODUCT_MODE", "api")
n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
gateway = os.environ.get("GATEWAY_URL", "http://gateway")
cookie = login_cookie(gateway)

for i in range(n):
    q = random.choice(QUESTIONS)
    if product == "local":
        use_local, outage, max_tokens = True, False, 128
    else:
        roll = random.random()
        use_local, outage, max_tokens = roll < 0.05, 0.05 <= roll < 0.15, 512
    t0 = time.time()
    try:
        # A fresh client per question, so each one starts a new conversation.
        client = Client(f"{gateway}/{product}/", headers={"Cookie": cookie}, verbose=False)
        answer = client.predict(q, SYSTEM, max_tokens, 0.7, 0.95, use_local, outage,
                                api_name="/respond")
    except Exception as exc:
        answer = f"ERROR {exc}"
    mode = "local" if use_local else ("outage" if outage else "remote")
    print(f"[{i + 1}/{n}] {time.time() - t0:5.1f}s {mode:6s} {q[:40]:40s} -> {str(answer)[:60]!r}",
          flush=True)
    time.sleep(random.uniform(1, 5))
