"""Core tracing, decorators, and monkeypatch helpers for benchmarking."""

from __future__ import annotations

import functools
import importlib
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


Extractor = Callable[[Any], dict[str, int] | None]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_event(trace_file: Path, event: dict[str, Any]) -> None:
    _ensure_parent(trace_file)
    with trace_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _summarize(value: Any, *, limit: int = 200) -> str:
    try:
        text = repr(value)
    except Exception:
        text = f"<unreprable {type(value).__name__}>"
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} chars truncated]"


def _extract_openai_usage(result: Any) -> dict[str, int] | None:
    usage = getattr(result, "usage", None)
    if usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if prompt_tokens is None or completion_tokens is None:
        return None
    total = total_tokens if total_tokens is not None else int(prompt_tokens) + int(completion_tokens)
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total),
    }


def _extract_anthropic_usage(result: Any) -> dict[str, int] | None:
    usage = getattr(result, "usage", None)
    if usage is None:
        input_tokens = getattr(result, "input_tokens", None)
        output_tokens = getattr(result, "output_tokens", None)
    else:
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    return {
        "prompt_tokens": int(input_tokens),
        "completion_tokens": int(output_tokens),
        "total_tokens": int(input_tokens) + int(output_tokens),
    }


def _extract_generic_dict_usage(result: Any) -> dict[str, int] | None:
    if not isinstance(result, dict):
        return None
    usage = result.get("usage", result)
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" not in usage or "completion_tokens" not in usage:
        return None
    total = usage.get("total_tokens")
    if total is None:
        total = int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
    return {
        "prompt_tokens": int(usage["prompt_tokens"]),
        "completion_tokens": int(usage["completion_tokens"]),
        "total_tokens": int(total),
    }


_EXTRACTORS: dict[str, Extractor] = {
    "openai": _extract_openai_usage,
    "anthropic": _extract_anthropic_usage,
    "generic_dict": _extract_generic_dict_usage,
}


def traced(
    name: str,
    *,
    trace_file: str | Path,
    usage_extractor: str | None = None,
    event_type: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    path = Path(trace_file)
    extractor = _EXTRACTORS.get(usage_extractor or "")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.time()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                event = {
                    "name": name,
                    "ok": False,
                    "duration_seconds": round(time.time() - started, 6),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
                if event_type:
                    event["event_type"] = event_type
                _write_event(path, event)
                raise

            usage = extractor(result) if extractor else None
            event = {
                "name": name,
                "ok": True,
                "duration_seconds": round(time.time() - started, 6),
                "result_summary": _summarize(result),
            }
            if event_type:
                event["event_type"] = event_type
            if usage is not None:
                event["usage"] = usage
            _write_event(path, event)
            return result

        return wrapper

    return decorator


@dataclass
class PatchHandle:
    owner: Any
    attr_name: str
    original: Any

    def restore(self) -> None:
        setattr(self.owner, self.attr_name, self.original)


def _resolve_target(target: str) -> tuple[Any, str, Any]:
    if ":" not in target:
        raise ValueError("patch target must use 'module.path:attr.chain' format")
    module_name, attr_chain = target.split(":", 1)
    module = importlib.import_module(module_name)
    owner: Any = module
    parts = [part for part in attr_chain.split(".") if part]
    if not parts:
        raise ValueError(f"invalid patch target: {target}")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    attr_name = parts[-1]
    return owner, attr_name, getattr(owner, attr_name)


def load_patch_specs(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        items = data.get("patches", [])
    else:
        items = data
    if not isinstance(items, list):
        raise ValueError("patch config must contain a list of patches")
    return [item for item in items if isinstance(item, dict)]


def apply_patch_specs(specs: list[dict[str, Any]], *, trace_file: str | Path) -> list[PatchHandle]:
    handles: list[PatchHandle] = []
    for spec in specs:
        target = str(spec["target"])
        name = str(spec.get("name", target))
        usage_extractor = spec.get("usage_extractor")
        event_type = spec.get("event_type")
        owner, attr_name, original = _resolve_target(target)
        wrapped = traced(
            name,
            trace_file=trace_file,
            usage_extractor=str(usage_extractor) if usage_extractor else None,
            event_type=str(event_type) if event_type else None,
        )(original)
        setattr(owner, attr_name, wrapped)
        handles.append(PatchHandle(owner=owner, attr_name=attr_name, original=original))
    return handles


def read_trace_events(path: str | Path) -> list[dict[str, Any]]:
    trace_path = Path(path)
    if not trace_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            events.append(item)
    return events
