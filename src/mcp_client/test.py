import asyncio
import logging
import sys
from src.mcp_client.client import MCPClient

# 1. Configure logging to use stderr
# This ensures that any logs from this script don't go to stdout,
# which would corrupt the MCP communication channel if this were a server.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr 
)

# 2. Specifically silence the MCP internal validation logs
# This stops the "Failed to parse JSONRPC message" walls of text
logging.getLogger("mcp").setLevel(logging.ERROR)
logger = logging.getLogger("mcp-client-terminal")

async def main():
    logger.info("Starting MCP Tool Discovery...")
    try:
        async with MCPClient() as client:
            tools = await client.discover_tools()
            
            # 3. Use formatted printing for the results
            logger.info(f"Successfully discovered {len(tools)} tools.")
            for tool in tools:
                print(f"[{tool.server_name}] Found tool: {tool.name}")
                
    except Exception as e:
        logger.error(f"An error occurred during discovery: {e}")

if __name__ == "__main__":
    asyncio.run(main())