"""Structured results for message delivery and verification."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SendResult:
    """Describe every meaningful stage of a message send attempt.

    The boolean return value used by the legacy sender is intentionally kept as
    a compatibility layer. New callers should retain this object so a failed
    send can be diagnosed without guessing which stage stopped progressing.
    """

    success: bool = False
    status: str = "failed"  # sent / pending_verify / failed / skipped
    stage: str = "window"  # window / search / draft / invoke / ui_verify / db_verify
    method: str = "foreground_uia"
    action_performed: bool = False
    draft_cleared: bool = False
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
    def for_message(
        cls,
        content: str,
        target_id: str,
        method: str,
        attempt_id: str = "",
    ) -> "SendResult":
        result = cls(
            method=method,
            target_id=str(target_id or ""),
            content_hash=hashlib.sha256(str(content or "").encode("utf-8")).hexdigest(),
        )
        if attempt_id:
            result.attempt_id = str(attempt_id)
        return result

    def fail(self, stage: str, code: str, message: str, **details: Any) -> "SendResult":
        self.success = False
        self.status = "failed"
        self.stage = stage
        self.error_code = code
        self.error_message = message
        self.details.update(details)
        self.finished_at = time.time()
        return self

    def pending(self, stage: str = "db_verify", **details: Any) -> "SendResult":
        self.success = False
        self.status = "pending_verify"
        self.stage = stage
        code = details.pop("error_code", "")
        message = details.pop("error_message", "")
        if code:
            self.error_code = str(code)
        if message:
            self.error_message = str(message)
        self.details.update(details)
        self.finished_at = time.time()
        return self

    def sent(self, stage: str = "db_verify", **details: Any) -> "SendResult":
        self.success = True
        self.status = "sent"
        self.stage = stage
        self.error_code = ""
        self.error_message = ""
        self.finished_at = time.time()
        self.details.update(details)
        return self

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["success"] = bool(self.success)
        return data
