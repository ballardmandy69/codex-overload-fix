"""Change only the installed relay provider endpoint. @author lyt @date 2026-11-16"""

import argparse
import os
from pathlib import Path
import re
import tempfile
import uuid


DIRECT = "https://chatgpt.com/backend-api/codex"


def endpoint_update(content, mode, port):
    """Reject ambiguous configurations and preserve unrelated settings byte for byte."""
    text = content.decode("utf-8-sig")
    headers = list(re.finditer(r"(?m)^\s*\[[^\r\n]+\][ \t]*(?:#[^\r\n]*)?\r?$", text))
    top = text[:headers[0].start()] if headers else text
    providers = re.findall(r'^model_provider\s*=\s*"([^"]+)"\s*(?:#[^\r\n]*)?$', top, re.M)
    if providers != ["openai-retry"]:
        raise ValueError("Expected active top-level model_provider = openai-retry; config was not changed.")
    sections = [i for i, h in enumerate(headers) if h.group().strip() == "[model_providers.openai-retry]"]
    if len(sections) != 1:
        raise ValueError("Expected exactly one openai-retry provider section.")
    index = sections[0]
    start = headers[index].end()
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    section = text[start:end]
    matches = list(re.finditer(r'(?m)^base_url[ \t]*=[ \t]*"([^"\r\n]+)"', section))
    if len(matches) != 1:
        raise ValueError("Expected exactly one provider base_url.")
    match = matches[0]
    relay = "http://127.0.0.1:" + str(port)
    if match.group(1) not in (DIRECT, relay):
        raise ValueError("Unknown provider endpoint; refusing to overwrite it.")
    target = DIRECT if mode == "direct" else relay
    updated = text[:start + match.start(1)] + target + text[start + match.end(1):]
    encoding = "utf-8-sig" if content.startswith(b"\xef\xbb\xbf") else "utf-8"
    return updated.encode(encoding)


def switch_config(path, mode, port, check=False):
    """Back up before an atomic replacement; refuse a concurrent configuration edit."""
    path = Path(path).resolve(strict=True)
    original = path.read_bytes()
    updated = endpoint_update(original, mode, port)
    if check or updated == original:
        return
    backup = path.with_name(path.name + ".before-switch-" + uuid.uuid4().hex + ".bak")
    backup.write_bytes(original)
    descriptor, temporary = tempfile.mkstemp(prefix=".switch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != original:
            raise RuntimeError("Config changed concurrently; retry after the other writer finishes.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("mode", choices=("direct", "relay"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    switch_config(arguments.config, arguments.mode, arguments.port, arguments.check)
