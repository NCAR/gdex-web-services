"""Create a directory under the GDEX exchange area and grant a user read/write access via POSIX ACLs."""
import os
import re
import pwd
import shutil
import subprocess
import sys

from gdexws.utils import service_log

COMMAND_NAME = "create-exchange-dir"
DESCRIPTION = "Create an exchange directory and grant an HPC user read/write access via ACL"

# The only location under which this tool will ever create a directory.
EXCHANGE_DIR = "/glade/campaign/collections/gdex/data/exchange"

# A single, plain path component: must start with an alphanumeric (no hidden
# dirs, no leading '-'), then only alphanumerics, '.', '_' or '-'.
_DIRNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIRNAME_MAX_LEN = 255

# POSIX portable username: lowercase letters, digits, '_' and '-', max 32 chars.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# rwx: 'x' is required on a directory for the user to traverse into it.
_ACL_PERMS = "rwx"
_DIR_MODE = 0o770


class ExchangeDirError(Exception):
    """Raised when the exchange directory cannot be safely created or shared."""


def validate_dirname(dirname):
    """Validate that ``dirname`` is a single, safe path component.

    Parameters
    ----------
    dirname : str
        Requested directory name.

    Returns
    -------
    str
        The validated directory name, unchanged.

    Raises
    ------
    ExchangeDirError
        If the name is empty, too long, contains a path separator, NUL, or
        characters outside ``[A-Za-z0-9._-]``, starts with a non-alphanumeric
        character, or is ``.``/``..``.
    """
    if not isinstance(dirname, str) or not dirname:
        raise ExchangeDirError("Directory name must be a non-empty string")
    if dirname in (".", ".."):
        raise ExchangeDirError(f"Invalid directory name: '{dirname}'")
    if len(dirname) > _DIRNAME_MAX_LEN:
        raise ExchangeDirError(f"Directory name exceeds {_DIRNAME_MAX_LEN} characters")
    if "/" in dirname or "\x00" in dirname or os.sep in dirname:
        raise ExchangeDirError("Directory name must be a single path component (no '/' allowed)")
    if not _DIRNAME_RE.fullmatch(dirname):
        raise ExchangeDirError(
            "Directory name must start with a letter or digit and contain only "
            "letters, digits, '.', '_' and '-'"
        )
    return dirname


def validate_username(username):
    """Validate the username format and verify the account exists on this machine.

    Parameters
    ----------
    username : str
        HPC username.

    Returns
    -------
    str
        The validated username, unchanged.

    Raises
    ------
    ExchangeDirError
        If the username is malformed, does not exist in the system user
        database, or resolves to root (uid 0).
    """
    if not isinstance(username, str) or not _USERNAME_RE.fullmatch(username):
        raise ExchangeDirError(f"Invalid username format: '{username}'")
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        raise ExchangeDirError(f"User '{username}' does not exist on this machine")
    if entry.pw_uid == 0:
        raise ExchangeDirError("Refusing to grant an ACL to uid 0")
    return username


def _resolve_exchange_dir(exchange_dir):
    """Return the real path of the exchange directory, verifying it is a directory.

    Parameters
    ----------
    exchange_dir : str
        Configured exchange directory.

    Returns
    -------
    str
        Fully resolved (symlink-free) absolute path.

    Raises
    ------
    ExchangeDirError
        If the path does not exist or is not a directory.
    """
    base = os.path.realpath(exchange_dir)
    if not os.path.isdir(base):
        raise ExchangeDirError(f"Exchange directory does not exist: {exchange_dir}")
    return base


def _run_acl_tool(args):
    """Run an ACL tool with a fixed argument list (no shell).

    Parameters
    ----------
    args : list of str
        Command and arguments; ``args[0]`` is looked up on ``PATH``.

    Returns
    -------
    str
        The command's stdout.

    Raises
    ------
    ExchangeDirError
        If the tool is missing or exits non-zero.
    """
    exe = shutil.which(args[0])
    if exe is None:
        raise ExchangeDirError(f"Required tool '{args[0]}' was not found on PATH")
    result = subprocess.run([exe] + args[1:], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ExchangeDirError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _grant_acl(path, username):
    """Grant ``username`` rwx on ``path`` (access and default ACL) and verify it took effect.

    Parameters
    ----------
    path : str
        Directory to modify.
    username : str
        User to add to the ACL.

    Raises
    ------
    ExchangeDirError
        If setfacl fails or the entries cannot be confirmed with getfacl.
    """
    # -P: never follow symlinks; '--' ends option parsing so the path cannot be read as a flag.
    _run_acl_tool([
        "setfacl", "-P",
        "-m", f"u:{username}:{_ACL_PERMS},d:u:{username}:{_ACL_PERMS}",
        "--", path,
    ])
    listing = _run_acl_tool(["getfacl", "--absolute-names", "--", path])
    # Drop trailing "#effective:..." comments before comparing entries.
    entries = {line.split("#")[0].strip() for line in listing.splitlines()}
    expected = {f"user:{username}:{_ACL_PERMS}", f"default:user:{username}:{_ACL_PERMS}"}
    if not expected <= entries:
        raise ExchangeDirError(f"ACL verification failed for '{username}' on {path}")


def create_exchange_dir(username, dirname, exchange_dir=EXCHANGE_DIR, debug=False):
    """Create ``dirname`` under the exchange directory and give ``username`` rwx via ACL.

    The directory must not already exist. If the ACL step fails, the newly
    created (still empty) directory is removed.

    Parameters
    ----------
    username : str
        HPC username that must exist on this machine.
    dirname : str
        Name of the single directory to create directly under ``exchange_dir``.
    exchange_dir : str
        Root under which directories may be created. Default: ``EXCHANGE_DIR``.
    debug : bool
        Emit DEBUG log lines.

    Returns
    -------
    str
        Absolute path of the created directory.

    Raises
    ------
    ExchangeDirError
        On any validation, creation, or ACL failure.
    """
    validate_dirname(dirname)
    validate_username(username)
    base = _resolve_exchange_dir(exchange_dir)
    target = os.path.join(base, dirname)

    # Defense in depth: the lexical join must still sit directly under base.
    if os.path.dirname(os.path.normpath(target)) != base:
        raise ExchangeDirError("Target path is not directly inside the exchange directory")
    if os.path.lexists(target):
        raise ExchangeDirError(f"'{dirname}' already exists in the exchange directory")

    if debug:
        service_log(COMMAND_NAME, "DEBUG", "Creating directory", path=target, username=username)

    # mkdir relative to an open fd on the base dir: atomic, fails with EEXIST if
    # anything (including a symlink) already occupies the name, and is not
    # affected by the base path being swapped after the checks above.
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(dirname, mode=_DIR_MODE, dir_fd=base_fd)
        except FileExistsError:
            raise ExchangeDirError(f"'{dirname}' already exists in the exchange directory")
        except OSError as exc:
            raise ExchangeDirError(f"Could not create directory: {exc}")

        try:
            _grant_acl(target, username)
        except ExchangeDirError:
            try:
                os.rmdir(dirname, dir_fd=base_fd)
            except OSError as exc:
                service_log(COMMAND_NAME, "WARNING", "Could not remove directory after ACL failure",
                            path=target, error=str(exc))
            raise
    finally:
        os.close(base_fd)

    service_log(COMMAND_NAME, "INFO", "Created directory and granted access",
                path=target, username=username, permissions=_ACL_PERMS)
    return target


def add_arguments(parser):
    """Declare CLI arguments on the `create-exchange-dir` subparser."""
    parser.add_argument("username", help="HPC username to grant read/write access")
    parser.add_argument("dirname", help="Name of the directory to create (a single path component)")


def run(args):
    """Execute the tool with parsed arguments."""
    try:
        create_exchange_dir(args.username, args.dirname, debug=args.debug)
    except ExchangeDirError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc), username=args.username, dirname=args.dirname)
        sys.exit(1)
