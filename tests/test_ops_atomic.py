import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from rhbot.ops import atomic_write


def test_atomic_write_concurrent_writers_keep_a_complete_heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    barrier = Barrier(2)
    original_write = Path.write_text

    def write_then_wait(self, text, *args, **kwargs):
        result = original_write(self, text, *args, **kwargs)
        if self.name.startswith("heartbeat.json.tmp"):
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Path, "write_text", write_then_wait)
    payloads = [json.dumps({"writer": i, "data": "x" * 1000}) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(atomic_write, path, payload) for payload in payloads]
        for future in futures:
            future.result(timeout=5)

    assert path.read_text(encoding="utf-8") in payloads
    assert not list(tmp_path.glob("heartbeat.json.tmp*"))


def test_atomic_write_cleans_up_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text("previous", encoding="utf-8")

    def fail_replace(self, target):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write(path, "new")

    assert path.read_text(encoding="utf-8") == "previous"
    assert not list(tmp_path.glob("heartbeat.json.tmp*"))
