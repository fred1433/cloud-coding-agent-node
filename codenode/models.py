"""Model backends. The loop only sees `Response` objects, so a scripted model can
drive the exact same code path as the real API (that is how the attack suite runs
without spending a token)."""
import os
from dataclasses import dataclass, field


@dataclass
class Response:
    blocks: list            # normalized dicts: {"type": "text"|"tool_use"|..., ...}
    raw_content: list       # what goes back into `messages` unchanged
    usage: dict
    stop_reason: str
    model: str
    request_id: str = ""
    extra: dict = field(default_factory=dict)


def _dump(block):
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)


class AnthropicModel:
    """Messages API with Anthropic-defined bash/editor tools, automatic prompt caching,
    and the key read from the host environment only."""

    def __init__(self, model, effort="medium", api_key_env="ANTHROPIC_API_KEY"):
        import anthropic  # imported lazily so the attack suite needs no SDK key
        key = os.environ.get(api_key_env)
        if not key:
            raise RuntimeError(f"{api_key_env} is not set on the host")
        self.client = anthropic.Anthropic(api_key=key, max_retries=0)   # retries are done and counted by the node
        self.model = model
        self.effort = effort

    def call(self, system, tools, messages, max_tokens, timeout):
        r = self.client.with_options(timeout=timeout).messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            cache_control={"type": "ephemeral"},
            output_config={"effort": self.effort},
        )
        return Response(
            blocks=[_dump(b) for b in r.content],
            raw_content=list(r.content),
            usage=r.usage.model_dump(exclude_none=True),
            stop_reason=r.stop_reason,
            model=r.model,
            request_id=getattr(r, "_request_id", "") or "",
        )


class ScriptedModel:
    """Replays a fixed list of turns. Each turn is a list of blocks, e.g.
    [{"type": "tool_use", "name": "bash", "input": {"command": "env"}}].
    Usage numbers are synthetic and deterministic."""

    def __init__(self, turns, model="claude-sonnet-5", usage=None):
        self.turns = list(turns)
        self.model = model
        self.usage = usage or {"input_tokens": 1200, "output_tokens": 150,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.calls = 0
        self.seen_messages = []

    def call(self, system, tools, messages, max_tokens, timeout):
        self.seen_messages.append(messages[-1])
        if callable(self.turns[0]) if self.turns else False:
            turn = self.turns[0](self.calls)
        else:
            turn = self.turns[self.calls] if self.calls < len(self.turns) else None
        if turn is None:
            blocks = [{"type": "text", "text": "Done."}]
        else:
            blocks = []
            for i, b in enumerate(turn):
                b = dict(b)
                if b["type"] == "tool_use":
                    b.setdefault("id", f"toolu_s{self.calls}_{i}")
                blocks.append(b)
        self.calls += 1
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return Response(blocks=blocks, raw_content=blocks, usage=dict(self.usage), stop_reason=stop,
                        model=self.model)
