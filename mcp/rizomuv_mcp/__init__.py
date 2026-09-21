"""MCP server for RizomUV: load, unfold, pack, measure and save UVs from an AI assistant.

Nothing is imported here on purpose: the server process must not load anything native
before the SDK has claimed stdout (see server.py), and the link worker imports only what
it needs.
"""
__version__ = "0.1.0"
