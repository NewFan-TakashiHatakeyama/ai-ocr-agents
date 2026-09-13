"""手元の帳票画像の傾き分布を DeskewPreprocessor で測る（DD-01 前処理の方針決定の材料）。

使い方（リポジトリルートで）:
    .venv/Scripts/python.exe golden/scripts/measure_deskew_angles.py [--samples samples] [--out golden/out/dd01_deskew]

出力: <out>/angles.json（ファイルごとの推定角・gain・applied・秒）と <out>/synthetic_skew.txt
（数件を合成で傾けて推定誤差を見る）。要 Pillow（newfan-ingest の runtime extra）。
記録: docs/design/dd01-deskew-measurement-2026-09-13.md
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import time
from pathlib import Path

from PIL import Image

from newfan_ingest.preprocess import DeskewPreprocessor

_SYNTHETIC_FILES = ("sample2.png", "sample18.jpeg", "sample27.jpg", "sample15.png", "sample8.png")
_SYNTHETIC_SKEWS = (0.5, 1.0, -2.0, 3.5, -4.5)


def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=Path, default=Path("samples"))
    ap.add_argument("--out", type=Path, default=Path("golden/out/dd01_deskew"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    pre = DeskewPreprocessor()
    rows: list[dict[str, object]] = []
    for f in sorted(args.samples.iterdir()):
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"):
            continue
        with Image.open(f) as im:
            im.load()
            w, h = im.size
            png = _png_bytes(im)
        t0 = time.perf_counter()
        angle, detail = pre.estimate_angle(png)
        dt = time.perf_counter() - t0
        rows.append(
            {
                "file": f.name,
                "width": w,
                "height": h,
                "estimated": detail["estimated"],
                "gain": detail["gain"],
                "applied": detail["applied"],
                "angle_applied": angle,
                "seconds": round(dt, 3),
            }
        )
        print(
            f"{f.name:16s} {w:5d}x{h:<5d} est={detail['estimated']:+.2f} "
            f"gain={detail['gain']:.3f} applied={detail['applied']!s:5s} {dt:.2f}s"
        )
    (args.out / "angles.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    est = [abs(float(r["estimated"])) for r in rows]  # type: ignore[arg-type]
    print("\n== summary ==")
    print(
        f"n={len(rows)} applied={sum(1 for r in rows if r['applied'])} "
        f"|est| median={statistics.median(est):.2f} max={max(est):.2f} "
        f"seconds median={statistics.median([float(r['seconds']) for r in rows]):.2f}"  # type: ignore[arg-type]
    )
    for lo, hi in ((0.0, 0.3), (0.3, 1.0), (1.0, 5.0)):
        print(f"  |est| in [{lo},{hi}): {sum(1 for v in est if lo <= v < hi)}")
    print(f"  |est| >= 5.0 (探索上限): {sum(1 for v in est if v >= 5.0)}")

    # 合成傾き: 実帳票を回してから推定し、打ち消す角度を返すか
    lines = ["file            skew   est    err   gain"]
    for name in _SYNTHETIC_FILES:
        src = args.samples / name
        if not src.exists():
            continue
        with Image.open(src) as im:
            rgb = im.convert("RGB")
        for skew in _SYNTHETIC_SKEWS:
            rot = rgb.rotate(skew, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
            _, d = pre.estimate_angle(_png_bytes(rot))
            lines.append(
                f"{name:15s} {skew:+5.2f} {d['estimated']:+6.2f} {d['estimated'] + skew:+6.2f} "
                f"{d['gain']:.3f} applied={d['applied']}"
            )
    (args.out / "synthetic_skew.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
