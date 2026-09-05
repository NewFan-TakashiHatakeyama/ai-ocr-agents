"""オブジェクトストレージ抽象（§2.3 パス規約）。

本番は S3（SSE-KMS）。dev/テストは LocalObjectStore（ファイルシステム）。
パス規約: {tenant_id}/{document_id}/original.{ext}, .../pages/{page_no}.png
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Protocol

# delete_prefix が受け付ける prefix。パス規約（{tenant_id}/{document_id}/）に
# 一致するものだけを消す。空文字や "ten_1/" を渡せてしまうと、1 帳票の削除の
# つもりでテナント丸ごと・バケット丸ごとを消せる。呼び出し側のバグを
# 「消えてから気付く」のではなく例外で止めるためのガード。
#
# 文字クラスに "." を許すのは doc_type 付きの id 等を想定してのことだが、そのままだと
# ".." が 1 セグメントとして通り、"a/../" が storage_root 丸ごとの rmtree になる。
# 正規表現だけに頼らず、下の _BAD_SEGMENTS でセグメント単位にも弾く。
_DOC_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/$")


def check_document_prefix(prefix: str) -> None:
    """delete_prefix の prefix が 1 帳票のパス規約に合うことを検査する。

    ドットだけのセグメント（"." ".." "...." 等）は一律拒否する。".." は明白な
    トラバーサルだが、"...." も Windows がパス解決で末尾ドットを落とすため
    OS によって挙動が割れる（Linux では無害なサブディレクトリ、Windows では
    ルート脱出になり得る — CI と実機の差として実測）。正規の tenant/document id に
    全ドット名は存在しないので、プラットフォーム非依存で弾くのが安全。
    """
    if not _DOC_PREFIX_RE.match(prefix) or any(
        not seg.strip(".") for seg in prefix.split("/")[:-1]
    ):
        raise ValueError(
            f"削除できるのは {{tenant_id}}/{{document_id}}/ 形式の prefix のみです: {prefix!r}"
        )


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """key に data を保存し、URI（s3://... 等）を返す。"""
        ...

    def delete_prefix(self, prefix: str) -> int:
        """prefix 配下を削除し、消したオブジェクト数を返す（§2.3 帳票単位の削除）。

        prefix は {tenant_id}/{document_id}/ 形式のみ。部分的にでも消し残したら
        例外を投げる（呼び出し側は DB を消さずに 500 を返し、再試行で前進する）。
        """
        ...


class LocalObjectStore:
    """ファイルシステム実装（dev/結合テスト用）。URI は file:// を返す。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.resolve().as_uri()

    def delete_prefix(self, prefix: str) -> int:
        check_document_prefix(prefix)
        # 正規表現ガードに加えて実パスでも保管ルート配下に閉じ込める。読み取り側
        # （page_images.read_local_image）と同じ二重防御。片方だけに頼ると、
        # ガードの穴がそのまま「ルートごと rmtree」になる。
        root = self._root.resolve()
        path = (self._root / prefix).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError(f"保管ルート外を指す prefix です: {prefix!r}")
        if not path.is_dir():
            return 0  # 既に無い＝削除済み。冪等に 0 を返す
        count = sum(1 for p in path.rglob("*") if p.is_file())
        shutil.rmtree(path)
        return count


class S3ObjectStore:
    """本番実装（S3 SSE-KMS, §2.3）。runtime extra（boto3）が必要。"""

    def __init__(self, bucket: str, *, kms_key_id: str | None = None, client: object | None = None) -> None:
        if client is not None:
            self._client = client
        else:
            import boto3  # 遅延 import（runtime extra）

            self._client = boto3.client("s3")
        self._bucket = bucket
        self._kms_key_id = kms_key_id

    def put(self, key: str, data: bytes, content_type: str) -> str:
        extra: dict[str, str] = {"ContentType": content_type}
        if self._kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self._kms_key_id
        else:
            extra["ServerSideEncryption"] = "aws:kms"
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)  # type: ignore[attr-defined]
        return f"s3://{self._bucket}/{key}"

    def delete_prefix(self, prefix: str) -> int:
        """prefix 配下を全バージョン削除する。

        production のバケットはバージョニング有効（terraform s3.tf）。VersionId を
        付けない DeleteObject は削除マーカーを置くだけで、バイトはバケットに残る
        ＝「元に戻せません」が嘘になる。そのため list_object_versions で
        Versions と DeleteMarkers の両方を列挙し、VersionId 付きで消す。
        staging/dev（バージョニング無効）では Versions に current 版だけが
        並ぶので、同じコードがそのまま正しく動く。
        """
        check_document_prefix(prefix)
        cli: Any = self._client
        deleted = 0
        token: dict[str, str] = {}
        while True:
            page = cli.list_object_versions(Bucket=self._bucket, Prefix=prefix, **token)
            targets = [
                {"Key": o["Key"], "VersionId": o["VersionId"]}
                for o in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])
            ]
            if targets:
                # delete_objects は 1 リクエスト 1000 件まで。空の Objects は
                # ParamValidationError になるので targets が空なら呼ばない。
                for i in range(0, len(targets), 1000):
                    resp = cli.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": targets[i : i + 1000]}
                    )
                    errors = resp.get("Errors") or []
                    if errors:
                        # 部分成功を成功として返すと「消えたはず」が黙って崩れる。
                        raise RuntimeError(f"S3 の削除に失敗しました: {errors[:3]}")
                    deleted += len(resp.get("Deleted") or [])
            if not page.get("IsTruncated"):
                return deleted
            token = {
                "KeyMarker": page["NextKeyMarker"],
                "VersionIdMarker": page["NextVersionIdMarker"],
            }


def original_key(tenant_id: str, document_id: str, ext: str) -> str:
    return f"{tenant_id}/{document_id}/original.{ext.lstrip('.')}"


def page_key(tenant_id: str, document_id: str, page_no: int) -> str:
    return f"{tenant_id}/{document_id}/pages/{page_no}.png"
