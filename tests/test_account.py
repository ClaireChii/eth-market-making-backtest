"""Tests for fill validation and average-cost account bookkeeping."""

import unittest

from src.account import Account
from src.fills import Fill
from src.orders import OrderSide


def fill(
    side: OrderSide,
    price: float,
    quantity: float,
    fee_cashflow: float = 0.0,
) -> Fill:
    """Build a valid fill with concise test inputs."""
    return Fill(
        timestamp_ns=100,
        order_id="order-1",
        side=side,
        price=price,
        quantity=quantity,
        fee_cashflow=fee_cashflow,
    )


class AccountTest(unittest.TestCase):
    """Verify inventory, average cost, and PnL across position changes."""

    def assert_accounting_identity(self, account: Account) -> None:
        """Check that analytical net PnL equals authoritative equity."""
        self.assertIsNotNone(account.net_pnl)
        self.assertIsNotNone(account.equity)
        self.assertAlmostEqual(account.net_pnl, account.equity)

    def test_open_and_add_to_long_position(self) -> None:
        """Use a quantity-weighted average price when adding inventory."""
        account = Account()
        account.apply_fill(fill(OrderSide.BUY, 100.0, 1.0))
        account.apply_fill(fill(OrderSide.BUY, 120.0, 1.0))
        account.mark_to_market(115.0)

        self.assertEqual(account.cash, -220.0)
        self.assertEqual(account.inventory, 2.0)
        self.assertEqual(account.average_entry_price, 110.0)
        self.assertEqual(account.realized_pnl, 0.0)
        self.assertEqual(account.unrealized_pnl, 10.0)
        self.assert_accounting_identity(account)

    def test_partial_and_complete_long_close(self) -> None:
        """Realize PnL while preserving cost on remaining long inventory."""
        account = Account()
        account.apply_fill(fill(OrderSide.BUY, 100.0, 1.0))

        account.apply_fill(fill(OrderSide.SELL, 110.0, 0.4))

        self.assertAlmostEqual(account.inventory, 0.6)
        self.assertEqual(account.average_entry_price, 100.0)
        self.assertEqual(account.realized_pnl, 4.0)

        account.apply_fill(fill(OrderSide.SELL, 120.0, 0.6))

        self.assertEqual(account.inventory, 0.0)
        self.assertIsNone(account.average_entry_price)
        self.assertEqual(account.realized_pnl, 16.0)
        self.assertEqual(account.equity, 16.0)
        self.assert_accounting_identity(account)

    def test_long_position_can_flip_to_short(self) -> None:
        """Close the old side and assign fill price to the new short side."""
        account = Account()
        account.apply_fill(fill(OrderSide.BUY, 100.0, 1.0))

        account.apply_fill(fill(OrderSide.SELL, 110.0, 1.5))
        account.mark_to_market(100.0)

        self.assertEqual(account.inventory, -0.5)
        self.assertEqual(account.average_entry_price, 110.0)
        self.assertEqual(account.realized_pnl, 10.0)
        self.assertEqual(account.unrealized_pnl, 5.0)
        self.assert_accounting_identity(account)

    def test_short_position_realizes_profit_when_bought_lower(self) -> None:
        """Use the correct realized-PnL sign when closing short inventory."""
        account = Account()
        account.apply_fill(fill(OrderSide.SELL, 100.0, 1.0))
        account.apply_fill(fill(OrderSide.BUY, 90.0, 1.0))

        self.assertEqual(account.realized_pnl, 10.0)
        self.assertEqual(account.inventory, 0.0)
        self.assertEqual(account.equity, 10.0)
        self.assert_accounting_identity(account)

    def test_fee_and_funding_cashflows_are_separate(self) -> None:
        """Attribute fee and funding cashflows without changing trading PnL."""
        account = Account()
        account.apply_fill(
            fill(
                OrderSide.BUY,
                price=100.0,
                quantity=1.0,
                fee_cashflow=-0.1,
            )
        )
        account.mark_to_market(105.0)
        account.apply_funding_cashflow(0.25)

        self.assertEqual(account.gross_trading_pnl, 5.0)
        self.assertEqual(account.cumulative_fee_cashflow, -0.1)
        self.assertEqual(account.cumulative_funding_cashflow, 0.25)
        self.assertAlmostEqual(account.net_pnl, 5.15)
        self.assert_accounting_identity(account)

    def test_open_inventory_requires_a_mark_for_pnl(self) -> None:
        """Avoid inventing unrealized PnL before a mark price is available."""
        account = Account()
        account.apply_fill(fill(OrderSide.BUY, 100.0, 1.0))

        self.assertIsNone(account.unrealized_pnl)
        self.assertIsNone(account.net_pnl)
        self.assertIsNone(account.equity)

    def test_invalid_fill_is_rejected(self) -> None:
        """Reject invalid fill side, price, and quantity values."""
        with self.assertRaises(TypeError):
            Fill(100, "order-1", "buy", 100.0, 1.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            fill(OrderSide.BUY, price=0.0, quantity=1.0)
        with self.assertRaises(ValueError):
            fill(OrderSide.BUY, price=100.0, quantity=0.0)


if __name__ == "__main__":
    unittest.main()
