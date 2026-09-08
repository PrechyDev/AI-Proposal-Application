from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

PAGE_SIZE = 20


@dataclass
class Page:
    page: int
    total_pages: int
    total_count: int


def paginate(db: Session, base_query: Select, order_by, page: int) -> tuple[list, Page]:
    """`base_query` is a `select()` with every WHERE filter already applied,
    but no `order_by`/`limit`/`offset` yet - counting needs the unordered,
    unsliced query, and applying `order_by` before wrapping in a COUNT
    subquery is needless work some backends complain about. `order_by` is
    applied only to the actual page fetch below.
    """
    total_count = db.execute(select(func.count()).select_from(base_query.subquery())).scalar_one()
    total_pages = max(1, -(-total_count // PAGE_SIZE))  # ceil division
    page = min(max(1, page), total_pages)
    rows = db.execute(
        base_query.order_by(order_by).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    ).scalars().all()
    return rows, Page(page=page, total_pages=total_pages, total_count=total_count)
