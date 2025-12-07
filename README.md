# pCloud MCP Server

An MCP (Model Context Protocol) server that provides tools for interacting with the pCloud API.

## Features

- List folder contents
- Download files to local system (async with progress tracking)
- Download entire folders with all contents (preserves directory structure)
- Upload files to pCloud (async with progress tracking)
- Upload entire folders (auto-creates remote directories)
- Conflict handling for uploads: skip, overwrite, or rename existing files

## Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/abiheiri/pcloud-mcp-server.git
   cd pcloud_mcp
   ```

2. Create virtual environment and install dependencies:

   ```bash
   uv venv
   uv add "mcp[cli]" httpx
   ```

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PCLOUD_USERNAME` | Yes | Your pCloud account email |
| `PCLOUD_PASSWORD` | Yes | Your pCloud account password |
| `PCLOUD_REGION` | No | API region: `us` (default) or `eu` |

**Note:** Use `eu` if your pCloud account was registered in Europe, otherwise use `us`.

## Usage with Claude Desktop

Add the following to your Claude Desktop configuration file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "pcloud": {
      "command": "/path/to/uv",
      "args": [
        "--directory",
        "/path/to/pcloud_mcp",
        "run",
        "pcloud-mcp"
      ],
      "env": {
        "PCLOUD_USERNAME": "youremail@example.com",
        "PCLOUD_PASSWORD": "yourpassword",
        "PCLOUD_REGION": "us"
      }
    }
  }
}
```

Replace:

- `/path/to/uv` with the actual path to your `uv` executable (find it with `which uv`)
- `/path/to/pcloud_mcp` with the actual path to this repository
- Credentials with your actual pCloud account details

## What You Can Do

Once configured, just ask Claude things like:

- "Show me what's in my pCloud"
- "List all files in my Documents folder"
- "Download report.pdf to my Downloads folder"
- "Download my entire Photos folder"
- "Upload this file to my pCloud backup folder"
- "Upload my project folder to pCloud"
- "What's the upload progress?"
- "Cancel that transfer"

Transfers happen in the background, so you can keep chatting while files upload or download.

## Authentication

The server uses pCloud's token authentication:

1. Credentials are read from environment variables on first API call
2. Token is cached in memory for the session duration
3. On auth errors (expired token), the token is automatically regenerated

## Development

Run the server directly:

```bash
uv run pcloud-mcp
```

Test the imports:

```bash
uv run python -c "from pcloud_mcp import mcp; print('Server:', mcp.name)"
```

## License

[MIT License](LICENSE)
