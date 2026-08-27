"""Utilities for parsing and executing payload configurations."""
import json
import subprocess
import sys
from typing import Any, Dict, List

from .logging import service_log


def load_payload(payload_path: str) -> Dict[str, Any]:
    """
    Load payload JSON from a file path.

    TODO: the path load of json is for testing purpose, we will change it to load from S3 in the future.

    Parameters
    ----------
    payload_path : str
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
    with open(payload_path, "r") as f:
        return json.load(f)


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
