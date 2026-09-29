from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence


STRICT_FIELDS = (
    "brand",
    "model",
    "cpu",
    "gpu",
    "gpu_vram_gb",
    "ram_gb",
    "ssd_gb",
    "screen_size_inch",
    "screen_resolution",
)

OPTIONAL_STRICT_FIELDS = (
    "screen_refresh_hz",
    "os",
)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def normalize_identifier(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    return re.sub(r"\s+", "", text).upper()


def normalize_brand(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    aliases = {
        "ASUS": "ASUS",
        "АСУС": "ASUS",
        "MSI": "MSI",
        "LENOVO": "LENOVO",
        "ACER": "ACER",
        "GIGABYTE": "GIGABYTE",
        "MAIBENBEN": "MAIBENBEN",
        "COLORFUL": "COLORFUL",
        "APPLE": "APPLE",
        "HP": "HP",
        "HUAWEI": "HUAWEI",
    }
    key = text.upper().replace("Ё", "Е")
    return aliases.get(key, key)


def normalize_ram_gb(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).lower().replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(тб|tb|гб|gb|mb|мб)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "gb").lower()
    if unit in {"tb", "тб"}:
        return int(amount * 1024)
    if unit in {"mb", "мб"}:
        return int(amount / 1024) if amount >= 1024 else None
    return int(amount)


def normalize_ssd_gb(value: str | int | float | None) -> int | None:
    return normalize_ram_gb(value)


def normalize_vram_gb(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Regard stores VRAM in MB when large (e.g. 12288).
        number = float(value)
        if number >= 256:
            return int(round(number / 1024))
        return int(number)
    text = str(value).lower().replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(тб|tb|гб|gb|mb|мб)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit is None:
        # bare 12288 from Regard characteristics
        if amount >= 256:
            return int(round(amount / 1024))
        return int(amount)
    unit = unit.lower()
    if unit in {"mb", "мб"}:
        return int(round(amount / 1024))
    if unit in {"tb", "тб"}:
        return int(amount * 1024)
    return int(amount)


def normalize_screen_size_inch(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def normalize_resolution(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    match = re.search(r"(\d+)\s*[xх×]\s*(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    return f"{int(match.group(1))}x{int(match.group(2))}"


def normalize_refresh_hz(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"(\d+)", str(value))
    return int(match.group(1)) if match else None


def normalize_gpu(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    upper = text.upper().replace("Ё", "Е")
    # Keep mobile/laptop distinction where present.
    patterns = [
        (r"RTX\s*5070\s*TI", "RTX 5070 TI LAPTOP"),
        (r"RTX\s*5080(?!\s*TI)", "RTX 5080 LAPTOP"),
        (r"RTX\s*5090", "RTX 5090 LAPTOP"),
        (r"RTX\s*5060", "RTX 5060 LAPTOP"),
        (r"RTX\s*5050", "RTX 5050 LAPTOP"),
        (r"RTX\s*4070", "RTX 4070 LAPTOP"),
        (r"RTX\s*4060", "RTX 4060 LAPTOP"),
        (r"RTX\s*4050", "RTX 4050 LAPTOP"),
    ]
    for pattern, canonical in patterns:
        if re.search(pattern, upper):
            return canonical
    # Generic cleanup
    upper = re.sub(r"\bNVIDIA\b|\bNVIDI?A\b|\bGEFORCE\b|\bNV\b", " ", upper)
    upper = re.sub(r"\bДЛЯ НОУТБУКА\b|\bMOBILE\b|\bLAPTOP\b|\bGPU\b", " ", upper)
    upper = re.sub(r"\s+", " ", upper).strip()
    return upper or None


def normalize_cpu(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    upper = text.upper().replace("Ё", "Е")
    upper = re.sub(r"\([^)]*\)", " ", upper)
    upper = re.sub(r"\bPROCESSOR\b|\bCPU\b", " ", upper)
    upper = re.sub(r"\s+", " ", upper).strip()

    model = re.search(r"\b(\d{4,5}[A-Z]{1,4})\b", upper)
    model_token = model.group(1) if model else None

    if "RYZEN" in upper:
        series = re.search(r"RYZEN(?:\s+AI)?(?:\s+\d+)?", upper)
        series_token = series.group(0) if series else "RYZEN"
        if model_token:
            return f"{series_token} {model_token}"
        return series_token

    if "CORE ULTRA" in upper or "ULTRA" in upper:
        series = re.search(r"CORE\s+ULTRA\s+\d+|ULTRA\s+\d+", upper)
        series_token = series.group(0) if series else "CORE ULTRA"
        if model_token:
            return f"{series_token} {model_token}"
        return series_token

    if re.search(r"\bI[3579]\b", upper):
        series = re.search(r"CORE\s+I[3579]|I[3579]", upper)
        series_token = series.group(0) if series else None
        if series_token and model_token:
            return f"{series_token} {model_token}"
        if series_token:
            return series_token

    return model_token or upper


def normalize_os(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    upper = text.upper().replace("Ё", "Е")
    if any(x in upper for x in ["НЕ УСТАНОВЛЕН", "WITHOUT OS", "NO OS", "FREEDOS", "FREE DOS", "DOS"]):
        return "NO_OS"
    if "WINDOWS 11 PRO" in upper:
        return "WINDOWS 11 PRO"
    if "WINDOWS 11 HOME" in upper or "WINDOWS 11" in upper:
        return "WINDOWS 11 HOME"
    if "LINUX" in upper:
        return "LINUX"
    return upper


def normalize_model(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    # Collapse separators for comparison, keep alnum tokens.
    upper = text.upper().replace("Ё", "Е")
    upper = upper.replace("—", "-").replace("–", "-")
    upper = re.sub(r"\s+", " ", upper).strip()
    return upper


def extract_model_code_candidates(text: str | None) -> set[str]:
    """Достаёт возможные manufacturer model codes из строки модели/названия."""
    if not text:
        return set()
    upper = text.upper()
    found: set[str] = set()
    patterns = [
        r"\bG\d{3}[A-Z]{2}-[A-Z0-9]+\b",  # G614PR-RV027
        r"\bGU\d{3}[A-Z]{2}-[A-Z0-9]+\b",
        r"\bH\d{4}[A-Z]{2}-[A-Z0-9]+\b",
        r"\b9S7-[A-Z0-9]+-[A-Z0-9]+\b",
        r"\b[A-Z]{2}\d{2}[A-Z0-9.-]+\b",
        r"\b\d{2}[A-Z0-9]{6,}\b",  # Lenovo 83F50029RK-like
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, upper):
            found.add(normalize_identifier(match.group(0)) or "")
    return {x for x in found if x}


@dataclass
class ProductIdentity:
    store: str
    external_id: str
    sku: str | None
    name: str
    price: int | None
    available: bool
    url: str | None

    brand: str | None = None
    model_family: str | None = None
    model: str | None = None
    manufacturer_part_number: str | None = None
    alternative_part_numbers: list[str] = field(default_factory=list)

    cpu: str | None = None
    gpu: str | None = None
    gpu_vram_gb: int | None = None

    ram_gb: int | None = None
    ram_type: str | None = None
    ssd_gb: int | None = None

    screen_size_inch: float | None = None
    screen_resolution: str | None = None
    screen_refresh_hz: int | None = None

    os: str | None = None
    color: str | None = None
    ean: str | None = None
    gtin: str | None = None

    raw_characteristics: dict[str, str] = field(default_factory=dict)
    source_identifiers: dict[str, Any] = field(default_factory=dict)

    def all_identifiers(self) -> set[str]:
        values = {
            normalize_identifier(self.sku),
            normalize_identifier(self.manufacturer_part_number),
            normalize_identifier(self.ean),
            normalize_identifier(self.gtin),
        }
        for item in self.alternative_part_numbers:
            values.add(normalize_identifier(item))
        values |= extract_model_code_candidates(self.model)
        values |= extract_model_code_candidates(self.name)
        return {v for v in values if v}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IdentityComparison:
    status: str  # CONFIRMED | CANDIDATE | REJECTED
    left: ProductIdentity
    right: ProductIdentity
    matching_fields: dict[str, Any] = field(default_factory=dict)
    different_fields: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    identifiers: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "regard_sku": self.left.sku if self.left.store == "regard" else self.right.sku,
            "andpro_sku": self.right.sku if self.right.store == "andpro" else self.left.sku,
            "regard": {
                "store": self.left.store if self.left.store == "regard" else self.right.store,
                "external_id": (
                    self.left.external_id if self.left.store == "regard" else self.right.external_id
                ),
                "sku": self.left.sku if self.left.store == "regard" else self.right.sku,
                "name": self.left.name if self.left.store == "regard" else self.right.name,
                "price": self.left.price if self.left.store == "regard" else self.right.price,
                "url": self.left.url if self.left.store == "regard" else self.right.url,
            },
            "andpro": {
                "store": self.right.store if self.right.store == "andpro" else self.left.store,
                "external_id": (
                    self.right.external_id if self.right.store == "andpro" else self.left.external_id
                ),
                "sku": self.right.sku if self.right.store == "andpro" else self.left.sku,
                "name": self.right.name if self.right.store == "andpro" else self.left.name,
                "price": self.right.price if self.right.store == "andpro" else self.left.price,
                "url": self.right.url if self.right.store == "andpro" else self.left.url,
            },
            "matching_fields": self.matching_fields,
            "different_fields": self.different_fields,
            "missing_fields": self.missing_fields,
            "identifiers": self.identifiers,
            "reason": self.reason,
        }


def _field_value(identity: ProductIdentity, field_name: str) -> Any:
    return getattr(identity, field_name)


CONFIG_CONFLICT_FIELDS = (
    "cpu",
    "gpu",
    "gpu_vram_gb",
    "ram_gb",
    "ssd_gb",
    "screen_size_inch",
    "screen_resolution",
)


def find_config_conflicts(
    left: ProductIdentity,
    right: ProductIdentity,
) -> dict[str, dict[str, Any]]:
    """
    Явные противоречия конфигурации.

    Missing field на одной стороне НЕ считается конфликтом.
    """
    different: dict[str, dict[str, Any]] = {}
    for field_name in CONFIG_CONFLICT_FIELDS:
        lv = getattr(left, field_name)
        rv = getattr(right, field_name)
        if lv is None or rv is None:
            continue
        if lv != rv:
            different[field_name] = {"left": lv, "right": rv}
    return different


def has_strong_identifier_link(left: ProductIdentity, right: ProductIdentity) -> tuple[bool, str]:
    left_ids = left.all_identifiers()
    right_ids = right.all_identifiers()

    # A. EAN/GTIN
    left_ean = {normalize_identifier(left.ean), normalize_identifier(left.gtin)} - {None}
    right_ean = {normalize_identifier(right.ean), normalize_identifier(right.gtin)} - {None}
    shared_ean = left_ean & right_ean
    if shared_ean:
        return True, f"Общий EAN/GTIN: {sorted(shared_ean)[0]}"

    # B/C. one store contains the other store SKU/MPN as alternative/model/part number
    left_sku = normalize_identifier(left.sku)
    right_sku = normalize_identifier(right.sku)
    left_mpn = normalize_identifier(left.manufacturer_part_number)
    right_mpn = normalize_identifier(right.manufacturer_part_number)

    if right_sku and right_sku in left_ids and right_sku != left_sku:
        return True, f"Regard содержит ANDPRO идентификатор {right_sku}"
    if left_sku and left_sku in right_ids and left_sku != right_sku:
        return True, f"ANDPRO содержит Regard идентификатор {left_sku}"
    if right_mpn and right_mpn in left_ids and right_mpn not in {left_sku, left_mpn}:
        return True, f"Regard содержит ANDPRO MPN {right_mpn}"
    if left_mpn and left_mpn in right_ids and left_mpn not in {right_sku, right_mpn}:
        return True, f"ANDPRO содержит Regard MPN {left_mpn}"

    shared = (left_ids & right_ids) - {None}
    # Ignore shared brand-only nonsense; require non-trivial token length
    shared = {x for x in shared if len(x) >= 6}
    if shared:
        return True, f"Общий идентификатор: {sorted(shared)[0]}"

    return False, ""


def _models_equivalent(left: ProductIdentity, right: ProductIdentity) -> bool:
    lv = left.model
    rv = right.model
    if lv and rv and lv == rv:
        return True
    left_codes = extract_model_code_candidates(left.model) | extract_model_code_candidates(
        left.manufacturer_part_number
    ) | extract_model_code_candidates(left.sku)
    right_codes = extract_model_code_candidates(right.model) | extract_model_code_candidates(
        right.manufacturer_part_number
    ) | extract_model_code_candidates(right.sku)
    shared = {c for c in (left_codes & right_codes) if len(c) >= 6}
    if shared:
        return True
    # Exact containment of full manufacturer code, not fuzzy similarity.
    if lv and rv:
        if normalize_identifier(lv) and normalize_identifier(lv) in normalize_identifier(rv):
            return True
        if normalize_identifier(rv) and normalize_identifier(rv) in normalize_identifier(lv):
            return True
    return False


def compare_identities(left: ProductIdentity, right: ProductIdentity) -> IdentityComparison:
    if left.store == "andpro" and right.store == "regard":
        left, right = right, left

    matching: dict[str, Any] = {}
    different: dict[str, Any] = {}
    missing: list[str] = []

    # Special handling for model: allow documented code containment, not fuzzy names.
    lv_model = left.model
    rv_model = right.model
    if lv_model is None or rv_model is None:
        missing.append("model")
    elif _models_equivalent(left, right):
        matching["model"] = {"regard": lv_model, "andpro": rv_model}
    else:
        different["model"] = {"regard": lv_model, "andpro": rv_model}

    for field_name in STRICT_FIELDS + OPTIONAL_STRICT_FIELDS:
        if field_name == "model":
            continue
        lv = _field_value(left, field_name)
        rv = _field_value(right, field_name)
        if lv is None or rv is None:
            if field_name in STRICT_FIELDS or lv is not None or rv is not None:
                if field_name in STRICT_FIELDS or (
                    field_name in OPTIONAL_STRICT_FIELDS and (lv is None or rv is None)
                ):
                    if field_name in OPTIONAL_STRICT_FIELDS and (lv is None or rv is None):
                        # optional missing on one/both sides: record but do not reject alone
                        if field_name not in missing:
                            missing.append(field_name)
                        continue
                    missing.append(field_name)
            continue
        if lv == rv:
            matching[field_name] = lv
        else:
            different[field_name] = {"regard": lv, "andpro": rv}

    identifiers = {
        "regard": sorted(left.all_identifiers()),
        "andpro": sorted(right.all_identifiers()),
        "regard_sku": left.sku,
        "andpro_sku": right.sku,
        "regard_mpn": left.manufacturer_part_number,
        "andpro_mpn": right.manufacturer_part_number,
        "regard_alt": left.alternative_part_numbers,
        "andpro_alt": right.alternative_part_numbers,
        "regard_ean": left.ean or left.gtin,
        "andpro_ean": right.ean or right.gtin,
    }

    strong, strong_reason = has_strong_identifier_link(left, right)
    if strong:
        return IdentityComparison(
            status="CONFIRMED",
            left=left,
            right=right,
            matching_fields=matching,
            different_fields=different,
            missing_fields=missing,
            identifiers=identifiers,
            reason=strong_reason,
        )

    # Optional fields differences should reject only if both present and differ.
    # Already captured in different.

    if different:
        diffs = ", ".join(
            f"{k}: {v['regard']} != {v['andpro']}" for k, v in different.items()
        )
        return IdentityComparison(
            status="REJECTED",
            left=left,
            right=right,
            matching_fields=matching,
            different_fields=different,
            missing_fields=missing,
            identifiers=identifiers,
            reason=f"Противоречие характеристик: {diffs}",
        )

    missing_strict = [f for f in STRICT_FIELDS if f in missing]
    if missing_strict:
        return IdentityComparison(
            status="REJECTED",
            left=left,
            right=right,
            matching_fields=matching,
            different_fields=different,
            missing_fields=missing,
            identifiers=identifiers,
            reason=(
                "Недостаточно данных для strict config match; "
                f"missing={missing_strict}"
            ),
        )

    return IdentityComparison(
        status="CANDIDATE",
        left=left,
        right=right,
        matching_fields=matching,
        different_fields=different,
        missing_fields=missing,
        identifiers=identifiers,
        reason="Конфигурация совпадает, общего part number/EAN не найдено",
    )


def pair_unmatched(
    regard_items: Sequence[ProductIdentity],
    andpro_items: Sequence[ProductIdentity],
) -> dict[str, Any]:
    comparisons: list[IdentityComparison] = []
    matched_regard: set[str] = set()
    matched_andpro: set[str] = set()

    # First pass: strong identifier links only for CONFIRMED pairing.
    for left in regard_items:
        for right in andpro_items:
            strong, _ = has_strong_identifier_link(left, right)
            if not strong:
                continue
            cmp = compare_identities(left, right)
            if cmp.status == "CONFIRMED":
                comparisons.append(cmp)
                matched_regard.add(left.external_id)
                matched_andpro.add(right.external_id)

    # Second pass: evaluate remaining pairs for CANDIDATE/REJECTED diagnostics.
    # Only keep CANDIDATE pairs; REJECTED are kept when brands match to explain.
    for left in regard_items:
        if left.external_id in matched_regard:
            continue
        for right in andpro_items:
            if right.external_id in matched_andpro:
                continue
            # Cheap prefilter: same brand required for candidate consideration.
            if left.brand and right.brand and left.brand != right.brand:
                continue
            cmp = compare_identities(left, right)
            if cmp.status == "CANDIDATE":
                comparisons.append(cmp)
                matched_regard.add(left.external_id)
                matched_andpro.add(right.external_id)
            elif cmp.status == "REJECTED" and (
                left.brand == right.brand
                or bool(extract_model_code_candidates(left.name) & extract_model_code_candidates(right.name))
                or bool(extract_model_code_candidates(left.model or "") & extract_model_code_candidates(right.model or ""))
            ):
                comparisons.append(cmp)

    confirmed = [c for c in comparisons if c.status == "CONFIRMED"]
    candidates = [c for c in comparisons if c.status == "CANDIDATE"]
    rejected = [c for c in comparisons if c.status == "REJECTED"]

    unpaired_regard = [i for i in regard_items if i.external_id not in matched_regard]
    unpaired_andpro = [i for i in andpro_items if i.external_id not in matched_andpro]

    # Mark unpaired as having no suitable pair (not every rejected pair).
    return {
        "confirmed": confirmed,
        "candidates": candidates,
        "rejected": rejected,
        "unpaired_regard": unpaired_regard,
        "unpaired_andpro": unpaired_andpro,
    }
