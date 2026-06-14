"""Build data/demo.db — a deterministic SQLite demo database.

Keeps the original repo's AtliQ t-shirt story but adds customers, orders and
order_items so time-series / join / ranking questions actually work.

    python scripts/create_demo_db.py
"""
from __future__ import annotations

import datetime as dt
import random
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "demo.db"

BRANDS = ["Nike", "Adidas", "Levi", "Van Heusen", "Puma"]
COLORS = ["White", "Black", "Blue", "Red", "Green"]
SIZES = ["XS", "S", "M", "L", "XL"]
CITIES = ["Bengaluru", "Pune", "Ranchi", "Mumbai", "Delhi", "Chennai", "Hyderabad"]
FIRST = ["Aman", "Ankit", "Priya", "Rahul", "Sneha", "Vikash", "Divya", "Karan",
         "Meera", "Arjun", "Pooja", "Rohit", "Anita", "Suresh", "Kavya", "Nikhil"]
LAST = ["Kumar", "Sharma", "Reddy", "Patel", "Iyer", "Singh", "Das", "Nair",
        "Gupta", "Verma", "Joshi", "Menon"]

SCHEMA = """
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS discounts;
DROP TABLE IF EXISTS t_shirts;

CREATE TABLE t_shirts (
    t_shirt_id     INTEGER PRIMARY KEY,
    brand          TEXT NOT NULL,
    color          TEXT NOT NULL,
    size           TEXT NOT NULL,
    price          REAL NOT NULL,
    stock_quantity INTEGER NOT NULL,
    UNIQUE (brand, color, size)
);

CREATE TABLE discounts (
    discount_id  INTEGER PRIMARY KEY,
    t_shirt_id   INTEGER NOT NULL REFERENCES t_shirts(t_shirt_id),
    pct_discount REAL NOT NULL CHECK (pct_discount BETWEEN 0 AND 100)
);

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    signup_date TEXT NOT NULL
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('completed','returned','cancelled'))
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    t_shirt_id    INTEGER NOT NULL REFERENCES t_shirts(t_shirt_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL
);
"""


def main() -> None:
    random.seed(42)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    # products -----------------------------------------------------------
    shirts = []
    for brand in BRANDS:
        for color in COLORS:
            for size in SIZES:
                if random.random() < 0.72:                  # not every combo exists
                    shirts.append((brand, color, size,
                                   random.choice([299, 399, 499, 599, 799, 999]),
                                   random.randint(5, 120)))
    conn.executemany(
        "INSERT INTO t_shirts (brand, color, size, price, stock_quantity) VALUES (?,?,?,?,?)",
        shirts,
    )
    ids = [r[0] for r in conn.execute("SELECT t_shirt_id FROM t_shirts")]

    conn.executemany(
        "INSERT INTO discounts (t_shirt_id, pct_discount) VALUES (?,?)",
        [(i, random.choice([5, 10, 15, 20, 25])) for i in random.sample(ids, k=len(ids) // 3)],
    )

    # customers ----------------------------------------------------------
    customers = []
    start = dt.date(2024, 6, 1)
    for _ in range(200):
        customers.append((
            f"{random.choice(FIRST)} {random.choice(LAST)}",
            random.choice(CITIES),
            (start + dt.timedelta(days=random.randint(0, 540))).isoformat(),
        ))
    conn.executemany(
        "INSERT INTO customers (name, city, signup_date) VALUES (?,?,?)", customers
    )

    # orders + items ------------------------------------------------------
    price_of = dict(conn.execute("SELECT t_shirt_id, price FROM t_shirts"))
    order_start = dt.date(2025, 1, 1)
    horizon = (dt.date(2026, 6, 1) - order_start).days
    for _ in range(1500):
        cust = random.randint(1, 200)
        when = order_start + dt.timedelta(days=int(random.triangular(0, horizon, horizon * 0.8)))
        status = random.choices(["completed", "returned", "cancelled"], [0.86, 0.09, 0.05])[0]
        cur = conn.execute(
            "INSERT INTO orders (customer_id, order_date, status) VALUES (?,?,?)",
            (cust, when.isoformat(), status),
        )
        oid = cur.lastrowid
        for tid in random.sample(ids, k=random.choices([1, 2, 3], [0.6, 0.3, 0.1])[0]):
            conn.execute(
                "INSERT INTO order_items (order_id, t_shirt_id, quantity, unit_price) VALUES (?,?,?,?)",
                (oid, tid, random.randint(1, 4), price_of[tid]),
            )

    conn.commit()
    n = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
         for t in ["t_shirts", "discounts", "customers", "orders", "order_items"]}
    conn.close()
    print(f"demo.db written to {DB_PATH}")
    print("  " + "  ".join(f"{k}={v}" for k, v in n.items()))


if __name__ == "__main__":
    main()
