"""Standalone, read-only Connector message counter."""

from .counter import count_messages, default_database_path

__all__ = ["count_messages", "default_database_path"]
__version__ = "1.0.0"
