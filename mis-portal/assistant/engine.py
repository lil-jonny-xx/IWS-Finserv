"""
Jarvis reasoning core — a manual, streaming agentic loop over the Claude Messages API.

Why a manual loop (not the SDK tool runner): OUR code must enforce the entity boundary.
Claude emits tool *intent*; we execute each client tool with a shared DB connection already
scoped to `eid` via _resolve_entity. The model is never the security boundary, and it never
supplies an entity id.

The loop:
  - stream text deltas out (for SSE) as they arrive,
  - on stop_reason == "tool_use": run our client tools, append tool_result, continue,
  - on stop_reason == "pause_turn": server-side web_search paused mid-loop -> re-send to resume
    (no extra user message — the API resumes from the trailing server_tool_use),
  - on end_turn (or any terminal reason): stop.

web_search is an Anthropic-hosted server tool; results stream back inline with citations.
"""

from __future__ import annotations

import os
from typing import Iterator, Optional

from . import tools as tools_mod
from .prompts import SYSTEM_PROMPT
from .routing import choose_model, effort_for

MAX_TURNS = 8
MAX_TOKENS = 16000
WEB_SEARCH_MAX_USES = 5

_client = None


def get_client():
    """Lazily build the Anthropic client (reads ANTHROPIC_API_KEY from the env/.env)."""
    global _client
    if _client is None:
        import anthropic  # imported lazily so the module loads even if SDK is absent
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic()
    return _client


def _api_tools() -> list:
    return tools_mod.TOOL_SCHEMAS + [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}
    ]


def _build_messages(history: list[dict], user_text: str) -> list[dict]:
    """Replay prior turns as plain text (user/assistant), then append the new user message."""
    messages = []
    for m in history:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _extract_citations(content_blocks) -> list[dict]:
    """Pull web_search citations off text blocks (url + title), de-duplicated by url."""
    out = []
    seen = set()
    for block in content_blocks:
        if getattr(block, "type", None) != "text":
            continue
        for cit in (getattr(block, "citations", None) or []):
            url = getattr(cit, "url", None)
            if url and url not in seen:
                seen.add(url)
                out.append({"url": url, "title": getattr(cit, "title", None)})
    return out


def run_stream(history: list[dict], user_text: str, eid: Optional[int],
               conn) -> Iterator[dict]:
    """
    Drive one assistant turn. Yields event dicts:
      {"type": "text", "text": str}        incremental answer text
      {"type": "tool", "name": str}        a tool is being run (for UI affordance)
      {"type": "citations", "items": [...]} citations discovered so far
      {"type": "done", "content": str, "citations": [...], "tool_names": [...]}
      {"type": "error", "message": str}
    The caller persists the final `content`/`citations` and serialises events as SSE.
    """
    try:
        client = get_client()
    except Exception as e:  # missing key / SDK -> surface cleanly to the endpoint
        yield {"type": "error", "message": str(e)}
        return

    messages = _build_messages(history, user_text)
    api_tools = _api_tools()
    # Pick the model once for this user turn; the agentic loop reuses it across
    # internal tool turns so the cached prefix and reasoning stay consistent.
    model = choose_model(user_text, history)
    effort = effort_for(model)
    full_text_parts: list[str] = []
    all_citations: list[dict] = []
    tool_names: list[str] = []

    try:
        for _turn in range(MAX_TURNS):
            with client.messages.stream(
                model=model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=api_tools,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                messages=messages,
            ) as stream:
                for event in stream:
                    if (event.type == "content_block_delta"
                            and getattr(event.delta, "type", None) == "text_delta"):
                        text = event.delta.text
                        full_text_parts.append(text)
                        yield {"type": "text", "text": text}
                final = stream.get_final_message()

            messages.append({"role": "assistant", "content": final.content})

            new_cites = _extract_citations(final.content)
            if new_cites:
                fresh = [c for c in new_cites if c not in all_citations]
                all_citations.extend(fresh)
                if fresh:
                    yield {"type": "citations", "items": all_citations}

            stop = final.stop_reason

            if stop == "tool_use":
                tool_results = []
                for block in final.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_names.append(block.name)
                    yield {"type": "tool", "name": block.name}
                    try:
                        result = tools_mod.dispatch(block.name, dict(block.input), conn, eid)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                    except Exception as te:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"tool error: {te}",
                            "is_error": True,
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            if stop == "pause_turn":
                # server-side web_search paused; re-send to let the API resume
                continue

            break  # end_turn / max_tokens / refusal — done

        yield {
            "type": "done",
            "content": "".join(full_text_parts).strip(),
            "citations": all_citations,
            "tool_names": tool_names,
            "model": model,
        }
    except Exception as e:
        yield {"type": "error", "message": f"engine error: {e}"}
