"""PaddleOCR 自己ホストサービングのクライアント。

対象は基本/高安定性サービングの REST 契約（同一。適合調査報告 §5）:
- structure-svc: POST /layout-parsing（PP-StructureV3, モデルは設定 YAML で固定）
- ocr-svc:       POST /ocr（PP-OCRv6 単体, return_word_box は設定 YAML で固定）

DD-01/ADR-0002 により、前処理は ingest 側で実施済みのため、既定で
useDocOrientationClassify=False / useDocUnwarping=False で呼び出す。
画像は Base64 インライン受領（return_urls は BOS 専用で AWS 非対応、報告 §5）。
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import httpx

from newfan_paddle_client.errors import PaddleServingError
from newfan_paddle_client.schema import (
    LayoutParsingResponse,
    OcrResponse,
    ServingEnvelope,
)


def encode_image(data: bytes) -> str:
    """ページ画像バイト列を Base64(ascii) へ。"""
    return base64.b64encode(data).decode("ascii")


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


class PaddleServingClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PaddleServingClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:  # 接続・タイムアウト等
            raise PaddleServingError(str(exc), endpoint=path) from exc
        if resp.status_code >= 400:
            raise PaddleServingError(
                f"serving returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                endpoint=path,
            )
        env = ServingEnvelope.model_validate(resp.json())
        if env.error_code not in (None, 0):
            raise PaddleServingError(
                env.error_msg or "serving error",
                status_code=resp.status_code,
                error_code=env.error_code,
                endpoint=path,
            )
        return env.result

    def layout_parsing(
        self,
        file_b64: str,
        *,
        file_type: int = 1,
        use_doc_orientation_classify: bool = False,
        use_doc_unwarping: bool = False,
        use_seal_recognition: Optional[bool] = None,
        use_table_recognition: bool = True,
        use_formula_recognition: bool = False,
        use_chart_recognition: bool = False,
        visualize: bool = False,
        text_rec_score_thresh: Optional[float] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> LayoutParsingResponse:
        """PP-StructureV3 の POST /layout-parsing を呼ぶ。file_type: 0=PDF, 1=画像。

        use_seal_recognition は既定 None（＝送らない）。印章認識はデプロイ単位のオプションで、
        structure-svc は use_seal_recognition: false、structure-svc-seal は true の config を
        持つ。クライアントが True を固定すると印章モデル未初期化の既定デプロイで
        「Set use_seal_recognition, but the models for seal recognition are not initialized」
        となり 500 になる（実コンテナの E2E で検出）。フラグは config を正とする（DD-03）。
        """
        payload = _drop_none(
            {
                "file": file_b64,
                "fileType": file_type,
                "useDocOrientationClassify": use_doc_orientation_classify,
                "useDocUnwarping": use_doc_unwarping,
                "useSealRecognition": use_seal_recognition,
                "useTableRecognition": use_table_recognition,
                "useFormulaRecognition": use_formula_recognition,
                "useChartRecognition": use_chart_recognition,
                "visualize": visualize,
                "textRecScoreThresh": text_rec_score_thresh,
            }
        )
        if extra:
            payload.update(extra)
        result = self._post("/layout-parsing", payload)
        return LayoutParsingResponse.model_validate(result)

    def ocr(
        self,
        file_b64: str,
        *,
        file_type: int = 1,
        use_doc_orientation_classify: bool = False,
        use_doc_unwarping: bool = False,
        use_textline_orientation: Optional[bool] = None,
        text_rec_score_thresh: Optional[float] = None,
        visualize: bool = False,
        extra: Optional[dict[str, Any]] = None,
    ) -> OcrResponse:
        """PP-OCRv6 単体 POST /ocr。crop 再認識・単文字座標補完に使う（DD-02 / §5.3.2）。

        return_word_box はサービング設定 YAML で有効化する（付録C-3）。リクエスト
        ボディでは指定できない。
        """
        payload = _drop_none(
            {
                "file": file_b64,
                "fileType": file_type,
                "useDocOrientationClassify": use_doc_orientation_classify,
                "useDocUnwarping": use_doc_unwarping,
                "useTextlineOrientation": use_textline_orientation,
                "textRecScoreThresh": text_rec_score_thresh,
                "visualize": visualize,
            }
        )
        if extra:
            payload.update(extra)
        result = self._post("/ocr", payload)
        return OcrResponse.model_validate(result)
