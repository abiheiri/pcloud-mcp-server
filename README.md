# pCloud MCP Server

An MCP (Model Context Protocol) server that provides tools for interacting with the pCloud API.

## Features

- List folder contents
- Download files and folders to your local system
- Upload files and folders to pCloud
- Rename, move, and delete files and folders
- Manage trash (list and restore deleted items)

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

There are three ways to run this server with Claude Desktop.

### Option 1: Run with uv (simplest for development)

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

Replace `/path/to/uv` with your uv path (find with `which uv`) and `/path/to/pcloud_mcp` with the repository path.

### Option 2: Run with Docker directly

First, build the Docker image:

```bash
docker build -t pcloud-mcp:1.0 .
```

Then add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "pcloud": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "PCLOUD_USERNAME=youremail@example.com",
        "-e", "PCLOUD_PASSWORD=yourpassword",
        "-e", "PCLOUD_REGION=us",
        "pcloud-mcp:1.0"
      ]
    }
  }
}
```

### Option 3: Use Docker MCP Gateway (recommended for Docker Desktop users)

If you have Docker Desktop with MCP Gateway, you can register the server once and manage it through Docker.

1. Build the image:
   ```bash
   docker build -t pcloud-mcp:1.0 .
   ```

2. Import the catalog:
   ```bash
   docker mcp catalog import catalog.yaml
   ```
   Enter a name like `pcloud-local` when prompted.

3. Enable the server:
   ```bash
   docker mcp server enable pcloud
   ```

4. Set your credentials:
   ```bash
   docker mcp secret set pcloud.username=youremail@example.com
   docker mcp secret set pcloud.password=yourpassword
   docker mcp secret set pcloud.region=us
   ```

5. Connect Claude Desktop to the gateway (if not already):
   ```bash
   docker mcp client connect claude-desktop
   ```

The server will now appear automatically in Claude Desktop. Docker manages the container lifecycle, starting it when needed and stopping it when idle.

## What You Can Do

Once configured, just ask Claude things like:

- "Show me what's in my pCloud"
- "List all files in my Documents folder"
- "Download report.pdf to my Downloads folder"
- "Download my entire Photos folder"
- "Upload this file to my pCloud backup folder"
- "Upload my project folder to pCloud"
- "Rename my old folder to something new"
- "Move this file to another folder"
- "Delete that folder"
- "What's in my trash?"
- "Restore that deleted file"

Transfers run in the background, so you can keep chatting while files upload or download.

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
