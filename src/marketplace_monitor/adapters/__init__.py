"""Marketplace adapters — each an isolated implementation behind one interface."""

from .base import BaseAdapter, MarketplaceAdapter
from .registry import available, build_adapter

__all__ = ["BaseAdapter", "MarketplaceAdapter", "available", "build_adapter"]
