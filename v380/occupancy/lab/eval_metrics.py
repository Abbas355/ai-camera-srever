"""Ground-truth evaluation for Detect Lab (IoU matching). Not model accuracy estimates."""

from __future__ import annotations

from typing import Any


def box_iou(a: list[float], b: list[float]) -> float:
    """IoU of two xyxy boxes."""
    if len(a) < 4 or len(b) < 4:
        return 0.0
    ax1, ay1, ax2, ay2 = map(float, a[:4])
    bx1, by1, bx2, by2 = map(float, b[:4])
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return float(inter / union) if union > 0 else 0.0


def _safe_div(n: float, d: float) -> float:
    return float(n / d) if d > 0 else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    prec = _safe_div(tp, tp + fp)
    rec = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * prec * rec, prec + rec) if (prec + rec) > 0 else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(prec, 4),
        "recall": round(rec, 4),
        "f1": round(f1, 4),
    }


def evaluate(
    predictions: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    eval_iou: float = 0.50,
    classes: list[str] | None = None,
) -> dict[str, Any]:
    """Greedy one-to-one match of preds to GT by class + IoU.

    Each item: {label|class, norm:[x1,y1,x2,y2]} in 0..1 image coords.
    Returns measured metrics only (not estimates).
    """
    iou_th = max(0.05, min(0.95, float(eval_iou)))
    want = classes or ["person", "chair", "table"]
    want = [c.lower().strip() for c in want]

    def _label(d: dict[str, Any]) -> str:
        raw = str(d.get("label") or d.get("class") or d.get("kind") or "").lower()
        if "person" in raw:
            return "person"
        if "chair" in raw:
            return "chair"
        if "table" in raw or "desk" in raw or "dining" in raw:
            return "table"
        return raw

    def _norm(d: dict[str, Any]) -> list[float] | None:
        n = d.get("norm")
        if isinstance(n, (list, tuple)) and len(n) >= 4:
            return [float(n[0]), float(n[1]), float(n[2]), float(n[3])]
        return None

    per: dict[str, dict[str, Any]] = {}
    for cls in want:
        gts = [g for g in ground_truth if _label(g) == cls and _norm(g)]
        preds = [p for p in predictions if _label(p) == cls and _norm(p)]
        # Sort preds by conf desc for greedy match
        preds_sorted = sorted(
            preds,
            key=lambda p: float(p.get("conf") or 0.0),
            reverse=True,
        )
        matched_gt: set[int] = set()
        tp = 0
        fp = 0
        for p in preds_sorted:
            pn = _norm(p)
            assert pn is not None
            best_i, best_iou = -1, 0.0
            for gi, g in enumerate(gts):
                if gi in matched_gt:
                    continue
                gn = _norm(g)
                assert gn is not None
                iou = box_iou(pn, gn)
                if iou > best_iou:
                    best_iou, best_i = iou, gi
            if best_i >= 0 and best_iou >= iou_th:
                matched_gt.add(best_i)
                tp += 1
            else:
                fp += 1
        fn = len(gts) - len(matched_gt)
        per[cls] = _prf(tp, fp, fn)
        per[cls]["gt_count"] = len(gts)
        per[cls]["pred_count"] = len(preds)

    tot_tp = sum(int(per[c]["tp"]) for c in want)
    tot_fp = sum(int(per[c]["fp"]) for c in want)
    tot_fn = sum(int(per[c]["fn"]) for c in want)
    overall = _prf(tot_tp, tot_fp, tot_fn)
    overall["gt_count"] = sum(int(per[c]["gt_count"]) for c in want)
    overall["pred_count"] = sum(int(per[c]["pred_count"]) for c in want)

    return {
        "ok": True,
        "kind": "measured",
        "eval_iou": iou_th,
        "note": (
            "Measured vs ground-truth annotations at eval_iou. "
            "This is not an estimate of model accuracy on unseen data."
        ),
        "per_class": per,
        "overall": overall,
    }
