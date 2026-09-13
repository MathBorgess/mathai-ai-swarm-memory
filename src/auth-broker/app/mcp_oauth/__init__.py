"""Remote MCP OAuth provider. Integration mounts the router and calls authorize()."""

from app.mcp_oauth.provider import McpOAuth, build_router

__all__ = ["McpOAuth", "build_router"]
