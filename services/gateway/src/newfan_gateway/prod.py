# mypy: ignore-errors
"""本番アダプタ: Redis Streams キューと orchestrator への内部 RPC（§9 / §4.4）。

RedisQueue は runtime extra（redis）が必要。HttpOrchestratorClient は httpx を使う。
resume は orchestrator が再開ジョブとして受け、ワーカーが invoke する（Web 内で長時間実行しない）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx


class RedisQueue:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url)

    def enqueue(self, stream: str, message: dict[str, Any]) -> None:
        # XADD。値は文字列化（本体は DB 参照, §9）
        self._redis.xadd(stream, {"payload": json.dumps(message)})


class QueueOrchestratorClient:
    """本番既定: resume を再開ジョブとしてキューへ発行する（§4.4 実体＝再開ジョブ発行）。

    ワーカーが q.extract から消費し、`resume` 付きペイロードで graph.invoke(Command(resume=...))。
    """

    def __init__(self, queue: "RedisQueue", *, stream: str = "q.extract") -> None:
        self._queue = queue
        self._stream = stream

    def resume(
        self,
        run_id: str,
        tenant_id: str,
        feedback: Optional[dict[str, Any]],
        *,
        notify: Optional[dict[str, Any]] = None,
    ) -> None:
        msg: dict[str, Any] = {"run_id": run_id, "tenant_id": tenant_id, "resume": feedback}
        if notify:
            # ワーカーは payload の notify しか見ない（DD-11: DB の workflow 情報は読まない）。
            # これを載せないと確定フロー完了の confirm_done が q.workflow に届かず、
            # hitl_gate で waiting_hitl のままの run が永久に再開されない。
            msg["notify"] = notify
        self._queue.enqueue(self._stream, msg)


class HttpOrchestratorClient:
    """代替: orchestrator が内部 HTTP を持つ構成向け（同期 RPC）。既定は QueueOrchestratorClient。"""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def resume(
        self,
        run_id: str,
        tenant_id: str,
        feedback: Optional[dict[str, Any]],
        *,
        notify: Optional[dict[str, Any]] = None,
    ) -> None:
        with httpx.Client(base_url=self._base_url, timeout=self._timeout) as client:
            resp = client.post(
                f"/internal/runs/{run_id}/resume",
                json={"tenant_id": tenant_id, "feedback": feedback, "notify": notify},
            )
            resp.raise_for_status()


class BotoSecretStore:
    """Secrets Manager 実装（§16.5 / P6）。

    名前は ai-ocr/<env>/conn/ 配下に限定する（task ロールの IAM がこのプレフィクスに
    スコープされている。terraform main.tf）。
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix.rstrip("/")
        self._client = None

    def _cli(self):  # noqa: ANN202
        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager")
        return self._client

    def create(self, name: str, value: str) -> str:
        full = f"{self._prefix}/{name}"
        try:
            resp = self._cli().create_secret(Name=full, SecretString=value)
            return resp["ARN"]
        except self._cli().exceptions.ResourceExistsException:
            self._cli().put_secret_value(SecretId=full, SecretString=value)
            return self._cli().describe_secret(SecretId=full)["ARN"]

    def get(self, ref: str) -> str:
        return self._cli().get_secret_value(SecretId=ref)["SecretString"]
