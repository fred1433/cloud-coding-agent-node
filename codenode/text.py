"""Cleaning of anything that comes back from the sandbox or the web before the model
or the UI sees it: decode as UTF-8 with replacement, drop ANSI escape sequences and
other control characters, and cut to a size with an explicit marker."""
import re

_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(data, limit=20_000):
    if isinstance(data, bytes):
        text = data.decode("utf-8", errors="replace")
    else:
        text = str(data)
    text = _ANSI.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    if len(text) > limit:
        head = text[: limit * 3 // 4]
        tail = text[-limit // 4:]
        text = f"{head}\n[... {len(text) - len(head) - len(tail)} characters cut ...]\n{tail}"
    return text


def excerpt(text, limit=600):
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f" [+{len(text) - limit} chars]"
