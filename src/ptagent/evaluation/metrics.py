"""Small helpers for precision/recall style metrics."""
from __future__ import annotations


def precision_recall(predicted: set, expected: set) -> dict:
    tp = len(predicted & expected)
    precision = tp / len(predicted) if predicted else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "true_positives": tp, "false_positives": sorted(map(str, predicted - expected)),
        "false_negatives": sorted(map(str, expected - predicted)),
    }
