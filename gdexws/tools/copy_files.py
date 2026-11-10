"""Copy a file or directory into an existing exchange directory with gdexcp."""
import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from gdexws.tools.create_exchange_dir import EXCHANGE_DIR
from gdexws.utils import service_log
from gdexws.utils.file_validation import PathValidationError, relpath_validate

COMMAND_NAME = "copy-files"
DESCRIPTION = "Copy files or directories into an existing exchange directory with gdexcp"

# Directories a copy may be written into. To allow another GDEX location later,
# add its path here; nothing else in this module needs to change.
ALLOWED_TARGET_ROOTS = (EXCHANGE_DIR,)


class CopyFilesError(Exception):
    """Raised when the copy cannot be done safely or gdexcp fails."""


def _validate_target(target_dir: Path) -> Path:
    """Return the resolved target directory, which must sit inside an allowed root.

    Parameters
    ----------
    target_dir : pathlib.Path
        Absolute path, or a path relative to an allowed root.

    Returns
    -------
    pathlib.Path
        Fully resolved (symlink-free) target path.

    Raises
    ------
    CopyFilesError
        If the path escapes every root in ``ALLOWED_TARGET_ROOTS``.
    """
    # relpath_validate raises when the path escapes a root; try the next root instead.
    for root in ALLOWED_TARGET_ROOTS:
        try:
            return relpath_validate([target_dir], base_dir=root)[0]
        except PathValidationError:
            continue
    raise CopyFilesError(f"Target '{target_dir}' is not inside an allowed directory")


def _is_loop(real_dir: str, real_child: str) -> bool:
    """True if ``real_child`` is ``real_dir`` itself or one of its ancestors."""
    return real_dir == real_child or real_dir.startswith(real_child.rstrip(os.sep) + os.sep)


def _is_selected(name: str, include: List[str], exclude: List[str]) -> bool:
    """determine whether a file name passes the include and then the exclude patterns.
    Exclude patterns take precedence over include patterns.

    Patterns are shell-style wildcards (``*``, ``?``, ``[abc]``) matched against the
    file name only, not its path, and case-sensitively.

    Parameters
    ----------
    name : str
        File name without any directory, e.g. ``"history.0001.nc"``.
    include : list of str
        If not empty, the name must match at least one of these.
    exclude : list of str
        The name must match none of these. Exclude wins over include.

    Returns
    -------
    bool

    Examples
    --------
    >>> _is_selected("run.h0.nc", ["*h0*"], [])
    True
    >>> _is_selected("run.h1.nc", ["*h0*"], [])
    False
    >>> _is_selected("run.h0.tmp", ["*h0*"], ["*.tmp"])
    False
    >>> _is_selected("anything", [], [])
    True
    """
    # An include list narrows the files; with no include list, every file is a candidate.
    if include:
        matches_include = False
        for pattern in include:
            if fnmatch.fnmatchcase(name, pattern):
                matches_include = True
                break
        if not matches_include:
            return False

    # Exclude always wins, even if the name also matched an include pattern.
    for pattern in exclude:
        if fnmatch.fnmatchcase(name, pattern):
            return False

    return True


def _plan_copies(
    source: Path,
    target_dir: Path,
    follow_symlinks: bool = False,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None
) -> List[Tuple[Path, Path]]:
    """List every ``(source_file, destination_file)`` pair to copy.

    A file goes to ``target_dir/<its name>``. The contents of a directory go straight
    into ``target_dir`` with their layout kept, without the directory's own name
    (``source/a/b.nc`` -> ``target_dir/a/b.nc``), like ``cp -r source/. target_dir``.
    Empty directories are skipped. Symlinks are skipped unless
    ``follow_symlinks`` is set. Then symlinked directories are walked and symlinked
    files are copied by content, and each one lands where the symlink sits in the
    source tree, not where it points.

    Parameters
    ----------
    source : pathlib.Path
        Existing file or directory. Not resolved, so a symlinked file keeps its own name.
    target_dir : pathlib.Path
        Existing, validated destination directory.
    follow_symlinks : bool
        Follow symlinks inside a directory source. Links that point back at the
        directory holding them (loops) and broken links are skipped with a warning.
    include, exclude : list of str, optional
        File name patterns, see ``_is_selected``. They apply to files only; directories
        are always walked.

    Returns
    -------
    list of tuple of pathlib.Path
    """
    if include is None:
        include = []
    if exclude is None:
        exclude = []

    if source.is_file():
        if not _is_selected(source.name, include, exclude):
            return []
        return [(source, target_dir / source.name)]

    pairs = []
    # os.walk reports symlinked directories but only descends into them with followlinks.
    for dirpath, dirnames, filenames in os.walk(source, followlinks=follow_symlinks):
        here = Path(dirpath)

        # skip a link that points back at the directory holding it which could create infinite loops
        if follow_symlinks:
            real_here = os.path.realpath(dirpath)
            for name in list(dirnames):
                if _is_loop(real_here, os.path.realpath(here / name)):
                    service_log(COMMAND_NAME, "WARNING", "Skipping symlink loop", path=str(here / name))
                    dirnames.remove(name)

        for name in sorted(filenames):
            # Check the name first: it is cheap and skips the symlink checks and
            # warnings for files that were filtered out anyway.
            if not _is_selected(name, include, exclude):
                continue

            src_file = here / name
            if src_file.is_symlink() and not (follow_symlinks and src_file.exists()):
                service_log(COMMAND_NAME, "WARNING", "Skipping symlink", path=str(src_file))
                continue
            pairs.append((src_file, target_dir / here.relative_to(source) / name))

    return pairs


def copy_files(
    source: Path,
    target: Path,
    follow_symlinks: bool = False,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    dry_run: bool = False,
    debug: bool = False
) -> List[Path]:
    """Copy ``source`` into the existing directory ``target`` with gdexcp.

    - ``target`` must already exist (create it with ``gdexws create-exchange-dir``)
      and be inside ``ALLOWED_TARGET_ROOTS``.
    - A file keeps its name.
    - A directory is copied recursively: its contents go straight into ``target``
      with their layout kept (no new subfolder named after the directory).
    - Symlinks inside a directory are skipped unless ``follow_symlinks`` is set.
    - Every file is attempted even if an earlier one fails; failures are reported
      together at the end.
    - Missing subdirectories are created by gdexcp, not here, so they are owned by
      ``gdexdata`` (a directory made by the caller would not be writable by it).
    - gdexcp's defaults: directories 775 (``-D``), files 664 (``-F``).
    - Inside a ``create-exchange-dir`` directory, the default ACL gives the granted
      user access too; on files it is rw, because 664 sets the ACL mask to rw.

    Parameters
    ----------
    source : pathlib.Path
        File or directory to copy. A str is also accepted and converted.
    target : pathlib.Path
        Existing destination directory: absolute, or relative to an allowed root.
        A str is also accepted and converted.
    follow_symlinks : bool
        Walk symlinked directories and copy symlinked files by content, keeping the
        layout of the tree that holds the links. Loops and broken links are skipped.
        Only follow links you trust: a link can point anywhere you can read.
    include : list of str, optional
        Wildcard patterns such as ``["*.nc", "history.*"]``. If given, only files whose
        name matches at least one are copied. Default: all files.
    exclude : list of str, optional
        Wildcard patterns for files to skip, e.g. ``["*.tmp"]``. Wins over ``include``.
        Both match the file name only (not the path), and files only (not directories).
    dry_run : bool
        Log the gdexcp commands without running them.
    debug : bool
        Emit DEBUG log lines.

    Returns
    -------
    list of pathlib.Path
        Destination file paths that were copied (or would be, for a dry run).

    Raises
    ------
    CopyFilesError
        If the target is outside the allowed roots or missing, the source is
        missing, gdexcp is not on PATH, there is nothing to copy (an empty directory,
        or every file filtered out by ``include``/``exclude``), or any copy fails.

    Examples
    --------
    From the command line:

    $ gdexws copy-files /glade/derecho/scratch/jdoe/run1.nc mydata
    $ gdexws copy-files /glade/derecho/scratch/jdoe/run1 mydata   # run1/x.nc -> mydata/x.nc
    $ gdexws copy-files /glade/derecho/scratch/jdoe/run1 mydata --include '*.nc' --exclude '*h1*'

    Quote the patterns, or the shell expands them against the current directory first.

    From Python (``mydata`` already created under the exchange directory):

    >>> copy_files(Path("/glade/derecho/scratch/jdoe/run1.nc"), Path("mydata"))
    [PosixPath('/glade/campaign/collections/gdex/data/exchange/mydata/run1.nc')]
    >>> copy_files(Path("/glade/derecho/scratch/jdoe/run1"), Path("mydata"), include=["history.*"])
    """
    # No-op for a Path; converts a str. None, ints, etc. raise TypeError.
    source = Path(source)
    target = Path(target)

    target_dir = _validate_target(target)
    if not target_dir.is_dir():
        raise CopyFilesError(f"Target directory does not exist: {target_dir}")

    if not source.exists():
        raise CopyFilesError(f"Source does not exist: {source}")

    exe = "gdexcp"
    if not dry_run:
        exe = shutil.which("gdexcp")
        if exe is None:
            raise CopyFilesError("Required tool 'gdexcp' was not found on PATH")

    # abspath, not resolve(): a symlinked file source keeps the link's name.
    pairs = _plan_copies(Path(os.path.abspath(source)), target_dir, follow_symlinks, include, exclude)
    if not pairs:
        # An empty plan usually means a wrong path or filter; fail so a PBS job notices.
        raise CopyFilesError(f"No files to copy from {source} (empty, or all filtered out)")

    failed: List[Path] = []  # source files that could not be copied
    for src_file, dest_file in pairs:
        command = [exe, "-f", str(src_file), "-t", str(dest_file)]
        if dry_run:
            service_log(COMMAND_NAME, "INFO", "Dry run: would run command " + " ".join(command))
            continue
        try:
            if debug:
                service_log(COMMAND_NAME, "DEBUG", "Copying", source=str(src_file), target=str(dest_file))
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            failed.append(src_file)
            service_log(COMMAND_NAME, "ERROR", "Copy failed", source=str(src_file), error=str(exc))
            continue
        if result.returncode != 0:
            failed.append(src_file)
            service_log(COMMAND_NAME, "ERROR", "gdexcp failed", source=str(src_file),
                        error=result.stderr.strip())

    if failed:
        raise CopyFilesError(f"{len(failed)} of {len(pairs)} file(s) failed to copy check the log for detail list")
    service_log(COMMAND_NAME, "INFO", process_message=f"Copy completed for {len(pairs)} file(s) to {target_dir}")
    # Nothing failed, so every planned destination was copied (or would be, for a dry run).
    return [dest_file for _, dest_file in pairs]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare CLI arguments on the `copy-files` subparser."""
    parser.add_argument("-s", "--source", required=True, help="File or directory to copy")
    parser.add_argument("-t", "--target", required=True, help="Existing exchange directory to copy into")
    parser.add_argument("-L", "--follow-symlinks", action="store_true",
                        help="Follow symlinks inside a directory, keeping the layout that holds the links")
    parser.add_argument("--include", action="append", metavar="PATTERN",
                        help="Copy only file names matching this wildcard, e.g. '*.nc' (repeatable; quote it)")
    parser.add_argument("--exclude", action="append", metavar="PATTERN",
                        help="Skip file names matching this wildcard (repeatable; quote it; wins over --include)")
    parser.add_argument("--dry-run", action="store_true", help="Log the gdexcp commands without running them")


def run(args: argparse.Namespace) -> None:
    """Execute the tool with parsed arguments."""
    try:
        copy_files(Path(args.source), Path(args.target), follow_symlinks=args.follow_symlinks,
                   include=args.include, exclude=args.exclude, dry_run=args.dry_run, debug=args.debug)
    except CopyFilesError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc), source=args.source, target=args.target)
        sys.exit(1)
