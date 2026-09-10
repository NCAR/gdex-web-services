"""Utility functions for file path validation."""

from pathlib import Path
from typing import List, Union, Dict, Any

from gdexws.utils import service_log

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
    full_paths = []
    for file_path in file_paths:
        try:
            full_path = (base_path / file_path).resolve()
            # This will raise ValueError if path escapes base directory
            full_path.relative_to(base_path)
            full_paths.append(str(full_path))
        except ValueError:
            return service_log("relpath-validate", "ERROR", f"Access denied: '{file_path}' escapes base directory")
    return full_paths
