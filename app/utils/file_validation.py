"""Utility functions for file path validation."""

import os
from pathlib import Path
from typing import List, Union, Dict, Any

from .dscheck_json import get_dscheck_json

# Absolute paths accepted by the read-only file endpoints must live under here.
GLADE_ROOT = "/glade/"


def glade_path_validate(path: str, root: str = None) -> str:
    """Resolve an absolute path and confirm it falls under the /glade/ mount.

    Symlinks and ``..`` are resolved with ``os.path.realpath`` before the
    prefix check, so neither can be used to escape the root.

    Parameters
    ----------
    path : str
        Absolute path supplied by the caller.
    root : str, optional
        Allowed root, with a trailing slash. Defaults to GLADE_ROOT, read at
        call time so tests can patch it.

    Returns
    -------
    str
        The resolved path.

    Raises
    ------
    ValueError
        If the path is not absolute or resolves outside the root.
    """
    root = root or GLADE_ROOT
    if not os.path.isabs(path):
        raise ValueError(f"Path must be absolute and under {root}")
    resolved = os.path.realpath(path)
    if resolved != root.rstrip("/") and not resolved.startswith(root):
        raise ValueError(f"Path must be under {root}")
    return resolved


def relpath_validate(
    file_paths: List[str],
    base_dir: str = '/glade/campaign/collections/gdex/data/exchange/'
) -> Union[bool, Dict[str, Any]]:
    """Validate that all relative file paths are within the base directory.

    Parameters
    ----------
    file_paths : List[str]
        List of relative file paths to validate.
    base_dir : str
        Base directory that all paths must be within.
        Default: '/glade/campaign/collections/gdex/data/exchange/'

    Returns
    -------
    bool or dict
        Returns True if all paths are valid.
        Returns standardized dscheck_json error response if any path is invalid.

    Examples
    --------
    >>> result = relpath_validate(["file1.nc", "file2.nc"])
    >>> if result is True:
    ...     # Process files
    ... else:
    ...     # result is an error dict, return to user
    ...     return result
    """
    base_path = Path(base_dir).resolve()

    for file_path in file_paths:
        try:
            full_path = (base_path / file_path).resolve()
            # This will raise ValueError if path escapes base directory
            full_path.relative_to(base_path)
        except ValueError:
            return get_dscheck_json(
                cindex=0,
                issuer=None,
                status_message=f"Access denied: '{file_path}' escapes base directory"
            )
    return True
