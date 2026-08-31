"""Run repeatable live acceptance checks against the local Weix service.

The script deliberately sends through ``/api/messages/send`` so the same
delivery log and pending-database verification path used by the management UI
is exercised.  It is read-only unless ``--live`` is supplied.

Examples (PowerShell):

    .\\venv\\Scripts\\python.exe scripts\\windows_uia_acceptance.py \\
        --live --password admin123 \\
        --private-name 芙莉叶 --private-id wxid_ybtkerfcizd422 \\
        --group-name 0op --group-id 57971185895@chatroom
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows UIA live acceptance checks")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.getenv("WEIX_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password", default=os.getenv("WEIX_ADMIN_PASSWORD", ""))
    parser.add_argument("--private-name", default="")
    parser.add_argument("--private-id", default="")
    parser.add_argument("--group-name", default="")
    parser.add_argument("--group-id", default="")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--verify-timeout", type=float, default=12.0)
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually send test messages; without this flag only service checks run",
    )
    return parser.parse_args()


def _json_response(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected response: {payload!r}")
    return payload


async def _login(client: httpx.AsyncClient, args: argparse.Namespace) -> str:
    if not args.password:
        raise RuntimeError("请通过 --password 或 WEIX_ADMIN_PASSWORD 提供管理后台密码")
    payload = _json_response(
        await client.post(
            "/api/auth/login",
            json={"username": args.username, "password": args.password},
        )
    )
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError(f"登录响应没有 access_token: {payload}")
    return token


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    **params: Any,
) -> dict[str, Any]:
    return _json_response(await client.get(path, headers=headers, params=params))


def _input_state() -> dict[str, int]:
    """Read global Windows input state without moving or focusing anything."""
    from app.core.sender_windows_uia import WindowsUIASender

    return WindowsUIASender._foreground_input_state()


def _state_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    return {
        "cursor_unchanged": (
            before.get("cursor_x") == after.get("cursor_x")
            and before.get("cursor_y") == after.get("cursor_y")
        ),
        "foreground_unchanged": before.get("foreground_hwnd") == after.get("foreground_hwnd"),
        "focus_unchanged": before.get("focus_hwnd") == after.get("focus_hwnd"),
        "before": before,
        "after": after,
    }


async def _find_attempt(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    attempt_id: str,
    target_id: str,
    is_group: bool,
    timeout: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.5, timeout)
    query_key = "room_id" if is_group else "user_id"
    latest: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = await _get_json(
            client,
            "/api/messages",
            headers,
            **{query_key: target_id, "direction": "outbound", "size": 100},
        )
        for item in payload.get("items", []):
            if str(item.get("attempt_id") or "") == attempt_id:
                latest = item
                if str(item.get("status") or "") not in {"generated", "sending", "pending_verify"}:
                    return item
                break
        await asyncio.sleep(0.5)
    return latest


async def _send_one(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    args: argparse.Namespace,
    *,
    label: str,
    receiver: str,
    target_id: str,
    is_group: bool,
    index: int,
) -> dict[str, Any]:
    content = f"[Weix UIA验收] {label} {index:02d} {datetime.now().isoformat(timespec='seconds')}"
    before = _input_state()
    started = time.time()
    response_payload = _json_response(
        await client.post(
            "/api/messages/send",
            headers=headers,
            json={
                "msg": content,
                "receiver": receiver,
                "target_id": target_id,
                "target_name": receiver,
                "is_group": is_group,
            },
        )
    )
    after = _input_state()
    attempt_id = str(response_payload.get("attempt_id") or "")
    final_log = await _find_attempt(
        client,
        headers,
        attempt_id,
        target_id,
        is_group,
        args.verify_timeout,
    ) if attempt_id else None
    return {
        "label": label,
        "index": index,
        "content": content,
        "target_id": target_id,
        "target_name": receiver,
        "is_group": is_group,
        "attempt_id": attempt_id,
        "elapsed_seconds": round(time.time() - started, 3),
        "api": response_payload,
        "log": final_log,
        "input_state": _state_diff(before, after),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        token = await _login(client, args)
        headers = {"Authorization": f"Bearer {token}"}
        status = await _get_json(client, "/api/platform/status", headers)
        diagnose = await _get_json(client, "/api/platform/uia/diagnose", headers)
        result: dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": args.base_url,
            "live": args.live,
            "status": status,
            "uia_diagnose": diagnose,
            "scenarios": [],
        }

        if not args.live:
            result["message"] = "未执行发送；使用 --live 才会向测试账号发消息"
            return result

        if args.count < 1 or args.count > 50:
            raise RuntimeError("--count 必须在 1 到 50 之间")
        scenarios = [
            ("private", args.private_name, args.private_id, False),
            ("group", args.group_name, args.group_id, True),
        ]
        for label, receiver, target_id, is_group in scenarios:
            if not receiver or not target_id:
                raise RuntimeError(f"{label} 场景必须同时提供名称和 ID")
            rows: list[dict[str, Any]] = []
            for index in range(1, args.count + 1):
                row = await _send_one(
                    client,
                    headers,
                    args,
                    label=label,
                    receiver=receiver,
                    target_id=target_id,
                    is_group=is_group,
                    index=index,
                )
                rows.append(row)
                if index < args.count:
                    await asyncio.sleep(max(0.0, args.delay))
            result["scenarios"].append({"name": label, "rows": rows})
        return result


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    scenarios = []
    for scenario in result.get("scenarios", []):
        rows = scenario.get("rows", [])
        sent = sum(1 for row in rows if (row.get("log") or {}).get("status") == "sent")
        cursor_ok = sum(
            1 for row in rows if (row.get("input_state") or {}).get("cursor_unchanged")
        )
        scenarios.append(
            {
                "name": scenario.get("name"),
                "total": len(rows),
                "sent": sent,
                "cursor_unchanged": cursor_ok,
                "all_sent": sent == len(rows),
                "all_cursor_unchanged": cursor_ok == len(rows),
            }
        )
    if not result.get("live"):
        return {"scenarios": scenarios, "all_passed": True, "live_checks_skipped": True}
    return {"scenarios": scenarios, "all_passed": bool(scenarios) and all(
        item["all_sent"] and item["all_cursor_unchanged"] for item in scenarios
    )}


def main() -> int:
    args = _parse_args()
    result = asyncio.run(_run(args))
    result["summary"] = _summarize(result)
    output = args.output or str(
        PROJECT_ROOT / "logs" / f"windows-uia-acceptance-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"详细结果: {output_path}")
    return 0 if (not args.live or result["summary"]["all_passed"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
