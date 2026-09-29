from __future__ import annotations

import re
from typing import Any, Mapping

from product_identity import normalize_gpu

# Канонические имена для TARGET_PRICES / отчётов.
TARGET_GPU_LABELS = (
    "RTX 5070 Ti",
    "RTX 5080",
)

_NORMALIZED_TO_LABEL = {
    "RTX 5070 TI LAPTOP": "RTX 5070 Ti",
    "RTX 5080 LAPTOP": "RTX 5080",
}

# Явные паттерны: Ti раньше 5080; 5080 не матчит 5080 Ti.
_TARGET_PATTERNS = (
    (re.compile(r"RTX\s*5070\s*TI\b", re.IGNORECASE), "RTX 5070 Ti"),
    (re.compile(r"RTX\s*5080(?!\s*TI)\b", re.IGNORECASE), "RTX 5080"),
)


def _from_normalized(value: str | None) -> str | None:
    if not value:
        return None
    return _NORMALIZED_TO_LABEL.get(value)


def _from_text(*parts: str | None) -> str | None:
    text = " ".join(p for p in parts if p)
    if not text:
        return None
    # Сначала через общую нормализацию GPU (отсекает desktop-формы через LAPTOP label).
    normalized = normalize_gpu(text)
    label = _from_normalized(normalized)
    if label:
        return label
    # Прямые паттерны на исходный текст (name / raw).
    for pattern, label in _TARGET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def get_target_gpu(product: Any) -> str | None:
    """
    Определяет целевой GPU для правил TARGET_PRICE.

    Returns:
        "RTX 5070 Ti" | "RTX 5080" | None

    Источники (в порядке приоритета):
    - поле gpu / already-normalized gpu из specs/identity;
    - raw characteristics (Видеочипсет / Графика);
    - name / title.
    """
    if product is None:
        return None

    if isinstance(product, str):
        return _from_text(product)

    if isinstance(product, Mapping):
        gpu = product.get("gpu")
        label = _from_normalized(str(gpu)) if gpu else None
        if label:
            return label
        label = _from_text(str(gpu) if gpu else None)
        if label:
            return label

        raw = product.get("raw_characteristics") or {}
        if isinstance(raw, Mapping):
            for key in (
                "Видеочипсет",
                "Графика/Модель",
                "short/Графический чипсет",
                "Тип графического контроллера",
            ):
                label = _from_text(raw.get(key) if isinstance(raw.get(key), str) else None)
                if label:
                    return label
            # Regard flat keys / any value mentioning RTX
            for value in raw.values():
                if isinstance(value, str) and "RTX" in value.upper():
                    label = _from_text(value)
                    if label:
                        return label

        return _from_text(
            product.get("name"),
            product.get("title"),
            product.get("model"),
        )

    # ProductIdentity / object with attributes
    gpu = getattr(product, "gpu", None)
    label = _from_normalized(str(gpu)) if gpu else None
    if label:
        return label
    label = _from_text(str(gpu) if gpu else None)
    if label:
        return label

    raw = getattr(product, "raw_characteristics", None) or {}
    if isinstance(raw, Mapping):
        for value in raw.values():
            if isinstance(value, str) and "RTX" in value.upper():
                label = _from_text(value)
                if label:
                    return label

    return _from_text(
        getattr(product, "name", None),
        getattr(product, "model", None),
    )
