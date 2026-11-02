# inventory

A tiny inventory tool. An item is **low on stock** when its quantity is at or below its reorder level.

    python -m inventory report data/stock.csv     # totals and the low-stock list
    python -m inventory restock data/stock.csv B200 25

Run the tests with `python -m unittest discover -s tests`.
