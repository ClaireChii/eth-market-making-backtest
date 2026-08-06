"""Executed order fills consumed by the account ledger."""

from dataclasses import dataclass
import math

from src.orders import OrderSide


@dataclass(frozen=True, slots=True)
class Fill:
    """Represent one execution of a simulated order."""

    timestamp_ns: int
    order_id: str
    side: OrderSide
    price: float
    quantity: float
    fee_cashflow: float = 0.0    # Fee cashflow for this fill; costs are negative.

    def __post_init__(self) -> None:
        """Validate the fill fields used by inventory and PnL accounting."""
        if not self.order_id:
            raise ValueError("order_id must not be empty")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("price must be finite and positive")
        if not math.isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        if not math.isfinite(self.fee_cashflow):
            raise ValueError("fee_cashflow must be finite")
