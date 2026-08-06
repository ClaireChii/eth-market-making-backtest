"""Inventory and PnL ledger for the simulated market maker."""

from dataclasses import dataclass, field
import math

from src.fills import Fill
from src.orders import OrderSide


@dataclass(slots=True)
class Account:
    """Maintain cash, inventory, and average-cost PnL attribution."""

    cash: float = field(init=False, default=0.0)
    inventory: float = field(init=False, default=0.0)
    average_entry_price: float | None = field(init=False, default=None)
    realized_pnl: float = field(init=False, default=0.0)
    cumulative_fee_cashflow: float = field(init=False, default=0.0)
    cumulative_funding_cashflow: float = field(init=False, default=0.0)
    mark_price: float | None = field(init=False, default=None)

    def apply_fill(self, fill: Fill) -> None:
        """Apply one fill to cash, inventory, fees, and realized PnL."""
        signed_quantity = (
            fill.quantity
            if fill.side is OrderSide.BUY
            else -fill.quantity
        )
        previous_inventory = self.inventory
        new_inventory = previous_inventory + signed_quantity

        self.cash -= signed_quantity * fill.price
        self.cumulative_fee_cashflow += fill.fee_cashflow
        self._update_average_cost(
            previous_inventory=previous_inventory,
            signed_quantity=signed_quantity,
            new_inventory=new_inventory,
            fill_price=fill.price,
        )
        self.inventory = new_inventory

        if math.isclose(self.inventory, 0.0, abs_tol=1e-12):
            self.inventory = 0.0
            self.average_entry_price = None

    def _update_average_cost(
        self,
        previous_inventory: float,
        signed_quantity: float,
        new_inventory: float,
        fill_price: float,
    ) -> None:
        """Update average entry price and realized PnL for a position change."""

        # 1. Open a position
        if previous_inventory == 0:
            self.average_entry_price = fill_price
            return

        # 2. Scale in
        if previous_inventory * signed_quantity > 0:
            previous_notional = (
                abs(previous_inventory) * self._required_entry_price()
            )
            added_notional = abs(signed_quantity) * fill_price
            self.average_entry_price = (
                previous_notional + added_notional
            ) / abs(new_inventory)
            return

        # 3. Close part or all of the existing position.
        entry_price = self._required_entry_price()
        closing_quantity = min(
            abs(previous_inventory),
            abs(signed_quantity),
        )
        position_sign = 1.0 if previous_inventory > 0 else -1.0
        self.realized_pnl += (
            closing_quantity
            * (fill_price - entry_price)
            * position_sign
        )

        # 4. Reverse the position
        if previous_inventory * new_inventory < 0:
            self.average_entry_price = fill_price

    def _required_entry_price(self) -> float:
        """Return the entry price required whenever inventory is non-zero."""
        if self.average_entry_price is None:
            raise RuntimeError("non-zero inventory requires an entry price")
        return self.average_entry_price

    def mark_to_market(self, price: float) -> None:
        """Set the latest positive finite mark price."""
        if not math.isfinite(price) or price <= 0:
            raise ValueError("mark price must be finite and positive")
        self.mark_price = price

    def apply_funding_cashflow(self, amount: float) -> None:
        """Apply a signed funding settlement cashflow when one is available."""
        if not math.isfinite(amount):
            raise ValueError("funding cashflow must be finite")
        self.cumulative_funding_cashflow += amount

    @property
    def unrealized_pnl(self) -> float | None:
        """Return marked PnL on the open position."""
        if self.inventory == 0:
            return 0.0
        if self.mark_price is None:
            return None
        return self.inventory * (
            self.mark_price - self._required_entry_price()
        )

    @property
    def gross_trading_pnl(self) -> float | None:
        """Return realized plus unrealized trading PnL before cashflows."""
        if self.unrealized_pnl is None:
            return None
        return self.realized_pnl + self.unrealized_pnl

    @property
    def net_pnl(self) -> float | None:
        """Return trading PnL after fee and funding cashflows."""
        if self.gross_trading_pnl is None:
            return None
        return (
            self.gross_trading_pnl
            + self.cumulative_fee_cashflow
            + self.cumulative_funding_cashflow
        )

    @property
    def equity(self) -> float | None:
        """Return authoritative cash-plus-marked-inventory account equity."""
        if self.inventory != 0 and self.mark_price is None:
            return None
        marked_inventory = self.inventory * (self.mark_price or 0.0)
        return (
            self.cash
            + marked_inventory
            + self.cumulative_fee_cashflow
            + self.cumulative_funding_cashflow
        )
