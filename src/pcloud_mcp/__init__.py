"""
pCloud MCP Server

An MCP server that provides tools for interacting with the pCloud API.
"""

from .server import mcp


def main():
    """Main entry point for the package."""
    from .auth import logger
    logger.info("Starting pCloud MCP server...")
    mcp.run()


__all__ = ["main", "mcp"]
