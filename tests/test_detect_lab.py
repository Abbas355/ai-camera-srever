"""Unit tests for Detect Lab eval + preprocess (no GPU / model download required)."""

from __future__ import annotations

import unittest

import numpy as np

from v380.occupancy.lab.eval_metrics import box_iou, evaluate
from v380.occupancy.lab.preprocess import apply_preprocess


class TestLabEval(unittest.TestCase):
    def test_box_iou_identical(self):
        self.assertAlmostEqual(box_iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)

    def test_box_iou_none(self):
        self.assertEqual(box_iou([0, 0, 1, 1], [2, 2, 3, 3]), 0.0)

    def test_evaluate_perfect(self):
        gt = [{"label": "chair", "norm": [0.1, 0.1, 0.3, 0.4]}]
        pred = [{"label": "chair", "conf": 0.9, "norm": [0.11, 0.12, 0.29, 0.39]}]
        m = evaluate(pred, gt, eval_iou=0.3)
        self.assertEqual(m["overall"]["tp"], 1)
        self.assertEqual(m["overall"]["fp"], 0)
        self.assertEqual(m["overall"]["fn"], 0)
        self.assertEqual(m["kind"], "measured")

    def test_evaluate_empty(self):
        m = evaluate([], [], eval_iou=0.5)
        self.assertEqual(m["overall"]["tp"], 0)
        self.assertEqual(m["overall"]["precision"], 0.0)
        self.assertEqual(m["overall"]["recall"], 0.0)

    def test_evaluate_false_positive(self):
        gt = [{"label": "person", "norm": [0.0, 0.0, 0.2, 0.2]}]
        pred = [
            {"label": "person", "conf": 0.9, "norm": [0.0, 0.0, 0.2, 0.2]},
            {"label": "person", "conf": 0.5, "norm": [0.8, 0.8, 0.9, 0.9]},
        ]
        m = evaluate(pred, gt, eval_iou=0.5)
        self.assertEqual(m["per_class"]["person"]["tp"], 1)
        self.assertEqual(m["per_class"]["person"]["fp"], 1)
        self.assertEqual(m["per_class"]["person"]["fn"], 0)


class TestLabPreprocess(unittest.TestCase):
    def test_disabled_noop(self):
        rgb = np.zeros((20, 30, 3), dtype=np.uint8)
        rgb[:] = 100
        out, meta = apply_preprocess(rgb, enabled=False, brightness=0.5, contrast=2.0)
        self.assertFalse(meta["enabled"])
        self.assertTrue(np.array_equal(out, rgb))

    def test_crop(self):
        rgb = np.zeros((100, 100, 3), dtype=np.uint8)
        out, meta = apply_preprocess(
            rgb, enabled=True, crop_norm=[0.1, 0.1, 0.5, 0.5]
        )
        self.assertEqual(meta["crop_norm"], [0.1, 0.1, 0.5, 0.5])
        self.assertEqual(out.shape[0], 40)
        self.assertEqual(out.shape[1], 40)


if __name__ == "__main__":
    unittest.main()
