"""Utilities for parsing and executing payload configurations."""
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

try:
    import httpx
except ImportError:
    httpx = None

from .logging import service_log


def load_payload(payload_path: str, timeout: float = 10.0) -> Dict[str, Any]:
    """
    Load payload JSON from a file path or URL.

    Accepts both local file paths (POSIX) and URLs (http/https).
    Automatically detects the input type and loads accordingly.

    Parameters
    ----------
    payload_path : str
        Either a local file path (POSIX) or a URL (http/https)
    timeout : float
        Timeout in seconds for HTTP requests. Default: 10.0

    Returns
    -------
    dict
        Dictionary containing payload configuration

    Raises
    ------
    FileNotFoundError
        If the local file does not exist
    json.JSONDecodeError
        If the content is not valid JSON
    httpx.RequestError
        If the HTTP request fails
    ImportError
        If loading from URL but httpx is not installed
    """
    # Check if it's a URL
    if payload_path.startswith('http://') or payload_path.startswith('https://'):
        return _load_payload_from_url(payload_path, timeout)
    else:
        return _load_payload_from_file(payload_path)


def _load_payload_from_file(file_path: str) -> Dict[str, Any]:
    """Load payload JSON from a local file.

    Parameters
    ----------
    file_path : str
        Path to the payload JSON file

    Returns
    -------
    dict
        Dictionary containing payload configuration

    Raises
    ------
    FileNotFoundError
        If the payload file does not exist
    json.JSONDecodeError
        If the file is not valid JSON
    """
    with open(file_path, "r") as f:
        return json.load(f)


def _load_payload_from_url(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Load payload JSON from a URL.

    Parameters
    ----------
    url : str
        HTTP or HTTPS URL to fetch payload JSON from
    timeout : float
        Timeout in seconds for the request

    Returns
    -------
    dict
        Dictionary containing payload configuration

    Raises
    ------
    ImportError
        If httpx is not installed
    httpx.RequestError
        If the HTTP request fails
    json.JSONDecodeError
        If the response is not valid JSON
    """
    if httpx is None:
        raise ImportError(
            "httpx is required for loading payload from URLs. "
            "Install with: pip install httpx"
        )

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def build_command(command_name: str, params: Dict[str, Any], file_path: str) -> List[str]:
    """
    Build a CLI command with arguments.

    Parameters
    ----------
    command_name : str
        Name of the CLI command
    params : dict
        Dictionary of parameters (excluding "command" key)
    file_path : str
        Path to the file to process

    Returns
    -------
    list
        List of command and arguments ready for subprocess
    """
    cmd = [command_name]

    # Add file argument
    cmd.extend(["-f", file_path])

    # Add other parameters
    for key, value in params.items():
        # Skip the command key itself
        if key == "command":
            continue

        # Convert underscore to hyphen for CLI arguments
        cli_key = f"--{key.replace('_', '-')}"

        # Handle boolean flags
        if isinstance(value, bool):
            if value:
                cmd.append(cli_key)
        else:
            cmd.extend([cli_key, str(value)])

    return cmd


def execute_command(cmd: List[str]) -> int:
    """
    Execute a CLI command.

    Parameters
    ----------
    cmd : list
        List of command and arguments

    Returns
    -------
    int
        Return code from subprocess
    """
    service_log(
        command_name="execute-command",
        level="INFO",
        process_message="Executing command",
        command=" ".join(cmd)
    )
    result = subprocess.run(cmd, capture_output=False, check=True)
    return result.returncode
