"""Honest cost per question (owner: P1): local GPU time and hosted-API tokens, priced side by side.

Stoa scores cost (lower is better), so every answer carries `Answer.cost` and the eval prices it here:

    usd = gpu_usd + api_usd + cpu_usd
    gpu_usd = billed GPU seconds x $STOA_GPU_USD_PER_HOUR / 3600   (default $2.00/h = one 96 GB GPU, cloud rental)
    api_usd = hosted tokens x the model's list price               (per-model table below; set at call time)
    cpu_usd = core seconds x $STOA_CPU_USD_PER_HOUR / 3600          (default $0.05 per core-hour)
    core seconds = wall seconds (this process, one core) + cpu_seconds x $STOA_SERVICE_CORES (default 4.7)

`cpu_seconds` is the wall time a multi-threaded CPU service worked for the question: the PP-OCRv6 engine records its
wall time at STOA_OCR_THREADS = 8 threads (stoa.ocr.text_layer, E1's area-table crops); the door detector's clients can
call record_service(seconds, threads). Until 20:25 Sat it was recorded but NOT priced (audit 19:51: '$1.49/1k' with
scans far above). Measured 20:25 (P1, /usr/bin/time, load 54-95): one cold OCR of a 6000x3979 px scan = 339 s wall,
1,594 CPU-s (470 %), 5.4 GB RAM -> 4.7 busy cores per recorded second = ~$0.022 per scanned sheet, more than the GPU
work of the same question. Wall time stretches with machine load, so treat CPU $ as +-2x.

Local GPU time is an amortised estimate from tokens (stoa.llm.gpu_seconds_estimate: prefill 25k tok/s,
decode 1k tok/s on the shared vLLM), not HTTP latency, because latency includes queueing behind other users.
Hosted calls are priced per model when stoa.llm records `api_usd` on the call; rows written before that (or by a
solver that reports only tokens) fall back to the generic hosted rate.

Every rate can be overridden from the environment, so a reader can re-price a run with their own numbers:
    STOA_GPU_USD_PER_HOUR=1.20 python -m stoa.eval ...            # a cheaper GPU
    STOA_API_PRICES='{"gemini-3-flash-preview": [0.5, 3.0]}'       # $ per 1M tokens in / out
"""

from __future__ import annotations

import json
import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


GPU_USD_PER_HOUR = _f("STOA_GPU_USD_PER_HOUR", 2.00)  # one 96 GB GPU equivalent (captain, P1 prompt)
CPU_USD_PER_HOUR = _f("STOA_CPU_USD_PER_HOUR", 0.05)  # per core-hour (cloud vCPU + its RAM, on demand)
SERVICE_THREADS = _f("STOA_OCR_THREADS", 8)  # nominal threads behind one recorded cpu_second (the OCR engine's)
SERVICE_CORES = _f("STOA_SERVICE_CORES", 4.7)  # cores actually busy per recorded cpu_second (measured 20:25, above)

# $ per 1M tokens (input, output), paid-tier list prices for prompts <= 200k tokens. Gemini figures checked on
# 14 Nov 2026 against public price aggregators (Google's own page did not render for us): Gemini output prices include
# thinking tokens, which stoa.llm counts as output. Nemotron on NVIDIA's API is credit-based (no list price), so it
# keeps the generic hosted-VLM assumption the eval used all night.
API_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3-pro-preview": (2.00, 12.00),
    "gemini-3.1-flash-lite-preview": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "nvidia/nemotron-3.5-super-vl-120b-a12b": (0.40, 1.60),
    "google/gemma-3-12b-it": (0.10, 0.20),
    # Verda Inference (V1 17:46, backend 'verda'): Verda publishes no per-token price we could find; ASSUMED vendor list
    # prices (DeepSeek official for V4.1 Flash; Moonshot for Kimi K3.1 Pro, which bills ~97 reasoning tokens per call)
    "deepseek-4-1-flash-1017": (0.30, 1.20),
    "kimi-k3-1-pro": (2.00, 10.00),
}
DEFAULT_API_PRICE = (0.40, 1.60)  # unknown hosted model: the night's generic assumption
try:
    API_PRICES.update({k: (float(v[0]), float(v[1])) for k, v in json.loads(os.environ.get("STOA_API_PRICES", "{}")).items()})
except (ValueError, TypeError, IndexError):
    pass

# Tokens only (no GPU seconds reported): a local call priced as if it were a cheap hosted one (legacy rows).
LOCAL_USD_PER_MTOK = (0.05, 0.20)

# Local-LLM GPU time from tokens at the shared server's measured throughput (same constants as stoa.llm).
PREFILL_TPS = _f("LLM_PREFILL_TPS", 25_000)
DECODE_TPS = _f("LLM_DECODE_TPS", 1_000)


def api_price(model: str | None) -> tuple[float, float]:
    """($/1M in, $/1M out) for a hosted model id; prefixes like 'models/' are ignored."""
    m = (model or "").removeprefix("models/")
    if m in API_PRICES:
        return API_PRICES[m]
    for k, v in API_PRICES.items():  # e.g. 'gemini-3-flash-preview-11-2026' -> its family's price
        if m.startswith(k):
            return v
    return DEFAULT_API_PRICE


def api_usd(model: str | None, tokens_in: int, tokens_out: int) -> float:
    pin, pout = api_price(model)
    return ((tokens_in or 0) * pin + (tokens_out or 0) * pout) / 1e6


def _g(c: dict, k: str) -> float:
    v = c.get(k, 0)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def gpu_seconds_billed(c: dict) -> float:
    """GPU seconds we charge. Solvers that time local LLM calls by HTTP latency include queueing on the
    shared server (00:30: 30.9 s reported for a 1.6k-in/205-out call that needs ~0.27 s). So when a row has
    local LLM tokens, charge min(reported, token estimate); GPU work without tokens is charged as reported."""
    gpu = _g(c, "gpu_seconds")
    local_in = max(0, _g(c, "llm_tokens_in") - _g(c, "api_tokens_in"))
    local_out = max(0, _g(c, "llm_tokens_out") - _g(c, "api_tokens_out"))
    if (local_in or local_out) and gpu:
        gpu = min(gpu, local_in / PREFILL_TPS + local_out / DECODE_TPS)
    return gpu


def breakdown(c: dict) -> dict:
    """{'gpu_usd', 'api_usd', 'cpu_usd', 'usd'} for one question's cost dict (Answer.cost.model_dump())."""
    gpu = gpu_seconds_billed(c) * GPU_USD_PER_HOUR / 3600
    if _g(c, "api_usd"):  # priced per model when the call was made (stoa.llm)
        api = _g(c, "api_usd")
    else:
        api = (_g(c, "api_tokens_in") * DEFAULT_API_PRICE[0] + _g(c, "api_tokens_out") * DEFAULT_API_PRICE[1]) / 1e6
    if not _g(c, "gpu_seconds"):  # local tokens without GPU seconds (old rows): token-priced
        local_in = max(0, _g(c, "llm_tokens_in") - _g(c, "api_tokens_in"))
        local_out = max(0, _g(c, "llm_tokens_out") - _g(c, "api_tokens_out"))
        gpu += (local_in * LOCAL_USD_PER_MTOK[0] + local_out * LOCAL_USD_PER_MTOK[1]) / 1e6
    cpu = core_seconds(c) * CPU_USD_PER_HOUR / 3600
    return {"gpu_usd": gpu, "api_usd": api, "cpu_usd": cpu, "usd": gpu + api + cpu}


def service_core_seconds(c: dict) -> float:
    """Core seconds of the CPU services (OCR engine, detector) this question used."""
    return _g(c, "cpu_seconds") * SERVICE_CORES


def core_seconds(c: dict) -> float:
    """All CPU work we charge: this process at one core for the wall time + the services' cores."""
    return _g(c, "wall_seconds") + service_core_seconds(c)


def record_cpu(cpu_s: float) -> None:
    """Charge CPU time a service MEASURED (O1 20:58: stoa.ocr.engine.read returns 'cpu_s' = time.process_time() over all
    its threads): recorded in the cpu_seconds unit so the price is exactly cpu_s core-seconds, whatever the load.
    Use it instead of record_cost(cpu_seconds=<wall seconds>) when the reply has cpu_s."""
    try:
        sec = float(cpu_s or 0) / SERVICE_CORES
    except (TypeError, ValueError):
        return
    if sec > 0:
        from stoa.contract import record_cost

        record_cost(cpu_seconds=round(sec, 3))


def record_service(seconds: float, threads: float = SERVICE_THREADS) -> None:
    """Charge a CPU service's busy time to the current question, e.g. the door detector's reply
    `record_service(res.get('infer_seconds') or 0, threads=16)` (DET_THREADS 16; 0 on a cache hit). No-op outside a meter.
    Recorded in the OCR engine's unit (seconds at SERVICE_THREADS threads)."""
    try:
        sec = float(seconds or 0) * float(threads) / SERVICE_THREADS
    except (TypeError, ValueError):
        return
    if sec > 0:
        from stoa.contract import record_cost

        record_cost(cpu_seconds=round(sec, 3))


def cost_usd(c: dict) -> float:
    return breakdown(c)["usd"]


def rates() -> dict:
    """The prices in force, written into every eval summary so runs stay comparable."""
    return {"gpu_usd_per_hour": GPU_USD_PER_HOUR, "cpu_usd_per_hour": CPU_USD_PER_HOUR, "service_cores": SERVICE_CORES,
            "api_default_usd_per_mtok": list(DEFAULT_API_PRICE),
            "api_usd_per_mtok": {k: list(v) for k, v in API_PRICES.items()},
            "gpu_seconds_from_tokens": {"prefill_tps": PREFILL_TPS, "decode_tps": DECODE_TPS}}


def describe(c: dict) -> str:
    """One line for a person: '$0.00012 = GPU $0.00010 (0.18 s) + API $0.00000 (0 calls) + CPU $0.00002'."""
    b = breakdown(c)
    return (f"${b['usd']:.5f} = GPU ${b['gpu_usd']:.5f} ({gpu_seconds_billed(c):.2f} s at ${GPU_USD_PER_HOUR:.2f}/h)"
            f" + API ${b['api_usd']:.5f} ({int(_g(c, 'api_calls'))} calls) + CPU ${b['cpu_usd']:.5f}"
            f" ({core_seconds(c):.0f} core-s)")


# ------------------------------------------------------------------------------------------- cost tier (badge)
# Stoa judges cost on 3 tiers, not in dollars (captain, 14 Nov 12:40). One badge per answer:
#   cheap       no AI model: read from the file's geometry, IFC model, text layer or a CPU vision model
#   in-between  a few calls to a small model (our local 8B / LoRA, or a cheap hosted model like Gemini Flash)
#   expensive   a large hosted model (Gemini Pro class, Nemotron 120B) or a lot of model work (OCR of a whole scan,
#               an agent loop): > TIER_TOKENS tokens, > TIER_CALLS calls, or > TIER_API_USD of hosted calls
TIER_TOKENS = int(_f("STOA_TIER_TOKENS", 30_000))
TIER_CALLS = int(_f("STOA_TIER_CALLS", 6))
TIER_API_USD = _f("STOA_TIER_API_USD", 0.003)  # one Gemini 3.1 Pro count = $0.0074; one Flash count = $0.0008
# CPU services (OCR of a scan, the door detector): cheap up to 10 core-minutes (~$0.008, e.g. OCR of a table crop or a
# small sheet), in-between above (one cold OCR of a 6000 px scan = 27 core-minutes), expensive above 80 core-minutes.
TIER_CPU_CORE_S = _f("STOA_TIER_CPU_CORE_S", 600)
TIER_CPU_CORE_S_HIGH = _f("STOA_TIER_CPU_CORE_S_HIGH", 4800)
TIER_LABELS = {"cheap": "Cheap", "in-between": "In-between", "expensive": "Expensive"}
_ORDER = ("cheap", "in-between", "expensive")


def tier(c: dict) -> dict:
    """{'tier': 'cheap' | 'in-between' | 'expensive', 'label', 'why'} for one answer's cost dict."""
    calls, tokens = int(_g(c, "llm_calls")), int(_g(c, "llm_tokens_in") + _g(c, "llm_tokens_out"))
    api = breakdown(c)["api_usd"]
    hosted = int(_g(c, "api_calls"))
    if calls == 0 and tokens == 0 and not _g(c, "gpu_seconds"):
        t, why = "cheap", "no AI model used (read from the file's geometry, model or text, or a typical-answer prior)"
    elif tokens > TIER_TOKENS or calls > TIER_CALLS or api > TIER_API_USD:
        bits = []
        if api > TIER_API_USD:
            bits.append(f"large hosted model (${api:.4f})")
        if tokens > TIER_TOKENS:
            bits.append(f"{tokens:,} model tokens")
        if calls > TIER_CALLS:
            bits.append(f"{calls} model calls")
        t, why = "expensive", "; ".join(bits)
    else:
        who = f"{hosted} hosted" if hosted else "our own GPU"
        t, why = "in-between", f"{calls} small-model call{'s' if calls != 1 else ''} ({who}), {tokens:,} tokens"
    svc = service_core_seconds(c)
    ct = "expensive" if svc > TIER_CPU_CORE_S_HIGH else "in-between" if svc > TIER_CPU_CORE_S else "cheap"
    if svc > TIER_CPU_CORE_S:
        cpu_why = f"text recognition / detection on our CPU ({svc / 60:.0f} core-minutes)"
        if _ORDER.index(ct) > _ORDER.index(t):
            t, why = ct, cpu_why
        else:
            why = f"{why}; {cpu_why}"
    return {"tier": t, "label": TIER_LABELS[t], "why": why}
