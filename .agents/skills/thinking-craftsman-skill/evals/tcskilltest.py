import os

# dead code test
def dead_code():
    if False:
        print("This code is never called.")
    pass

class Inventory:
    """
    A simple class to manage a collection of items.
    This class demonstrates a violation of the guideline by returning
    a direct reference to its internal list.
    """
    def __init__(self, initial_items):
        self._items = list(initial_items)

    def get_items(self):
        """
        """
        return self._items

    def __str__(self):
        return f"Inventory contains: {self._items}"


class LSPBase:
    """
    Base behavior contract:
    - deposit(amount): accepts any positive number and returns new balance as float
    - get_daily_limit(): always returns a numeric daily limit
    """

    def __init__(self, starting_balance=0.0):
        self.balance = float(starting_balance)

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")
        self.balance += amount
        return self.balance

    def get_daily_limit(self):
        return 1000.0


class LSPViolator(LSPBase):
    """
    Intentionally violates LSP by changing base assumptions:
    - Strengthens precondition in deposit (only amount >= 100 allowed)
    - Weakens postcondition in get_daily_limit (returns None instead of number)
    """

    def deposit(self, amount):
        if amount < 100:
            raise ValueError("Amount must be at least 100 for this account.")
        return super().deposit(amount)

    def get_daily_limit(self):
        return None

