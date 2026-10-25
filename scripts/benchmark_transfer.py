"""Offline benchmark of the current application; every HTTP request is mocked.

Run from the repository root. Results model latency, not Baidu throughput.
No real API requests, credentials, or config writes are permitted.
"""
import argparse
import json
import runpy
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.operations import Operations

make_root = runpy.run_path(str(ROOT / "tests/test_operations.py"))["make_root"]
COUNT = 40
DELAY = 0.005


def run_case(custom, passwords, prefetch, existing=0, metadata=True):
    counts = Counter()
    sessions = set()
    guard = threading.Lock()
    directories = {
        "/target": [
            {"fs_id": 100000 + i, "server_filename": f"old-{i}", "isdir": 0}
            for i in range(existing)
        ]
    }

    def request(session, method, url, **kwargs):
        endpoint = urlparse(url).path
        key = "share_page" if endpoint.startswith("/s/") else endpoint
        with guard:
            counts[key] += 1
            sessions.add(session)
        time.sleep(DELAY)
        params, data = kwargs.get("params", {}), kwargs.get("data", {})
        payload = {"errno": 0}
        if endpoint == "/api/gettemplatevariable":
            payload["result"] = {"bdstoken": "mock-token"}
        elif endpoint == "/share/verify":
            payload["randsk"] = "mock-" + params["surl"]
        elif endpoint.startswith("/s/"):
            i = int(endpoint.rsplit("item", 1)[1])
            item = {"fs_id": 1000 + i, "server_filename": f"item-{i}", "isdir": 0}
            if metadata:
                item.update(size=100, md5="a" * 32)
            payload = {"shareid": 2000 + i, "share_uk": 3000, "file_list": [item]}
        elif endpoint == "/api/create":
            directories.setdefault(data["path"], [])
            payload["path"] = data["path"]
        elif endpoint == "/api/list":
            items = directories[params["dir"]]
            page, size = int(params["page"]), int(params["num"])
            payload.update(list=items[(page - 1) * size:page * size], total=len(items))
        elif endpoint == "/share/transfer":
            i = int(params["shareid"]) - 2000
            directories[data["path"]].append({
                "fs_id": 200000 + i, "server_filename": f"item-{i}",
                "isdir": 0, "size": 100, "md5": "a" * 32,
            })
        else:
            raise AssertionError(f"Unexpected mocked endpoint: {endpoint}")
        return SimpleNamespace(
            json=lambda: payload, content=json.dumps(payload).encode(),
            status_code=200, raise_for_status=lambda: None,
        )

    links = [
        f"https://pan.baidu.com/s/1item{i:04d}"
        + (" abcd" if passwords == "all" or passwords == "mixed" and i % 2 == 0 else "")
        for i in range(COUNT)
    ]
    op = Operations(make_root("\n".join(links)))
    op.root.var_custom_mode.value = custom
    op.PREFETCH_WORKERS = prefetch
    with patch("requests.Session.request", request), patch("src.operations.write_config"):
        start = time.perf_counter()
        op.save()
        elapsed = time.perf_counter() - start
    assert op.completed_task_count == COUNT, op.root.text_logs.value
    assert op.root.text_logs.value.count("转存成功") == COUNT
    return {"seconds": elapsed, "http_requests": sum(counts.values()), "by_endpoint": dict(counts), "sessions": len(sessions)}


cases = [
    ("custom_password_prefetch0", True, "all", 0, 0, True),
    ("custom_password_prefetch2", True, "all", 2, 0, True),
    ("custom_open_prefetch0", True, "none", 0, 0, True),
    ("custom_open_prefetch2", True, "none", 2, 0, True),
    ("normal_password_empty", False, "all", 2, 0, True),
    ("normal_password_2500", False, "all", 2, 2500, True),
    ("normal_password_no_metadata", False, "all", 2, 0, False),
    ("custom_mixed_prefetch0", True, "mixed", 0, 0, True),
    ("custom_mixed_prefetch2", True, "mixed", 2, 0, True),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON result file')
    options = parser.parse_args()
    results = []
    for name, *args in cases:
        samples = [run_case(*args) for _ in range(3)]
        result = {
            "case": name,
            "median_seconds": round(statistics.median(s["seconds"] for s in samples), 4),
            "request_counts": [s["http_requests"] for s in samples],
            "sessions": samples[-1]["sessions"],
            "by_endpoint": samples[-1]["by_endpoint"],
        }
        results.append(result)
        print(json.dumps(result), flush=True)
    if options.output:
        options.output.write_text(json.dumps({
            "links": COUNT, "mock_latency_ms_per_request": DELAY * 1000,
            "repetitions": 3, "real_api_requests": 0, "results": results,
        }, indent=2) + "\n", encoding='utf-8')


if __name__ == '__main__':
    main()
