# pCloud MCP Server

An MCP (Model Context Protocol) server that provides tools for interacting with the pCloud API.

## Features

- This project is still ongoing, at the moment all it does is list folder contents.

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
        "pcloud.py"
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

## Available Tools

### list_folder

List the contents of a folder in pCloud.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `folder_id` | int | 0 | The folder ID to list (0 = root folder) |
| `path` | string | null | Alternative: path to the folder |
| `recursive` | bool | false | Return full directory tree |
| `show_deleted` | bool | false | Show deleted files that can be restored |
| `no_files` | bool | false | Return only folder structure |
| `no_shares` | bool | false | Exclude shared content |

**Example usage in Claude:**

- "List my pCloud root folder"
- "Show me all files in my Documents folder recursively"
- "List folder ID 12345"

## Authentication

The server uses pCloud's token authentication:

1. Credentials are read from environment variables on first API call
2. Token is cached in memory for the session duration
3. On auth errors (expired token), the token is automatically regenerated

## Development

Run the server directly:

```bash
source .venv/bin/activate
python pcloud.py
```

Test the imports:

```bash
python -c "from pcloud import mcp; print('Server:', mcp.name)"
```

## License

MIT License

Copyright (c) 2025 AL Biheiri

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
