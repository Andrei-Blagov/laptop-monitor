from dataclasses import dataclass
from datetime import datetime


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
