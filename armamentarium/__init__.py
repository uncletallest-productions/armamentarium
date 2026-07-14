"""armamentarium — portable Claude Desktop sessions + MCP brokering.

An *armamentarium* is the complete kit of tools and resources one brings to
bear on a task. Here it names a tool that carries a Claude Desktop session
across machines (Substrate NFS) and marshals whatever MCP/filesystem access is
available on the host it lands on into one working kit.

See README.md. The first shipped component is the MCP filesystem router
(``armamentarium.mcp_router``): a backend-routing MCP server that presents
generic filesystem tools and forwards them to whichever backend is live on the
host.
"""

__version__ = "0.1.0"
