"""
pCloud authentication and logging configuration.

Handles token-based authentication with automatic refresh on expiry.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# Configure logging to stderr (critical for MCP servers using stdio transport)
# stdout is reserved for JSON-RPC messages, so all logging must go to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("pcloud-mcp")


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
        # Cache for path -> folder_id mapping to avoid repeated API calls
        self._folder_id_cache: dict[str, int] = {"/": 0}

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
            self._token = await self._generate_token()
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

    async def resolve_path_to_folder_id(self, path: str) -> int:
        """
        Resolve a pCloud path to a folder ID, creating folders as needed.

        Uses createfolderifnotexists with folderid + name (not path) as recommended
        by pCloud API documentation.

        Args:
            path: The pCloud path (e.g., "/Documents/Photos/2024")

        Returns:
            The folder ID of the final folder in the path
        """
        # Normalize path
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")

        if not path or path == "/":
            return 0  # Root folder

        # Check cache first
        if path in self._folder_id_cache:
            return self._folder_id_cache[path]

        # Split path and resolve each segment
        segments = [s for s in path.split("/") if s]
        current_folder_id = 0
        current_path = ""

        for segment in segments:
            current_path = f"{current_path}/{segment}"

            # Check cache for this intermediate path
            if current_path in self._folder_id_cache:
                current_folder_id = self._folder_id_cache[current_path]
                continue

            # Create folder if not exists using folderid + name (not path!)
            result = await self.make_request(
                "createfolderifnotexists",
                {"folderid": current_folder_id, "name": segment},
            )

            metadata = result.get("metadata", {})
            current_folder_id = metadata.get("folderid", 0)

            # Cache this path
            self._folder_id_cache[current_path] = current_folder_id

            if result.get("created"):
                logger.info(f"Created folder: {current_path} (id: {current_folder_id})")
            else:
                logger.debug(f"Folder exists: {current_path} (id: {current_folder_id})")

        return current_folder_id

    async def check_file_exists(self, folder_id: int, filename: str) -> dict[str, Any] | None:
        """
        Check if a file exists in a folder.

        Args:
            folder_id: The folder ID to check in
            filename: The filename to look for

        Returns:
            File metadata if exists, None otherwise
        """
        try:
            result = await self.make_request("listfolder", {"folderid": folder_id})
            contents = result.get("metadata", {}).get("contents", [])

            for item in contents:
                if not item.get("isfolder") and item.get("name") == filename:
                    return item

            return None
        except Exception:
            return None

    async def upload_file(
        self,
        local_path: Path,
        folder_id: int,
        filename: str | None = None,
        rename_if_exists: bool = False,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """
        Upload a file to pCloud using POST multipart/form-data.

        Args:
            local_path: Local path to the file
            folder_id: Target folder ID in pCloud
            filename: Override filename (defaults to local filename)
            rename_if_exists: If True, rename file if it already exists
            progress_callback: Optional callback(uploaded_bytes, total_bytes)

        Returns:
            The upload response with file metadata
        """
        token = await self.get_token()

        if filename is None:
            filename = local_path.name

        file_size = local_path.stat().st_size

        # Build form data
        params = {
            "auth": token,
            "folderid": str(folder_id),
            "filename": filename,
            "nopartial": "1",  # Don't save partial files on failure
        }

        if rename_if_exists:
            params["renameifexists"] = "1"

        # Create a streaming file reader with progress tracking
        async def file_reader():
            uploaded = 0
            with open(local_path, "rb") as f:
                while chunk := f.read(65536):  # 64KB chunks
                    uploaded += len(chunk)
                    if progress_callback:
                        progress_callback(uploaded, file_size)
                    yield chunk

        # Use a custom async client with no timeout for large uploads
        async with httpx.AsyncClient(timeout=None) as client:
            # For httpx, we need to read the file content
            # Using streaming upload with progress tracking
            with open(local_path, "rb") as f:
                files = {"file": (filename, f, "application/octet-stream")}

                response = await client.post(
                    f"{self._api_host}/uploadfile",
                    data=params,
                    files=files,
                )

        response.raise_for_status()
        data = response.json()

        result_code = data.get("result", 0)
        if result_code != 0:
            error_msg = data.get("error", f"Unknown error (code: {result_code})")
            raise ValueError(f"Upload failed: {error_msg}")

        return data


# Global auth instance
auth = PCloudAuth()
