"""LLMAdapter: JSON 契約実行・リペアリトライ・コスト計測・ZDR ガード（§2.1 / §10 / §11）。"""

from __future__ import annotations

import json
from typing import Any, Optional

from newfan_metrics import current_tenant, llm_cost_jpy_total, llm_tokens_total

from newfan_llm_adapter.errors import LLMError
from newfan_llm_adapter.provider import LLMProvider, LLMResponse

# §12.1 の llm_cost_jpy_total は円建て。相場は動くので概算の固定レートで換算する
# （課金の正確な金額ではなく、テナント別のコスト傾向を見るための指標）。
USD_JPY = 150.0

# per-1M USD（claude-api skill のモデル表）。jpy 換算はテナント/構成側で行う。
_RATES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
    # Gemini（KIE の既定プロバイダ, worker_main._make_provider）
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.0),
}

# ZDR 必須テナントで許可するモデル（構成でホワイトリスト, §11）。
# 注意: claude-fable-5 は ZDR 非対応（30日保持必須）のため既定では含めない。
_DEFAULT_ZDR_ALLOWED = frozenset({"claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"})


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = _RATES.get(model, (0.0, 0.0))
    return (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000


def _extract_json(text: str) -> Any:
    """テキストから最外の JSON オブジェクト/配列を取り出して parse する。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
    if start == -1:
        raise ValueError("JSON が見つかりません")
    # 末尾の対応する括弧までを探索
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)
    return json.loads(text[start : end + 1])


class LLMAdapter:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str = "claude-opus-4-8",
        max_tokens: int = 8192,
        zdr_required: bool = False,
        zdr_allowed: frozenset[str] = _DEFAULT_ZDR_ALLOWED,
    ) -> None:
        if zdr_required and model not in zdr_allowed:
            raise LLMError(
                "E5001",
                f"ZDR 必須テナントでモデル {model!r} は許可されていません",
            )
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0

    def usage_snapshot(self) -> dict[str, float]:
        """累積使用量の断面。worker が run 前後の差分を extraction_runs.metrics へ
        永続化する（SCR-04 の LLM コスト算出の元データ, §12.1）。"""
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cost_jpy": self.total_cost_usd * USD_JPY,
        }

    def _account(self, resp: LLMResponse, purpose: str) -> None:
        self.total_input_tokens += resp.input_tokens
        self.total_output_tokens += resp.output_tokens
        cost_usd = estimate_cost_usd(
            resp.model or self._model, resp.input_tokens, resp.output_tokens
        )
        self.total_cost_usd += cost_usd

        # §12.1: llm_tokens_total{purpose} / llm_cost_jpy_total{tenant}。
        # LLM 呼出しは全てここを通るので、計装はこの 1 箇所で足りる。
        tenant = current_tenant()
        llm_tokens_total.labels(purpose=purpose, direction="input").inc(resp.input_tokens)
        llm_tokens_total.labels(purpose=purpose, direction="output").inc(resp.output_tokens)
        llm_cost_jpy_total.labels(tenant=tenant).inc(cost_usd * USD_JPY)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        purpose: str = "unknown",
    ) -> tuple[Any, LLMResponse]:
        """JSON 応答を要求し parse して返す。不正なら 1 回だけ矯正リトライ（→E3002）。

        purpose は §12.1 の llm_tokens_total{purpose}（kie/correct/chat 別）に使う。
        """
        budget = max_tokens or self._max_tokens
        resp = self._provider.complete(system=system, user=user, max_tokens=budget)
        self._account(resp, purpose)
        try:
            return _extract_json(resp.text), resp
        except (ValueError, json.JSONDecodeError):
            pass

        repair_user = (
            user
            + "\n\n# 重要\n直前の出力は JSON として不正でした。JSON のみを出力してください。"
        )
        resp2 = self._provider.complete(system=system, user=repair_user, max_tokens=budget)
        self._account(resp2, purpose)
        try:
            return _extract_json(resp2.text), resp2
        except (ValueError, json.JSONDecodeError) as exc:
            raise LLMError("E3002", "LLM 出力の JSON 契約違反（再試行後）", detail=str(exc)) from exc
