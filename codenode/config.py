"""Node configuration: validated against node.schema.json, defaults filled in."""
import copy
import json
import os
from pathlib import Path

import jsonschema

SCHEMA = json.loads((Path(__file__).parent / "node.schema.json").read_text())
DEFAULT_MODEL = "claude-sonnet-5"


def _defaults(schema):
    out = {}
    for k, sub in schema.get("properties", {}).items():
        if "default" in sub:
            out[k] = copy.deepcopy(sub["default"])
        elif sub.get("type") == "object":
            out[k] = _defaults(sub)
    return out


def load(cfg=None):
    cfg = copy.deepcopy(cfg or {})
    jsonschema.validate(cfg, SCHEMA)
    merged = _defaults(SCHEMA)
    for k, v in cfg.items():
        if isinstance(v, dict):
            merged[k].update(v)
        else:
            merged[k] = v
    merged["model"] = os.environ.get("CODENODE_MODEL") or cfg.get("model") or DEFAULT_MODEL
    if "fetch_url" in merged["tools"] and not merged["fetch"]["allow_domains"]:
        raise jsonschema.ValidationError("fetch_url is enabled but fetch.allow_domains is empty")
    return merged
