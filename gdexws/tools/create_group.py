"""Generate the group settings JSON for a dataset by scanning its source directory.

Each subdirectory where the tree branches becomes a group. Files matching the
``--include`` wildcards (e.g. ``diag.*.nc``) are attached to the group of the
directory that holds them.

TODO: create or update the group index in dsarch from the generated settings.
"""
import argparse
import sys
from pathlib import Path

from gdexws.utils import service_log
from gdexws.utils.auto_group_creation import create

COMMAND_NAME = "create-group"
DESCRIPTION = "Generate the group settings JSON for a dataset by scanning its source directory"


class CreateGroupError(Exception):
    """Raised when the group settings cannot be generated or saved."""


class _ServiceLogger:
    """Adapter so ``create()``'s ``logger.error(...)`` calls go to the jsonl log."""

    def error(self, message: str) -> None:
        # create() logs and skips these files, so the run can still succeed.
        service_log(COMMAND_NAME, "WARNING", message)


def create_group(source: Path, output: Path, existing: Path | None = None, include=None,
                 exclude=None, one_group: bool = False, debug: bool = False) -> dict:
    """Scan ``source`` and save its group settings to ``output``.

    Parameters
    ----------
    source : pathlib.Path
        Root directory of the dataset's original data.
    output : pathlib.Path
        JSON file to write. An existing file is overwritten.
    existing : pathlib.Path, optional
        Group settings JSON from an earlier run. Groups already in it keep their
        ``group_index``; new groups are numbered after the highest one.
        Default: None (start from an empty settings, indices from 1).
    include : list of str, optional
        Wildcards for the files to put in each group, e.g. ``["diag.*.nc"]``.
        Default: every file (``["*"]``).
    exclude : list of str, optional
        Folders or files to leave out, e.g. ``["tmp*", "*.log", "runA/scratch"]``.
        A pattern without ``/`` matches a name at any depth; one with ``/`` matches
        that path relative to ``source``. Excluded folders are not scanned, so they
        never become groups. The patterns are saved in each group as
        ``exclude_pattern``, and ``exchange-to-archive`` skips those files too.
        Default: nothing excluded.
    one_group : bool
        Treat the whole source as a single group.
    debug : bool
        Emit DEBUG log lines.

    Returns
    -------
    dict
        The group settings that were saved.

    Raises
    ------
    CreateGroupError
        If ``source`` is not a directory, ``existing`` is given but missing, the
        tree can't be grouped, or the JSON can't be written.

    Examples
    --------
    $ gdexws create-group --source /lustre/desc1/gdex/work/jdoe/d430681.cckw --output group_settings.json --include 'diag.*.nc' --include 'history.*.nc'
    $ gdexws create-group -s <source> -o group_settings.json --exclude 'tmp*' --exclude 'runA/scratch'
    """
    if not source.is_dir():
        raise CreateGroupError(f"Source is not a directory: {source}")
    if existing is not None and not existing.is_file():
        raise CreateGroupError(f"Existing group settings JSON not found: {existing}")

    if debug:
        service_log(COMMAND_NAME, "DEBUG", "Scanning source", source=str(source),
                    existing=str(existing) if existing else None,
                    include=include, exclude=exclude, one_group=one_group)
    try:
        groups = create(
            orignal_data_location=source,
            exist_group_json=existing,
            new_group_json=output,
            filename_wcard_list=include,
            one_group=one_group,
            logger=_ServiceLogger(),
            exclude_list=exclude,
        )
    except ValueError as exc:  # e.g. --one-group with several subdirectories
        raise CreateGroupError(str(exc)) from exc
    except OSError as exc:
        raise CreateGroupError(f"Could not write {output}: {exc}") from exc

    service_log(COMMAND_NAME, "INFO", f"Saved {len(groups)} group(s)", output=str(output))
    return groups


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare CLI arguments on the `create-group` subparser."""
    parser.add_argument("-s", "--source", required=True,
                        help="Root directory of the dataset's original data to scan")
    parser.add_argument("-o", "--output", required=True,
                        help="Group settings JSON file to write (overwritten if it exists)")
    parser.add_argument("-e", "--existing-json", default=None,
                        help="Group settings JSON from an earlier run; its groups keep their "
                             "group_index (default: none, indices start at 1)")
    parser.add_argument("--include", action="append", metavar="PATTERN",
                        help="Wildcard for files to put in each group, e.g. 'diag.*.nc' "
                             "(repeatable; quote it; default: every file)")
    parser.add_argument("--exclude", action="append", metavar="PATTERN",
                        help="Folder or file to leave out, e.g. 'tmp*', '*.log' or 'runA/scratch' "
                             "(a name matches at any depth, a path with '/' is relative to "
                             "--source; repeatable; quote it)")
    parser.add_argument("--one-group", action="store_true",
                        help="Treat the whole source as a single group")


def run(args: argparse.Namespace) -> None:
    """Execute the tool with parsed arguments."""
    try:
        existing = None
        if args.existing_json:
            existing = Path(args.existing_json)
        create_group(Path(args.source), Path(args.output), existing=existing,
                     include=args.include, exclude=args.exclude, one_group=args.one_group,
                     debug=args.debug)
    except CreateGroupError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc), source=args.source, output=args.output)
        sys.exit(1)
