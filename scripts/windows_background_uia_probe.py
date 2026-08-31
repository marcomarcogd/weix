"""Send a small number of explicitly approved background-UIA test messages.

This probe does not modify config.yaml or the running management service.  It
constructs the normal Windows sender, then overrides only the in-memory UIA
mode for the isolated process.  Background mode is fail-closed: if foreground
window, keyboard focus, or cursor state changes, the current send fails and the
probe stops without trying foreground or mouse delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.sender_windows import WindowsSender
from app.core.sender_windows_uia import WindowsUIASender


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isolated background UIA probe")
    parser.add_argument("--receiver", default="芙莉叶")
    parser.add_argument("--target-id", default="wxid_ybtkerfcizd422")
    parser.add_argument("--pid", type=int, default=0, help="已确认属于所选账号的微信主进程 PID")
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--output", default="")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    if not 1 <= args.count <= 4:
        raise ValueError("本次后台探测只允许 1 到 4 条消息")

    from app.core.platform import Platform

    platform = Platform.get()
    extractor = platform.key_extractor
    bound_pid = int(args.pid or 0)
    if bound_pid <= 0:
        raise RuntimeError("后台探测必须显式提供已确认的微信主进程 PID（--pid）")
    if not extractor.bind_process_for_cached_keys(bound_pid):
        raise RuntimeError(f"无法用缓存密钥确认微信 PID {bound_pid} 属于所选账号")

    sender = WindowsSender()
    uia = WindowsUIASender()
    # Explicitly isolate the experiment from production configuration.
    uia._send_mode = "background_uia"
    uia._background_mode = True
    uia._allow_foreground_activation = False
    uia._background_post_message = True
    uia._send_key_fallback = "none"
    uia._send_button_key_fallback = "none"
    sender._uia_sender = uia
    sender._method = "uia"
    sender._allow_mouse_fallback = False

    rows: list[dict] = []
    for index in range(1, args.count + 1):
        content = (
            f"[Weix 后台UIA验收] {index:02d} "
            f"{datetime.now().isoformat(timespec='seconds')}"
        )
        before = WindowsUIASender._foreground_input_state()
        started = time.time()
        result = await sender.send_text_result(
            content,
            args.receiver,
            is_group=False,
            target_id=args.target_id,
            attempt_id=f"background-probe-{int(started * 1000)}-{index}",
            wait_for_db_verify=True,
        )
        after = WindowsUIASender._foreground_input_state()
        invoke_guard = (
            result.details.get("background_guard", {}).get("after_invoke", {})
        )
        guard_before = invoke_guard.get("before", before)
        guard_after = invoke_guard.get("after", after)
        state = {
            "cursor_unchanged": (
                before["cursor_x"] == after["cursor_x"]
                and before["cursor_y"] == after["cursor_y"]
            ),
            "foreground_unchanged": before["foreground_hwnd"] == after["foreground_hwnd"],
            "focus_unchanged": before["focus_hwnd"] == after["focus_hwnd"],
            "sender_guard_cursor_unchanged": (
                guard_before.get("cursor_x") == guard_after.get("cursor_x")
                and guard_before.get("cursor_y") == guard_after.get("cursor_y")
            ),
            "sender_guard_foreground_unchanged": (
                guard_before.get("foreground_hwnd") == guard_after.get("foreground_hwnd")
            ),
            "sender_guard_focus_unchanged": (
                guard_before.get("focus_hwnd") == guard_after.get("focus_hwnd")
            ),
            "sender_guard": invoke_guard,
            "before": before,
            "after": after,
        }
        row = {
            "index": index,
            "content": content,
            "attempt_id": result.attempt_id,
            "elapsed_seconds": round(time.time() - started, 3),
            "result": result.as_dict(),
            "input_state": state,
        }
        rows.append(row)
        print(json.dumps({
            "index": index,
            "status": result.status,
            "stage": result.stage,
            "error_code": result.error_code,
            "draft_cleared": result.draft_cleared,
            "ui_verified": result.ui_verified,
            "db_verified": result.db_verified,
            "cursor_unchanged": state["cursor_unchanged"],
            "foreground_unchanged": state["foreground_unchanged"],
            "focus_unchanged": state["focus_unchanged"],
            "sender_guard_cursor_unchanged": state["sender_guard_cursor_unchanged"],
            "sender_guard_foreground_unchanged": state["sender_guard_foreground_unchanged"],
            "sender_guard_focus_unchanged": state["sender_guard_focus_unchanged"],
            "invoke_method": result.details.get("invoke_method", ""),
        }, ensure_ascii=False))
        if not result.success:
            print("后台发送未通过，按 fail-closed 规则停止后续测试。")
            break
        if index < args.count:
            await asyncio.sleep(max(0.0, args.delay))

    passed = bool(rows) and len(rows) == args.count and all(
        row["result"]["status"] == "sent"
        and row["result"]["db_verified"]
        and row["result"]["ui_verified"]
        and row["input_state"]["cursor_unchanged"]
        and row["input_state"]["sender_guard_cursor_unchanged"]
        and row["input_state"]["sender_guard_foreground_unchanged"]
        and row["input_state"]["sender_guard_focus_unchanged"]
        for row in rows
    )
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "background_uia",
        "background_post_message": True,
        "receiver": args.receiver,
        "target_id": args.target_id,
        "bound_pid": bound_pid,
        "requested_count": args.count,
        "rows": rows,
        "passed": passed,
    }


def main() -> int:
    args = parse_args()
    result = asyncio.run(run(args))
    output = Path(args.output) if args.output else PROJECT_ROOT / "logs" / (
        f"windows-background-uia-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "rows": len(result["rows"])}, ensure_ascii=False))
    print(f"详细结果: {output}")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
