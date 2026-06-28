"""Protocol v2 — JSON-RPC 2.0 wire format with Thread/Turn/Item primitives.

Replaces v1's custom envelope (type+payload+seq+ts) with standard JSON-RPC
messages. Not backward compatible: the desktop frontend is unreleased.
"""
