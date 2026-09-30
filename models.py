from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Product:
    store: str
    external_id: str
    url: str
    name: str
    sku: str | None
    price: int | None
    available: bool
    checked_at: datetime
    # Optional extras: member_price, availability_status, promo_price, etc.
    metadata: dict[str, Any] = field(default_factory=dict)
