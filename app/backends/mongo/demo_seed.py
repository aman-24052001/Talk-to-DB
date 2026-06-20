"""Seed data for the bundled, zero-setup Mongo demo (database.url ==
'mongomock://demo'). Deliberately complements the SQL demo's t_shirts/
customers/orders rather than duplicating it: product_reviews and
support_tickets are the kind of semi-structured, schema-varying data that
doesn't fit a relational table well, which is also the more honest story
for *why* you'd reach for Mongo instead of SQL in the first place.

References customer_id 1-200 and t_shirt_id 1-89 — the exact ranges
scripts/create_demo_db.py produces with its fixed random.seed(42), so a
question spanning both demo backends (e.g. "which low-stock t-shirts have
the worst reviews?") has a real, joinable answer.
"""
from __future__ import annotations

import datetime as dt
import random

_COMMENTS_POSITIVE = [
    "Great fit, exactly as described.", "Fast delivery, good quality fabric.",
    "Color is a bit different from the photo but still nice.",
    "Best t-shirt I've bought this year.", "Comfortable, true to size.",
]
_COMMENTS_NEGATIVE = [
    "Shrunk after the first wash.", "Color faded quickly.",
    "Size runs smaller than expected.", "Stitching came undone within a week.",
    "Not worth the price.",
]
_TICKET_SUBJECTS = [
    "Order arrived damaged", "Wrong size delivered", "Refund not processed",
    "Where is my order?", "Want to exchange for a different color",
    "Discount code not applying", "Late delivery complaint",
]


def seed(db) -> None:
    """Insert demo documents into an empty mongomock database. No-op if
    product_reviews already has data (idempotent across repeated lifespan
    starts within the same process)."""
    if db.product_reviews.count_documents({}) > 0:
        return

    random.seed(42)  # same seed as scripts/create_demo_db.py, for a stable demo

    reviews = []
    review_start = dt.date(2025, 2, 1)
    for t_shirt_id in range(1, 90):
        if random.random() < 0.4:  # not every product has reviews — realistic + tests presence%
            for _ in range(random.randint(1, 4)):
                rating = random.choices([5, 4, 3, 2, 1], [0.35, 0.25, 0.15, 0.15, 0.10])[0]
                comment = random.choice(_COMMENTS_POSITIVE if rating >= 4 else _COMMENTS_NEGATIVE)
                reviews.append({
                    "t_shirt_id": t_shirt_id,
                    "customer_id": random.randint(1, 200),
                    "rating": rating,
                    "comment": comment,
                    "reviewed_at": (review_start + dt.timedelta(
                        days=random.randint(0, 480))).isoformat(),
                })
    if reviews:
        db.product_reviews.insert_many(reviews)

    tickets = []
    ticket_start = dt.date(2025, 3, 1)
    for _ in range(40):
        has_priority = random.random() < 0.5   # deliberately sparse field
        ticket = {
            "customer_id": random.randint(1, 200),
            "subject": random.choice(_TICKET_SUBJECTS),
            "status": random.choices(["open", "resolved"], [0.3, 0.7])[0],
            "created_at": (ticket_start + dt.timedelta(
                days=random.randint(0, 450))).isoformat(),
        }
        if has_priority:
            ticket["priority"] = random.choice(["low", "medium", "high"])
        tickets.append(ticket)
    db.support_tickets.insert_many(tickets)
