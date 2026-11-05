"""Utility functions for file path validation."""

from pathlib import Path
from typing import Iterable, List, Union

PathLike = Union[str, Path]

DEFAULT_BASE_DIR = Path('/glade/campaign/collections/gdex/data/exchange/')


class PathValidationError(ValueError):
    """Raised when a path resolves outside the base directory."""


def relpath_validate(
    file_paths: Iterable[PathLike],
    base_dir: PathLike = DEFAULT_BASE_DIR
) -> List[Path]:
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
    list of pathlib.Path
        Fully resolved (symlink-free) absolute paths, in the same order as
        ``file_paths``. A single path comes back as a list of one: take ``[0]``.

    Raises
    ------
    PathValidationError
        If any path escapes ``base_dir``; the remaining paths are not checked.
        It is a ``ValueError`` subclass, and nothing is logged here: the caller
        decides whether to log, so a tool can catch it in its own ``try`` block.

    Examples
    --------
    >>> relpath_validate(["file1.nc", Path("sub/file2.nc")])
    [PosixPath('/glade/campaign/collections/gdex/data/exchange/file1.nc'),
     PosixPath('/glade/campaign/collections/gdex/data/exchange/sub/file2.nc')]
    >>> relpath_validate(["../secret.nc"])
    Traceback (most recent call last):
        ...
    PathValidationError: Access denied: '../secret.nc' escapes base directory
    """
    base_path = Path(base_dir).resolve()
    full_paths = []
    for file_path in file_paths:
        try:
            full_path = (base_path / Path(file_path)).resolve()
            # This will raise ValueError if path escapes base directory
            full_path.relative_to(base_path)
        except ValueError:
            raise PathValidationError(f"Access denied: '{file_path}' escapes base directory") from None
        full_paths.append(full_path)
    return full_paths
