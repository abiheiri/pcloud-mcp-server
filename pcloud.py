"""
pCloud MCP Server

An MCP server that provides tools for interacting with the pCloud API.
"""

import os
import sys
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Configure logging to stderr (critical for MCP servers using stdio transport)
# stdout is reserved for JSON-RPC messages, so all logging must go to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("pcloud-mcp")

# Initialize the MCP server
mcp = FastMCP("pcloud")


class PCloudAuth:
    """
    Manages pCloud authentication with automatic token generation and refresh.

    Authentication strategy:
    1. Store username/password as environment variables
    2. Generate token on first use
    3. Cache token in memory during session
    4. Catch auth errors and regenerate token automatically
    5. Use long expiration times (2 years)
    """

    # pCloud has two API endpoints based on user registration region
    API_HOSTS = {
        "us": "https://api.pcloud.com",
        "eu": "https://eapi.pcloud.com",
    }

    # Token expiration settings (in seconds)
    # authexpire: max 63072000 (2 years), default 31536000 (1 year)
    # authinactiveexpire: max 5356800 (~62 days), default 2678400 (~31 days)
    TOKEN_EXPIRE = 63072000  # 2 years
    TOKEN_INACTIVE_EXPIRE = 5356800  # ~62 days

    def __init__(self):
        self._token: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._api_host: str | None = None
        self._client: httpx.AsyncClient | None = None

    def _load_credentials(self) -> None:
        """Load credentials from environment variables."""
        self._username = os.environ.get("PCLOUD_USERNAME")
        self._password = os.environ.get("PCLOUD_PASSWORD")

        # Determine API host (default to US if not specified)
        region = os.environ.get("PCLOUD_REGION", "us").lower()
        if region not in self.API_HOSTS:
            logger.warning(f"Invalid PCLOUD_REGION '{region}', defaulting to 'us'")
            region = "us"
        self._api_host = self.API_HOSTS[region]

        if not self._username or not self._password:
            raise ValueError(
                "PCLOUD_USERNAME and PCLOUD_PASSWORD environment variables are required"
            )

        logger.info(f"Loaded credentials for {self._username}, using {region.upper()} API")

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _generate_token(self) -> str:
        """
        Generate a new authentication token using userinfo method.

        The userinfo method is recommended as a login entry point according to
        pCloud documentation. We use the getauth parameter to receive a token.
        """
        if self._username is None:
            self._load_credentials()

        client = await self._get_client()

        params = {
            "username": self._username,
            "password": self._password,
            "getauth": 1,  # Request auth token in response
            "authexpire": self.TOKEN_EXPIRE,
            "authinactiveexpire": self.TOKEN_INACTIVE_EXPIRE,
        }

        logger.info("Generating new authentication token...")

        response = await client.get(f"{self._api_host}/userinfo", params=params)
        response.raise_for_status()

        data = response.json()

        # Check for pCloud API errors
        result_code = data.get("result", 0)
        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            logger.error(f"Authentication failed: {error_msg}")
            raise ValueError(f"pCloud authentication failed: {error_msg}")

        token = data.get("auth")
        if not token:
            raise ValueError("No auth token returned from pCloud API")

        self._token = token
        logger.info("Successfully generated authentication token")
        return token

    async def get_token(self) -> str:
        """
        Get the current token, generating a new one if needed.
        """
        if self._token is None:
            await self._generate_token()
        return self._token

    async def invalidate_token(self) -> None:
        """
        Invalidate the current token, forcing regeneration on next use.
        """
        logger.info("Invalidating current authentication token")
        self._token = None

    async def make_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        retry_on_auth_error: bool = True,
    ) -> dict[str, Any]:
        """
        Make an authenticated request to the pCloud API.

        Automatically handles auth errors by regenerating the token and retrying.

        Args:
            method: The API method to call (e.g., "listfolder")
            params: Additional parameters for the API call
            retry_on_auth_error: Whether to retry on auth errors (default: True)

        Returns:
            The JSON response from the API

        Raises:
            ValueError: If the API returns an error
            httpx.HTTPError: If the HTTP request fails
        """
        token = await self.get_token()
        client = await self._get_client()

        request_params = params.copy() if params else {}
        request_params["auth"] = token

        logger.debug(f"Making request to {method} with params: {request_params.keys()}")

        response = await client.get(f"{self._api_host}/{method}", params=request_params)
        response.raise_for_status()

        data = response.json()
        result_code = data.get("result", 0)

        # Check for auth errors (1000 = login required, 2000 = login failed)
        if result_code in (1000, 2000) and retry_on_auth_error:
            logger.warning(f"Auth error (code: {result_code}), regenerating token...")
            await self.invalidate_token()
            return await self.make_request(method, params, retry_on_auth_error=False)

        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            logger.error(f"API error in {method}: {error_msg}")
            raise ValueError(f"pCloud API error: {error_msg}")

        return data

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Global auth instance
auth = PCloudAuth()


@mcp.tool()
async def list_folder(
    folder_id: int = 0,
    path: str | None = None,
    recursive: bool = False,
    show_deleted: bool = False,
    no_files: bool = False,
    no_shares: bool = False,
) -> dict[str, Any]:
    """
    List the contents of a folder in pCloud.

    Args:
        folder_id: The folder ID to list (default: 0 for root folder)
        path: Alternative to folder_id - the path to the folder (discouraged by pCloud)
        recursive: If True, return full directory tree with contents for all directories
        show_deleted: If True, show deleted files and folders that can be restored
        no_files: If True, return only folder structure without files
        no_shares: If True, show only user's own folders and files (no shared content)

    Returns:
        Dictionary containing folder metadata and contents array with metadata
        for each item (files and subfolders)

    Possible errors:
        - 1000: Authentication required
        - 1002: Neither path nor folderid provided
        - 2000: Authentication failed
        - 2003: Insufficient permissions
        - 2005: Folder does not exist
        - 4000: IP address rate-limited
        - 5000: Server error
    """
    params: dict[str, Any] = {}

    # Use path if provided, otherwise use folder_id
    if path is not None:
        params["path"] = path
        logger.info(f"Listing folder by path: {path}")
    else:
        params["folderid"] = folder_id
        logger.info(f"Listing folder by ID: {folder_id}")

    # Add optional parameters
    if recursive:
        params["recursive"] = 1
    if show_deleted:
        params["showdeleted"] = 1
    if no_files:
        params["nofiles"] = 1
    if no_shares:
        params["noshares"] = 1

    result = await auth.make_request("listfolder", params)

    # Log some stats about the result
    metadata = result.get("metadata", {})
    contents = metadata.get("contents", [])
    logger.info(f"Listed {len(contents)} items in folder")

    return result


def main():
    """Run the MCP server."""
    logger.info("Starting pCloud MCP server...")
    mcp.run()


if __name__ == "__main__":
    main()
