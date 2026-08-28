"""结构化记录一次消息发送尝试。"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SendResult:
    """区分发送前失败、动作已执行待确认和数据库确认成功。"""

    success: bool = False
    status: str = "failed"
    stage: str = "window"
    method: str = "auto"
    action_performed: bool = False
    ui_verified: bool = False
    db_verified: bool = False
    target_id: str = ""
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    content_hash: str = ""
    error_code: str = ""
    error_message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_message(cls, content: str, target_id: str, method: str) -> "SendResult":
        return cls(
            method=method,
            target_id=str(target_id or ""),
            content_hash=hashlib.sha256(
                str(content or "").encode("utf-8")
            ).hexdigest(),
        )

    def fail(self, stage: str, code: str, message: str, **details: Any) -> "SendResult":
        self.success = False
        self.status = "failed"
        self.stage = stage
        self.error_code = code
        self.error_message = message
        self.details.update(details)
        self.finished_at = time.time()
        return self

    def pending(self, stage: str, code: str, message: str, **details: Any) -> "SendResult":
        self.success = False
        self.status = "pending_verify"
        self.stage = stage
        self.error_code = code
        self.error_message = message
        self.details.update(details)
        self.finished_at = time.time()
        return self

    def sent(self, **details: Any) -> "SendResult":
        self.success = True
        self.status = "sent"
        self.stage = "db_verify"
        self.error_code = ""
        self.error_message = ""
        self.db_verified = True
        self.details.update(details)
        self.finished_at = time.time()
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
