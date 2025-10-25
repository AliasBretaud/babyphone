"""Compatibility helpers for protobuf API changes."""

from __future__ import annotations

from google.protobuf import message_factory, symbol_database


if not getattr(symbol_database, "_patched_get_prototype", False):
    def _get_message_class(self, descriptor):
        return message_factory.GetMessageClass(descriptor)

    symbol_database.SymbolDatabase.GetPrototype = _get_message_class
    symbol_database._patched_get_prototype = True
