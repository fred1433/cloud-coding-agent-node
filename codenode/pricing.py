"""Cost of a model call, recomputed from the raw `usage` block the API returns."""
import json
from pathlib import Path

_TABLE = json.loads((Path(__file__).parent / "pricing.json").read_text())


class PriceTable:
    def __init__(self, table=None):
        self.table = table or _TABLE
        self.as_of = self.table["as_of"]

    def rates(self, model):
        try:
            return self.table["per_million_tokens"][model]
        except KeyError:
            raise KeyError(f"no price for model {model!r} in the table dated {self.as_of}") from None

    def cost(self, model, usage):
        """Dollars for one call. `usage` is the API's usage object as a dict.

        input_tokens is the uncached remainder only; cache writes and reads are billed
        separately. When the API splits cache writes by TTL we price each part.
        """
        r = self.rates(model)
        cw = usage.get("cache_creation_input_tokens") or 0
        split = usage.get("cache_creation") or {}
        cw_1h = split.get("ephemeral_1h_input_tokens") or 0
        cw_5m = split.get("ephemeral_5m_input_tokens")
        if cw_5m is None:
            cw_5m = cw - cw_1h
        dollars = (
            (usage.get("input_tokens") or 0) * r["input"]
            + (usage.get("output_tokens") or 0) * r["output"]
            + cw_5m * r["cache_write_5m"]
            + cw_1h * r["cache_write_1h"]
            + (usage.get("cache_read_input_tokens") or 0) * r["cache_read"]
        ) / 1_000_000
        return round(dollars, 6)

    def worst_case_next_call(self, model, prompt_tokens, max_output_tokens):
        """Upper bound used BEFORE a call: whole prompt billed as a fresh cache write
        (the most expensive way to pay for input) plus a full max_tokens reply."""
        r = self.rates(model)
        top_input = max(r["input"], r["cache_write_5m"])
        return round((prompt_tokens * top_input + max_output_tokens * r["output"]) / 1_000_000, 6)
