"""Utility functions for file path validation."""

from pathlib import Path
from typing import Iterable, List, Optional, Union

from gdexws.utils import service_log

PathLike = Union[str, Path]

DEFAULT_BASE_DIR = Path('/glade/campaign/collections/gdex/data/exchange/')

def relpath_validate(
    file_paths: Iterable[PathLike],
    base_dir: PathLike = DEFAULT_BASE_DIR
) -> Optional[List[Path]]:
    """Resolve file paths against the base directory and check they stay inside it.

    Parameters
    ----------
    file_paths : Iterable of str or pathlib.Path
        Paths relative to ``base_dir``. An absolute path is also accepted, but
        only if it points inside ``base_dir``. Each item is converted to a
        ``Path``, so strings still work.
    base_dir : str or pathlib.Path
        Base directory that all paths must be within; a str is converted to a ``Path``.
        Default: ``DEFAULT_BASE_DIR`` ('/glade/campaign/collections/gdex/data/exchange/')

    Returns
    -------
    list of pathlib.Path or None
        Fully resolved (symlink-free) absolute paths, in the same order as
        ``file_paths``, if all of them are inside ``base_dir``.
        ``None`` if any path escapes it; an ERROR line is logged and the
        remaining paths are not checked.

    Examples
    --------
    >>> relpath_validate(["file1.nc", Path("sub/file2.nc")])
    [PosixPath('/glade/campaign/collections/gdex/data/exchange/file1.nc'),
     PosixPath('/glade/campaign/collections/gdex/data/exchange/sub/file2.nc')]
    >>> relpath_validate(["../secret.nc"]) is None
    True
    """
    base_path = Path(base_dir).resolve()
    full_paths = []
    for file_path in file_paths:
        try:
            full_path = (base_path / Path(file_path)).resolve()
            # This will raise ValueError if path escapes base directory
            full_path.relative_to(base_path)
            full_paths.append(full_path)
        except ValueError:
            service_log("file-validation", "ERROR", f"Access denied: '{file_path}' escapes base directory")
            return None
    return full_paths
