#!/usr/bin/env python3
"""Dedicated ask worker: Hermes AIAgent with tools/memory off, isolated HERMES_HOME."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Constructor flags must stay in lockstep with app.ask.isolation.ISOLATED_AGENT_KWARGS.
# Hermes model_tools: enabled_toolsets is None => all tools; [] => none.


def parse_model_output(raw: str, source_handles: list[str]) -> dict:
    """Parse structured model JSON. Never treat provided sources as cited."""
    allowed = [handle for handle in source_handles if isinstance(handle, str)]
    allowed_set = set(allowed)
    text = ""
    cited: list[str] = []
    payload = _extract_json_object(raw)
    if isinstance(payload, dict):
        value = payload.get("text")
        if isinstance(value, str):
            text = value
        raw_cited = payload.get("cited_handles")
        if isinstance(raw_cited, list):
            for handle in raw_cited:
                if isinstance(handle, str) and handle in allowed_set and handle not in cited:
                    cited.append(handle)
    elif isinstance(raw, str):
        text = raw
    return {"text": text, "cited_handles": cited, "source_handles": allowed}


def _extract_json_object(raw: str) -> dict | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = []
        for line in lines[1:]:
            if line.strip().startswith("```"):
                break
            inner.append(line)
        text = "\n".join(inner).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def read_inference(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"fail closed: inference file: {error}") from error
    if not isinstance(data, dict):
        raise SystemExit("fail closed: inference file must be a JSON object")
    return data


def inspect_sentinel(path: str) -> bool:
    """Attempt the read. Do not skip based on a confinement helper."""
    if not path:
        return False
    try:
        Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return True


def assert_agent_runtime(agent) -> None:
    tools = getattr(agent, "tools", None)
    if tools:
        raise SystemExit("fail closed: AIAgent tools are not empty")
    names = getattr(agent, "valid_tool_names", None)
    if names:
        raise SystemExit("fail closed: AIAgent tool names are not empty")
    if getattr(agent, "_memory_manager", None) is not None:
        raise SystemExit("fail closed: external memory provider is active")
    if getattr(agent, "_memory_store", None) is not None:
        raise SystemExit("fail closed: memory store is active")
    chat = getattr(agent, "chat", None)
    if not callable(chat):
        raise SystemExit("fail closed: AIAgent.chat missing")


def _assert_isolated(job_path: Path) -> None:
    if os.environ.get("HERMES_BROKER_TOKEN"):
        raise SystemExit("fail closed: broker token visible")
    hermes_home = os.environ.get("HERMES_HOME", "")
    home = os.environ.get("HOME", "")
    if not hermes_home or not Path(hermes_home).is_absolute():
        raise SystemExit("fail closed: HERMES_HOME")
    expected_home = str(Path(hermes_home) / "home")
    if home != expected_home:
        raise SystemExit("fail closed: HOME must be the isolated profile home")
    if os.environ.get("HERMES_REAL_HOME"):
        raise SystemExit("fail closed: HERMES_REAL_HOME leaks owner home")
    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("fail closed: owner provider env visible")
    if not job_path.is_file():
        raise SystemExit("fail closed: job missing")


def _prompt(job: dict) -> str:
    style = Path("/opt/ask-worker/style/SOUL.md")
    if not style.is_file():
        style = Path(os.environ["HERMES_HOME"]) / "SOUL.md"
    voice = style.read_text(encoding="utf-8") if style.is_file() else ""
    handles = [item["handle"] for item in job.get("envelope") or [] if isinstance(item, dict) and "handle" in item]
    lines = [voice.strip(), "", "Authorized excerpts for this turn only:", f"source_handles={json.dumps(handles)}"]
    for item in job.get("envelope") or []:
        lines.append(f"- handle={item['handle']} revision={item.get('source_revision', '')}")
        lines.append(item.get("text", ""))
    lines.append("Return the JSON object described above. Cite only listed source_handles.")
    return "\n".join(lines)


def _user_message(job: dict) -> str:
    prior = job.get("prior_user_turns") or []
    parts = []
    for turn in prior:
        if isinstance(turn, str) and turn.strip():
            parts.append(f"Previous question: {turn.strip()}")
    parts.append(job["query"])
    return "\n".join(parts)


def _capped_chat(agent, message: str, limit: int) -> str:
    chunks: list[str] = []
    used = 0

    def on_delta(delta) -> None:
        nonlocal used
        if not isinstance(delta, str) or used >= limit:
            return
        piece = delta[: limit - used]
        chunks.append(piece)
        used += len(piece)

    text = agent.chat(message, stream_callback=on_delta)
    if not isinstance(text, str):
        raise SystemExit(f"fail closed: chat returned {type(text).__name__}, not str")
    if chunks:
        return "".join(chunks)[:limit]
    return text[:limit]


def _generate(job: dict, inference: dict) -> dict:
    source_handles = [item["handle"] for item in job.get("envelope") or [] if isinstance(item, dict) and "handle" in item]
    limit = int((job.get("budget") or {}).get("max_output_chars") or 8000)
    timeout = int((job.get("budget") or {}).get("timeout_seconds") or 30)
    if inference.get("transport") == "stub":
        cited = inference.get("stub_cited_handles")
        if not isinstance(cited, list):
            cited = []
        text = inference.get("stub_text")
        if not isinstance(text, str):
            text = json.dumps({"text": "stub", "cited_handles": cited})
        parsed = parse_model_output(text[:limit], source_handles)
        parsed["text"] = parsed["text"][:limit]
        return parsed
    from run_agent import AIAgent

    model = inference.get("model")
    api_key = inference.get("api_key")
    base_url = inference.get("base_url")
    if not isinstance(model, str) or not model.strip():
        raise SystemExit("fail closed: inference model missing")
    if not isinstance(api_key, str) or not api_key.strip():
        raise SystemExit("fail closed: inference api_key missing")
    if not isinstance(base_url, str) or not base_url.strip():
        raise SystemExit("fail closed: inference base_url missing")
    agent = AIAgent(
        model=model.strip(),
        api_key=api_key.strip(),
        base_url=base_url.strip(),
        enabled_toolsets=[],
        skip_memory=True,
        skip_context_files=True,
        load_soul_identity=False,
        quiet_mode=True,
        max_iterations=1,
        save_trajectories=False,
        skip_background_review=True,
        ephemeral_system_prompt=_prompt(job),
        max_tokens=min(4096, max(16, limit // 2)),
        run_budget_seconds=float(timeout),
    )
    agent._disable_streaming = False
    assert_agent_runtime(agent)
    raw = _capped_chat(agent, _user_message(job), limit)
    parsed = parse_model_output(raw, source_handles)
    parsed["text"] = parsed["text"][:limit]
    return parsed


def _inspection() -> dict:
    hermes_home = Path(os.environ.get("HERMES_HOME", "")).resolve()
    home = Path(os.environ.get("HOME", "")).resolve()
    sentinel = os.environ.get("ASK_OWNER_SENTINEL", "")
    return {
        "owner_sentinel_readable": inspect_sentinel(sentinel),
        "home": str(home),
        "hermes_home": str(hermes_home),
        "broker_token_present": bool(os.environ.get("HERMES_BROKER_TOKEN")),
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: worker_main.py /job.json")
    job_path = Path(sys.argv[1])
    _assert_isolated(job_path)
    inference_path = Path(os.environ.get("ASK_INFERENCE", "/inference.json"))
    if not inference_path.is_file():
        raise SystemExit("fail closed: inference file missing")
    inference = read_inference(inference_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if not isinstance(job, dict) or not job.get("envelope"):
        raise SystemExit("fail closed: empty envelope must not reach the worker")
    result = _generate(job, inference)
    if inference.get("inspect"):
        result["inspection"] = _inspection()
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
