# newfan-export

確定データの JSON/CSV/Webhook 配信（詳細設計 §5.9 / §6.4）。

## 提供物

| モジュール | 内容 |
|---|---|
| `canonical.py` | canonical JSON 生成（§6.3 result 形式 ＋ 確定値 `final`） |
| `csv_export.py` | メイン CSV（テナントのマッピング設定に従い flatten）＋ 明細別 CSV（親 document_id 付き） |
| `webhook.py` | HMAC-SHA256 署名（`X-NF-Signature`）・タイムスタンプ・**SSRF ガード**・5回指数リトライのスケジュール |
| `storage.py` | canonical JSON 保存先（本番 S3 / dev LocalObjectStore） |
| `service.py` | `ExportService.export_confirmed`: JSON 保存 ＋ Webhook 配信 |

## セキュリティ（§11）

- Webhook 署名は `sha256=HMAC(body, endpoint_secret)`。受信側は `X-NF-Timestamp`（±5分）と併せて検証する。
- **SSRF 対策**: 配信先 URL が非 http(s)、または解決先がプライベート/ループバック/リンクローカル/
  予約 IP の場合は拒否する（`is_blocked_url`）。クラウドメタデータ (169.254.169.254) 等をブロック。

## リトライ（§6.4 / §9）

`next_retry_delay(attempt)` が 1m/5m/30m/2h/12h を返す。本モジュールは 1 回送信＋スケジュール算出を
担い、実際の再送は §9 のジョブ基盤（`q.export` のリトライ・DLQ）に委ねる。5回失敗で UI 通知。

## テスト / 本番

```bash
uv run pytest services/export
```

canonical/CSV は純ロジック、webhook は httpx MockTransport ＋ IP リテラルで DNS なしに検証する。
本番の S3 保存は `runtime` extra（boto3）。会計・販売管理の個別コネクタはフェーズ2以降（§5.9）。
