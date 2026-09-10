"""Pydantic models for request/response schemas across all endpoints."""

from typing import Annotated, List
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_files(file_paths: List[str]) -> List[str]:
    """Validate that all file paths are relative and don't escape base directory.

    Parameters
    ----------
    file_paths : List[str]
        List of file paths to validate.

    Returns
    -------
    List[str]
        The validated file paths.

    Raises
    ------
    ValueError
        If any path is absolute or contains path traversal attempts.
    """
    for file_path in file_paths:
        if file_path.startswith('/'):
            raise ValueError(f"File paths must be relative, got absolute path: {file_path}")
        if ".." in file_path:
            raise ValueError(f"Path traversal not allowed: {file_path}")
    return file_paths


class Command(BaseModel):
    """Model for a transformation command with flexible attributes.

    Allows any key-value pairs in addition to the required 'command' field.
    This enables support for different command types and their specific parameters.
    """
    command: str

    model_config = ConfigDict(extra='allow')

class TransformRequest(BaseModel):
    """Request model for transformation operations.

    demo json payload format :
    {
        "Files": ["Web-services/test.nc"],
        "Commands": [
            {
            "command": "add_global_meta",
            "global-attr-name": "gdex_dsid",
            "global-attr-value": "dPayLoadTest",
            "debug": true
            }
        ]
    }
    """
    # validate_files is used to ensure that all file paths are relative and do not contain path traversal attempts.
    files: Annotated[List[str], AfterValidator(validate_files)] = Field(alias="Files")
    commands: List[Command] = Field(alias="Commands")
