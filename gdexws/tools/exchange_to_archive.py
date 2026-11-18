"""Copy a dataset's files from an exchange directory to the GDEX archive with gdexcp.

What to copy comes from a group settings JSON made by ``gdexws create-group``: for each
group, the files under its ``scan_root_path`` that match its ``file_pattern``. Each
group's files are listed in a file list and copied with one ``gdexcp -i`` call (no
recursive folder copy) into the folder named by the group's ``web_path``, keeping
each file's subfolders below the group's folder (``runA/out/x.nc`` -> ``<web_path>/out/x.nc``).

Archive locations (``--storage``):

1. ``glade``  : POSIX, ``/gdex/data/<dataset-id>/<web_path>/``
2. ``object`` : Boreas object storage, ``<bucket>/<dataset-id>/<web_path>/``
3. TODO: cold storage or quasar directly
"""
import argparse
import glob
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Literal, Optional, Tuple
    

from gdexws.tools.create_exchange_dir import EXCHANGE_DIR
from gdexws.utils import service_log
from gdexws.utils.auto_group_creation import is_excluded
from gdexws.utils.file_validation import PathValidationError, relpath_validate

COMMAND_NAME = "exchange-to-archive"
DESCRIPTION = "Copy a dataset's files from an exchange directory to the GDEX archive with gdexcp"

# Where files may be read from and written to. To allow another location later,
# add its path here; nothing else in this module needs to change.
ALLOWED_SOURCE_ROOTS = (Path(EXCHANGE_DIR),)
GLADE_DIR = Path("/gdex/data/")
ALLOWED_TARGET_ROOTS = (GLADE_DIR,)

DEFAULT_BUCKET = "gdex-data"
GDEXCP_THREADS = "16"  # gdexcp -m: parallel copies (glade and object storage)
_DATASET_ID_RE = re.compile(r"^d\d{6}$")


class ExchangeToArchiveError(Exception):
    """Raised when the copy to the archive cannot be done safely or gdexcp fails."""


def _confirm_relative(path: str, what: str) -> str:
    """Return ``path`` normalized, if it is relative and stays inside its root.

    ``file_pattern`` and ``web_path`` come from a JSON file, so they are checked
    before being joined to the source or archive directory.

    Raises
    ------
    ExchangeToArchiveError
        If ``path`` is absolute or climbs out with ``..``.
    """
    norm = posixpath.normpath(path)
    if posixpath.isabs(norm) or norm == ".." or norm.startswith("../"):
        raise ExchangeToArchiveError(f"{what} must be a relative path that stays inside: {path!r}")
    return norm


def _confirm_source_root(group_key: str, group: dict) -> Path:
    """Return the group's resolved ``scan_root_path``, which must be inside an allowed root."""
    scan_root = group.get("scan_root_path")
    if not scan_root:
        raise ExchangeToArchiveError(f"Group '{group_key}' has no scan_root_path")
    for root in ALLOWED_SOURCE_ROOTS:
        try:
            resolved = relpath_validate([scan_root], base_dir=root)[0]
        except PathValidationError:
            continue
        if not resolved.is_dir():
            raise ExchangeToArchiveError(f"Source directory does not exist: {resolved}")
        return resolved
    raise ExchangeToArchiveError(
        f"Group '{group_key}' source '{scan_root}' is not inside an allowed directory")


def _find_match_files(source_root: Path, pattern: str) -> List[Path]:
    """Files under ``source_root`` matching ``pattern``, sorted.

    Symlinks, and anything that resolves outside ``source_root``, are skipped with a warning.
    """
    files = []
    for name in sorted(glob.glob(str(source_root / pattern))):
        path = Path(name)
        if path.is_symlink():
            service_log(COMMAND_NAME, "WARNING", "Skipping symlink", path=name)
            continue
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(source_root)
        except ValueError:
            service_log(COMMAND_NAME, "WARNING", "Skipping file outside the source", path=name)
            continue
        files.append(path)
    return files


def _gdexcp(command: List[str], dry_run: bool, debug: bool) -> Optional[str]:
    """Run one gdexcp command. Return None on success, or the error message."""
    if dry_run:
        service_log(COMMAND_NAME, "INFO", "Dry run: would run command " + " ".join(command))
        return None
    if debug:
        service_log(COMMAND_NAME, "DEBUG", "Running command " + " ".join(command))
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return str(exc)
    if result.returncode != 0:
        return result.stderr.strip() or f"exit code {result.returncode}"
    return None


def _get_group_files(group_key: str, group: dict, web_path: str) -> List[Tuple[Path, str]]:
    """Return ``(source_file, path under web_path)`` for every file the patterns match.

    The path under ``web_path`` is the file's path below ``<scan_root_path>/<web_path>``,
    so every subfolder is kept: with ``web_path`` ``runA``, ``runA/diag.1.nc`` gives
    ``diag.1.nc`` and ``runA/out/diag.1.nc`` gives ``out/diag.1.nc``.

    Files matching the group's ``exclude_pattern`` (saved by ``create-group --exclude``)
    are skipped.

    Raises
    ------
    ExchangeToArchiveError
        If a matched file is not inside the group's ``web_path`` folder.
    """
    source_root = _confirm_source_root(group_key, group)
    group_dir = source_root / web_path
    exclude_list = group.get("exclude_pattern") or []
    pairs: List[Tuple[Path, str]] = []
    seen = set()
    for pattern in group["file_pattern"]:
        pattern = _confirm_relative(pattern, "file_pattern")
        matched = _find_match_files(source_root, pattern)
        if not matched:
            service_log(COMMAND_NAME, "WARNING", "No files matched", group=group_key,
                        pattern=pattern, source=str(source_root))
            continue
        service_log(COMMAND_NAME, "INFO", f"Pattern '{pattern}': {len(matched)} file(s)", group=group_key)
        # A file matched by an earlier pattern is already in the list; add it only once.
        for file_path in matched:
            if file_path in seen:
                continue
            seen.add(file_path)
            if is_excluded(file_path.relative_to(source_root).as_posix(), exclude_list):
                service_log(COMMAND_NAME, "INFO", "Skipping excluded file", group=group_key,
                            path=str(file_path))
                continue
            try:
                dest = file_path.relative_to(group_dir).as_posix()
            except ValueError:
                raise ExchangeToArchiveError(
                    f"Group '{group_key}' pattern '{pattern}' matched {file_path}, which is not "
                    f"inside the group's web_path folder {group_dir}") from None
            pairs.append((file_path, dest))
    return pairs


def _get_group_target(group_key: str, group: dict) -> str:
    """Return the group's ``web_path``, checked to be a relative path.

    It is both the group's folder under ``scan_root_path`` and the folder (glade) or
    key prefix (object) its files are copied to under the dataset's archive location.
    """
    web_path = group.get("web_path")
    if not web_path:
        raise ExchangeToArchiveError(f"Group '{group_key}' has no web_path")
    web_path = _confirm_relative(web_path, "web_path")
    if web_path == ".":
        raise ExchangeToArchiveError(f"Group '{group_key}' web_path must name a folder: {web_path!r}")
    return web_path


def _check_unique_destinations(group_key: str, pairs: List[Tuple[Path, str]]) -> None:
    """Fail if two source files would be copied to the same place under ``web_path``.

    Subfolders are kept, so different source files normally get different paths;
    this is a last check before anything is written, since the archive must never be
    overwritten by a file from the same run.
    """
    seen = {}
    clashes = []
    for file_path, dest in pairs:
        if dest in seen:
            clashes.append(f"{seen[dest]} and {file_path} -> {dest}")
        else:
            seen[dest] = file_path
    if clashes:
        raise ExchangeToArchiveError(
            f"Group '{group_key}' has files that would overwrite each other: {'; '.join(clashes)}")


def _copy_group(exe, group_key, files, target_args, target, dataset_id, dry_run, debug):
    """Copy files (one inputfile list) into one target folder with one ``gdexcp -i <file list>`` call.

    Parameters
    ----------
    exe : str
        gdexcp executable.
    group_key : str
        Group name, for the log.
    files : list of pathlib.Path
        Source files, all copied directly into ``target``.
    target_args : list of str
        gdexcp target options, e.g. ``["-t", "/gdex/data/d123456/runA/", "-m", "16"]`` or
        ``["-tb", bucket, "-t", "d123456/runA/", "-m", "16"]``.
    target : str
        Target folder or ``bucket/prefix/``, used to report each copied file.
    dataset_id : str
        Used to name the temporary file list.
    dry_run, debug : bool
        See ``exchange_to_archive``.

    Returns
    -------
    tuple of (list of str, list of pathlib.Path)
        Copied destinations, and source files that failed.
    """
    # gdexcp runs as gdexdata, so the list must be readable by it (0644);
    # it is deleted again once gdexcp is done.
    with tempfile.NamedTemporaryFile("w", prefix=f"{dataset_id}-", suffix=".inputfile.txt",
                                     delete=False) as listing:
        for file_path in files:
            listing.write(f"{file_path}\n")
    try:
        os.chmod(listing.name, 0o644)
        if dry_run:
            service_log(COMMAND_NAME, "INFO", f"Dry run: {len(files)} file(s) in the -i list",
                        group=group_key, target=target)
        error = _gdexcp([exe, "-i", listing.name, *target_args], dry_run, debug)
    finally:
        os.unlink(listing.name)

    if error:
        service_log(COMMAND_NAME, "ERROR", "gdexcp failed", group=group_key, target=target,
                    files=len(files), error=error)
        # return copied, failed list
        return [], list(files)

    # return copied, failed list
    copied = []
    for file_path in files:
        copied.append(target + file_path.name)
    return copied, []


def exchange_to_archive(
    group_json: Path,
    dataset_id: str,
    storage: Literal["glade", "object"],
    bucket: str = DEFAULT_BUCKET,
    dry_run: bool = False,
    debug: bool = False,
) -> List[str]:
    """Copy each group in ``group_json`` to the archive for ``dataset_id``.

    - Groups without a ``file_pattern`` are skipped with a warning.
    - Each group's ``scan_root_path`` must be inside ``ALLOWED_SOURCE_ROOTS`` (the
      exchange directory); ``file_pattern`` must stay inside it.
    - Each group's files go under the folder named by its ``web_path``:
      ``/gdex/data/<dataset_id>/<web_path>/`` (glade) or
      ``<bucket>/<dataset_id>/<web_path>/`` (object), copied with one
      ``gdexcp -i <file list>`` call per target folder.
    - Subfolders below the group's folder (``<scan_root_path>/<web_path>``) are kept:
      with ``web_path`` ``runA``, ``runA/out/diag.1.nc`` goes to ``<web_path>/out/diag.1.nc``.
      A matched file outside that folder fails the group.
    - A group where two files would land on the same path fails (one would overwrite
      the other); the other groups are still copied.
    - Symlinks are skipped. A file matched by several patterns is copied once.
    - gdexcp creates missing archive folders itself, owned by gdexdata.
    - Every group is attempted even if an earlier one fails; failures are reported
      together at the end.

    Parameters
    ----------
    group_json : pathlib.Path
        Group settings JSON from ``gdexws create-group``.
    dataset_id : str
        Dataset ID such as ``d430681``.
    storage : {"glade", "object"}
        Archive to copy into.
    bucket : str
        Object storage bucket (``storage="object"`` only). Default: ``gdex-data``.
    dry_run : bool
        Log the gdexcp commands without running them.
    debug : bool
        Emit DEBUG log lines.

    Returns
    -------
    list of str
        archived file paths (glade) or ``bucket/key`` (object).

    Raises
    ------
    ExchangeToArchiveError
        If the dataset ID, storage, JSON or a group's paths are invalid, gdexcp is
        not on PATH, nothing matched, or any copy fails.

    Examples
    --------
    $ gdexws exchange-to-archive --group-json group_settings.json --dataset-id d430681 --storage glade
    $ gdexws exchange-to-archive -g group_settings.json -i d430681 --storage object --dry-run
    """
    if not _DATASET_ID_RE.match(dataset_id):
        raise ExchangeToArchiveError(f"Dataset ID must look like d123456: {dataset_id}")
    if storage not in ("glade", "object"):
        raise ExchangeToArchiveError(f"storage must be 'glade' or 'object', got '{storage}'")

    try:
        groups = json.loads(Path(group_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExchangeToArchiveError(f"Could not read group settings {group_json}: {exc}") from exc
    if not isinstance(groups, dict) or not groups:
        raise ExchangeToArchiveError(f"No groups in {group_json}")

    # check if the gdexcp executable is available on PATH
    exe = "gdexcp"
    if not dry_run:
        exe = shutil.which("gdexcp")
        if exe is None:
            raise ExchangeToArchiveError("Required tool 'gdexcp' was not found on PATH")

    archive_dir = GLADE_DIR / dataset_id
    copied: List[str] = []
    failed: List[Path] = []
    for group_key, group in groups.items():
        # Skip gdexcp for groups without a file pattern
        if not group.get("file_pattern"):
            service_log(COMMAND_NAME, "WARNING", "No file pattern, skipping group", group=group_key)
            continue
        service_log(COMMAND_NAME, "INFO", f"Processing group '{group_key}'")
        try:
            web_path = _get_group_target(group_key, group)
            pairs = _get_group_files(group_key, group, web_path)
            _check_unique_destinations(group_key, pairs)
        except ExchangeToArchiveError as exc:
            # A bad group must not stop the others; it still fails the run at the end.
            service_log(COMMAND_NAME, "ERROR", str(exc), group=group_key)
            failed.append(Path(group_key))
            continue

        # gdexcp -t takes one folder, so make one -i list per target folder
        # (<web_path>/ plus one per subfolder that holds files).
        dict_dests = {}
        for file_path, dest in pairs:
            # normpath drops the trailing "/" join leaves when dest has no folder
            rel_dest_folder = posixpath.normpath(posixpath.join(web_path, posixpath.dirname(dest)))
            dict_dests.setdefault(rel_dest_folder, []).append(file_path)

        for folder, files in dict_dests.items():
            if storage == "glade":
                target = f"{archive_dir / folder}/"
                target_args = ["-t", target, "-m", GDEXCP_THREADS]
            else:
                prefix = f"{dataset_id}/{folder}/"
                target = f"{bucket}/{prefix}"
                target_args = ["-tb", bucket, "-t", prefix, "-m", GDEXCP_THREADS]
            done, bad = _copy_group(exe, group_key, files, target_args, target, dataset_id,
                                    dry_run, debug)
            copied.extend(done)
            failed.extend(bad)

    # if any failed or empty copied the process will fail
    if failed:
        raise ExchangeToArchiveError(
            f"{len(failed)} item(s) failed to copy, check the log for the list")
    if not copied:
        # Usually a wrong JSON or pattern; fail so a PBS job notices.
        raise ExchangeToArchiveError(f"Nothing to copy from the groups in {group_json}")

    service_log(COMMAND_NAME, "INFO", f"Copy completed for {len(copied)} item(s) to {storage}",
                dataset_id=dataset_id)
    return copied


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare CLI arguments on the `exchange-to-archive` subparser."""
    parser.add_argument("-g", "--group-json", required=True,
                        help="Group settings JSON from 'gdexws create-group'")
    parser.add_argument("-i", "--dataset-id", required=True,
                        help="Dataset ID, e.g. d430681")
    parser.add_argument("--storage", required=True, choices=("glade", "object"),
                        help="Archive to copy into: glade (/gdex/data/<dataset-id>) or object (Boreas)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET,
                        help=f"Object storage bucket, --storage object only (default: {DEFAULT_BUCKET})")
    parser.add_argument("--dry-run", action="store_true", help="Log the gdexcp commands without running them")


def run(args: argparse.Namespace) -> None:
    """Execute the tool with parsed arguments."""
    try:
        exchange_to_archive(Path(args.group_json), args.dataset_id, args.storage, bucket=args.bucket,
                            dry_run=args.dry_run, debug=args.debug)
    except ExchangeToArchiveError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc), group_json=args.group_json,
                    dataset_id=args.dataset_id)
        sys.exit(1)
