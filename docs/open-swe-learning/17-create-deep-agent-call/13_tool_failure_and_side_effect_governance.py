"""第 3 章最小验证：参数清洗、重试判定和 PR shell fallback 阻断。"""

from __future__ import annotations

import json

from agent.middleware.pr_creation_guard import is_pr_creation_fallback_command
from agent.middleware.sanitize_tool_inputs import _sanitize_read_file_args
from agent.middleware.task_retry import task_on_failure, task_retry_on


class HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class InvalidPromptError(Exception):
    body = {"error": {"type": "invalid_request_error", "code": "invalid_prompt"}}


def main() -> None:
    sanitized = _sanitize_read_file_args({"file_path": "agent.py", "offset": '170, "limit": 60'})
    assert sanitized["offset"] == 170
    print(f"sanitized read_file offset: {sanitized['offset']}")

    assert task_retry_on(HttpError(503)) is True
    assert task_retry_on(HttpError(400)) is False
    print("retryable statuses: 503=True, 400=False")

    failure = json.loads(task_on_failure(InvalidPromptError("bad prompt")))
    assert failure["error"]["code"] == "invalid_prompt"
    print(f"model-fixable task failure: {failure['error']['code']}")

    command = "GH_TOKEN=dummy gh pr create --draft"
    assert is_pr_creation_fallback_command(command) is True
    print("blocked shell fallback: True")


if __name__ == "__main__":
    main()
