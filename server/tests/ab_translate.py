#!/usr/bin/env python3
"""A/B test: compare translation models for Japanese manga dialogue.

Usage:
    python tests/ab_translate.py                # run all models
    python tests/ab_translate.py qwen3:8b       # run specific model(s)
"""

import json
import re
import sys
import time

import requests

OLLAMA_URL = "http://localhost:11434"

# Models to compare (tag → short label for display)
MODELS = {
    "qwen2.5vl:32b": "qwen2.5vl-32b (current)",
    "qwen3:8b": "qwen3-8b",
    "qwen3:14b": "qwen3-14b",
    "qwen3:30b-a3b-instruct-2507-q4_K_M": "qwen3-30b-instruct",
    "translategemma:12b": "translategemma-12b",
}

# Models that support think=false to disable thinking mode
THINKING_MODELS = {"qwen3:8b", "qwen3:14b"}

# Test cases: diverse manga dialogue styles
# (id, japanese_text, context_note)
TEST_CASES = [
    # --- Formal / narrative ---
    ("formal_1",
     "阿多妃は後宮を出た後、南の離宮に住まうことになった",
     "Historical court drama narration"),
    ("formal_2",
     "離宮で囲うのは珍しい対応だが全て皇帝の判断だ",
     "Political dialogue"),
    ("formal_3",
     "しかし阿多妃は上級妃を下りることが決定している",
     "Formal announcement"),

    # --- Casual / slang ---
    ("casual_1",
     "やっちまったなぁ！！",
     "Rough masculine exclamation"),
    ("casual_2",
     "テメェよくもやりやがったな．．．！！",
     "Angry threat, vulgar"),
    ("casual_3",
     "悪いけどあんたらにかまってる暇ねぇから",
     "Dismissive, colloquial"),

    # --- Action / exclamation ---
    ("action_1",
     "帆をはれ！！！",
     "Ship command, One Piece style"),
    ("action_2",
     "ナミ待て！！そんな勝手な別れは許さんぞ！！",
     "Emotional shout with character name"),
    ("action_3",
     "船を出して！！",
     "Urgent command"),

    # --- Emotional / nuanced ---
    ("emotional_1",
     "フェルン。私は全部お見通しだったんだよ。",
     "Gentle reveal, Frieren style"),
    ("emotional_2",
     "縁起でもないこと言わないでよ。",
     "Mild scolding/worry"),
    ("emotional_3",
     "泣き喚いてブチギレてたじゃん．．．",
     "Teasing observation"),

    # --- Short / SFX-adjacent ---
    ("short_1", "くっ", "Pain/frustration grunt"),
    ("short_2", "逃げたな。", "Terse observation"),
    ("short_3", "良いね", "Casual agreement"),

    # --- Complex / long ---
    ("complex_1",
     "まさかあいつ．．．我々に礼も言わせず別れも言おずに行こうというのか！？",
     "Long rhetorical question with ellipsis"),
    ("complex_2",
     "新手のゆすりですね．．．．．．締め出しをくった総会屋達が何かしてくるとは思ったけど、まさかこんな方法でくる人は思わなかった",
     "Business dialogue, complex sentence"),
]

PROMPT_TEMPLATE = (
    "Translate this Japanese manga dialogue to natural English.\n"
    "Keep it concise and suitable for a speech bubble.\n"
    "Output ONLY the English translation, nothing else.\n"
    "\nJapanese: {text}"
)


def clean_response(text: str) -> str:
    """Strip thinking tags and clean up translation output."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().strip('"').strip("'").strip()
    # Collapse multiple newlines
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def check_model_available(model: str) -> bool:
    """Check if model is pulled in Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        tags = [m["name"] for m in resp.json().get("models", [])]
        # Ollama tags can have :latest suffix
        return any(model in t or t.startswith(model) for t in tags)
    except Exception:
        return False


def unload_all_models() -> None:
    """Unload all loaded models from Ollama to free GPU memory."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        for m in resp.json().get("models", []):
            name = m["name"]
            requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=10,
            )
    except Exception:
        pass


def translate_with_model(model: str, text: str) -> tuple[str, float]:
    """Translate text with a model via chat API. Returns (translation, seconds)."""
    payload = {
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT_TEMPLATE.format(text=text)}],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024},
    }
    # Disable thinking mode for qwen3 hybrid models
    if model in THINKING_MODELS:
        payload["think"] = False

    start = time.time()
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        elapsed = time.time() - start
        return clean_response(raw), elapsed
    except Exception as e:
        elapsed = time.time() - start
        return f"[ERROR: {e}]", elapsed


def run_ab_test(models: list[str]):
    """Run all test cases one model at a time (unloading between models)."""
    results = {}  # {test_id: {model: (translation, time)}}

    for model in models:
        label = MODELS.get(model, model)
        print(f"\n{'='*80}")
        print(f"Loading {label}...")
        print(f"{'='*80}")

        # Unload previous model to free GPU memory
        unload_all_models()
        time.sleep(2)

        # Warm up (loads model into GPU)
        print(f"  Warming up {model}...", end="", flush=True)
        translate_with_model(model, "テスト")
        print(" done")

        for test_id, jp_text, context in TEST_CASES:
            if test_id not in results:
                results[test_id] = {}
            translation, elapsed = translate_with_model(model, jp_text)
            results[test_id][model] = (translation, elapsed)
            print(f"  [{test_id}] ({elapsed:5.1f}s) {translation}")

    # Side-by-side comparison
    print(f"\n\n{'='*80}")
    print("TRANSLATION COMPARISON")
    print(f"{'='*80}")
    for test_id, jp_text, context in TEST_CASES:
        print(f"\n[{test_id}] {context}")
        print(f"  JP: {jp_text}")
        for model in models:
            label = MODELS.get(model, model)
            text, elapsed = results[test_id][model]
            print(f"  {label:.<30s} ({elapsed:5.1f}s) {text}")

    # Timing summary
    print(f"\n\n{'='*80}")
    print("TIMING SUMMARY (seconds per translation)")
    print(f"{'─'*80}")
    header = f"{'Model':<30s}"
    header += f"{'Avg':>8s}{'Min':>8s}{'Max':>8s}{'Total':>8s}"
    print(header)
    print(f"{'─'*80}")

    for model in models:
        label = MODELS.get(model, model)
        times = [results[tid][model][1] for tid in results]
        avg_t = sum(times) / len(times)
        min_t = min(times)
        max_t = max(times)
        total_t = sum(times)
        print(f"{label:<30s}{avg_t:8.1f}{min_t:8.1f}{max_t:8.1f}{total_t:8.1f}")

    # Save full results as JSON
    out = {}
    for test_id, jp_text, context in TEST_CASES:
        out[test_id] = {
            "japanese": jp_text,
            "context": context,
            "translations": {
                MODELS.get(m, m): {"text": results[test_id][m][0],
                                    "time": round(results[test_id][m][1], 2)}
                for m in models
            },
        }

    outpath = "tests/ab_translate_results.json"
    with open(outpath, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\nFull results saved to {outpath}")


def main():
    # Filter to requested models or use all
    if len(sys.argv) > 1:
        requested = sys.argv[1:]
    else:
        requested = list(MODELS.keys())

    # Check availability
    available = []
    for model in requested:
        if check_model_available(model):
            available.append(model)
            print(f"  [ok] {model}")
        else:
            print(f"  [--] {model} not installed, skipping")

    if not available:
        print("No models available. Pull models with: ollama pull <model>")
        sys.exit(1)

    print(f"\nRunning A/B test with {len(available)} models, "
          f"{len(TEST_CASES)} test cases...\n")
    run_ab_test(available)


if __name__ == "__main__":
    main()
