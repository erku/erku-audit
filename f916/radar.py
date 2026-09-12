"""Cheap deterministic radar. Classification stays separate from fetching."""
from __future__ import annotations


def summarize(listings, grants):
    rows = []
    for item in listings if isinstance(listings, list) else []:
        rows.append({"kind":"listing", "id":item.get("id"), "title":item.get("title"),
                     "funder":item.get("funder") or item.get("author"), "asset":item.get("asset"),
                     "amount":item.get("amount") or item.get("price")})
    for item in grants if isinstance(grants, list) else []:
        rows.append({"kind":"grant", "slug":item.get("slug"), "title":item.get("title"), "state":item.get("state")})
    return rows
