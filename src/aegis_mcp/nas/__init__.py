"""NAS MCP server: confined filesystem ops, SMART, TrueNAS/Synology."""

from .server import NasSettings, build_server

__all__ = ["NasSettings", "build_server"]
