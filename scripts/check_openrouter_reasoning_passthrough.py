"""Quick check that OpenRouter preserves Claude reasoning_details across turns.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python scripts/check_openrouter_reasoning_passthrough.py

Flow:
    1. Send a reasoning-heavy prompt to anthropic/claude-opus-4.6 with
       `reasoning: {effort: high, exclude: false}` so we get back
       `reasoning` / `reasoning_details` on the assistant message.
    2. Print what came back (presence and lengths of each reasoning field).
    3. Build the next request: keep the prior assistant message verbatim
       (including `reasoning`, `reasoning_content`, `reasoning_details`)
       and append a follow-up user turn.
    4. Send it again and print whether the API accepted the echoed
       reasoning blocks (HTTP 200 + non-empty content).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import requests

MODEL = os.environ.get("OUROBOROS_CHECK_MODEL", "anthropic/claude-opus-4.6")
EFFORT = os.environ.get("OUROBOROS_CHECK_EFFORT", "high")
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_details")


def call(messages: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 1024,
        "reasoning": {"effort": EFFORT, "exclude": False},
        "provider": {"require_parameters": True},
    }
    resp = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ouroboros.local/check",
            "X-Title": "ouroboros reasoning passthrough check",
        },
        data=json.dumps(body),
        timeout=180,
    )
    if resp.status_code != 200:
        print(f"[!] HTTP {resp.status_code}: {resp.text[:1000]}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def summarize(msg: Dict[str, Any]) -> Dict[str, Any]:
    details = msg.get("reasoning_details") or []
    detail_types: List[str] = []
    encrypted = 0
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        t = str(d.get("type") or "")
        detail_types.append(t)
        if t == "reasoning.encrypted" or d.get("data") or d.get("signature"):
            encrypted += 1
    return {
        "content_chars": len(str(msg.get("content") or "")),
        "reasoning_chars": len(str(msg.get("reasoning") or "")),
        "reasoning_content_chars": len(str(msg.get("reasoning_content") or "")),
        "reasoning_details_count": len(details) if isinstance(details, list) else 0,
        "reasoning_detail_types": detail_types,
        "encrypted_blocks": encrypted,
    }


def carry_assistant(msg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    for k in REASONING_KEYS:
        if msg.get(k) is not None:
            out[k] = msg[k]
    return out


def main() -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2

    print(f"Model:  {MODEL}")
    print(f"Effort: {EFFORT}\n")

    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Think step by step. A farmer has 17 sheep. All but 9 run away. "
                "How many are left? Then briefly explain why the answer is "
                "counter-intuitive."
            ),
        }
    ]

    print("--- turn 1: initial request ---")
    r1 = call(messages, api_key)
    msg1 = r1["choices"][0]["message"]
    s1 = summarize(msg1)
    print(json.dumps(s1, ensure_ascii=False, indent=2))
    print(f"\nassistant: {str(msg1.get('content') or '')[:300]}")

    if not s1["reasoning_details_count"] and not s1["reasoning_chars"]:
        print("\n[!] No reasoning fields came back. Provider may not be returning them.")
        return 1

    messages.append(carry_assistant(msg1))
    messages.append(
        {
            "role": "user",
            "content": "Now restate the answer in one short sentence.",
        }
    )

    echoed = {k: (k in messages[-2]) for k in REASONING_KEYS}
    print(f"\n--- turn 2: echoing reasoning back -> {echoed} ---")

    r2 = call(messages, api_key)
    msg2 = r2["choices"][0]["message"]
    s2 = summarize(msg2)
    print(json.dumps(s2, ensure_ascii=False, indent=2))
    print(f"\nassistant: {str(msg2.get('content') or '')[:300]}")

    ok = bool(msg2.get("content")) and any(echoed.values())
    print("\nRESULT:", "OK — OpenRouter accepted echoed reasoning_details" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
