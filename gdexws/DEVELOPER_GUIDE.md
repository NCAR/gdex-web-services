# gdexws Developer Guide

How to add a tool to the `gdexws` command line. For installing the developer version, see
[README.md](README.md#developer-version-github).

## How tools are discovered

`gdexws` has no list of tools to maintain. On every run, [cli.py](cli.py) scans the packages
in `TOOL_PACKAGES`:

```python
TOOL_PACKAGES = ["gdexws.tools", "gdexws.composers"]
```

and turns each qualifying module into a subcommand:

| File | Command |
|---|---|
| `tools/add_global_meta.py` | `gdexws add-global-meta` |
| `tools/create_exchange_dir.py` | `gdexws create-exchange-dir` |
| `composers/transform.py` | `gdexws transform` |

- **`tools/`**: single-purpose operations (on a file, a directory, etc.).
- **`composers/`**: tools that orchestrate other tools, such as `transform`, which runs the
  commands listed in a payload.

A module becomes a command when **all** of these hold:

1. It is a `.py` file directly inside `tools/` or `composers/`. Subfolders are skipped.
2. Its file name does not start with `_`. Use this for private helpers, e.g. `tools/_common.py`.
3. It defines both `add_arguments` and `run` at module level.

A module that doesn't meet rule 3 is skipped **silently**. If you misspell
`add_arguments` (for example `add_argument`), your tool simply won't appear in `gdexws --help`,
and there's no error.

The command name is the file name with `_` replaced by `-`. It must be unique across **both**
folders. `tools/foo.py` and `composers/foo.py` together make every `gdexws` command fail with
`RuntimeError: Duplicate tool name`.

## What a tool module needs

| Name | Required | Purpose |
|---|---|---|
| `add_arguments(parser)` | yes | Declare the tool's command-line options. |
| `run(args)` | yes | Do the work, using the parsed options. |
| `DESCRIPTION` | recommended | One-line summary shown in `gdexws --help` and `gdexws <tool> --help`. |
| Module docstring | fallback | Used as the description if `DESCRIPTION` is missing. |

### `DESCRIPTION`

```python
DESCRIPTION = "Add a global attribute to a netCDF file"
```

A short string of one line, with no trailing period. Without it (and without a module
docstring), the tool shows up in `gdexws --help` with no description.

### `add_arguments(parser)`

`parser` is an `argparse.ArgumentParser` created just for your tool. Add your options to it
and return nothing:

```python
def add_arguments(parser):
    """Declare CLI arguments on the `my-tool` subparser."""
    parser.add_argument("-f", "--file", required=True, help="Path to the input file")
    parser.add_argument("--count", type=int, default=1, help="How many times to repeat")
```

Names you must **not** use, because the CLI already defines them:

| Name | Used by |
|---|---|
| `-d` / `--debug` | Added to every tool automatically; read it as `args.debug`. |
| `-h` / `--help` | argparse's help option. |
| an option stored as `tool` (e.g. `--tool`) | The CLI stores the chosen command name in `args.tool`. |
| an option stored as `func` (e.g. `--func`) | The CLI stores your `run` function in `args.func`. |

argparse turns `--global-attr-name` into `args.global_attr_name`. Hyphens become underscores.

### `run(args)`

`args` is the `argparse.Namespace` holding your options plus `args.debug`. Keep `run` thin:
read `args`, call a plain function that does the work, and handle errors.

```python
def run(args):
    """Execute the tool with parsed arguments."""
    try:
        my_tool(args.file, args.count, debug=args.debug)
    except MyToolError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc))
        sys.exit(1)
```

**Exit code.** The CLI ignores what `run` returns. The process exit code is:

| `run` ... | Exit code |
|---|---|
| returns normally | `0` (success), even if you `return 1` |
| calls `sys.exit(n)` | `n` |
| raises an uncaught exception | `1`, with a Python traceback |

To report failure, **call `sys.exit(1)`**. `transform` and PBS jobs rely on the exit code to
detect failures.

## Template

Save as `tools/my_tool.py` to get `gdexws my-tool`:

```python
"""One-line summary of what my-tool does."""
import sys

from gdexws.utils import service_log

COMMAND_NAME = "my-tool"  # must match the file name with '_' -> '-'
DESCRIPTION = "One-line summary of what my-tool does"


class MyToolError(Exception):
    """Raised when my-tool cannot complete."""


def my_tool(file_path, count=1, debug=False):
    """Do the work. Plain function: easy to call from tests and other tools."""
    import netCDF4  # heavy imports go here, not at the top of the module (see below)

    if debug:
        service_log(COMMAND_NAME, "DEBUG", "Starting", file_path=file_path, count=count)
    ...
    service_log(COMMAND_NAME, "INFO", "Done", file_path=file_path)


def add_arguments(parser):
    """Declare CLI arguments on the `my-tool` subparser."""
    parser.add_argument("-f", "--file", required=True, help="Path to the input file")
    parser.add_argument("--count", type=int, default=1, help="How many times to repeat")


def run(args):
    """Execute the tool with parsed arguments."""
    try:
        my_tool(args.file, args.count, debug=args.debug)
    except MyToolError as exc:
        service_log(COMMAND_NAME, "ERROR", str(exc), file=args.file)
        sys.exit(1)
```

`COMMAND_NAME` is a convention for log messages only; the CLI doesn't read it. Keep it equal
to the command name so logs match what the user typed.

## Rules that keep the whole CLI working

The CLI **imports every tool module on every run**, including `gdexws --help`. That makes
module-level code in one tool affect every other tool.

- **Nothing at module level should be able to fail.** A missing package or a syntax error in
  your module breaks *every* `gdexws` command, not just yours. Before opening a PR, run
  `gdexws --help`.
- **No side effects at module level.** Don't read files, contact servers or create
  directories at import time. That code would run on every `gdexws` call.
- **Put heavy imports inside functions.** `netCDF4`, `numpy`, `xarray` and similar are slow to
  import. Put them in the function that uses them (see `add_global_meta.py`) so
  `gdexws --help` stays fast.

## Logging

Use `service_log` from `gdexws.utils` instead of `print`. It writes one JSON object per line to
stdout, which the PBS job collects into a `.jsonl` log:

```python
from gdexws.utils import service_log

service_log(COMMAND_NAME, "INFO", "Processed file", file_path=path, n_vars=12)
# {"command": "my-tool", "time_of_process": "...", "level": "INFO",
#  "process_message": "Processed file", "file_path": "...", "n_vars": 12}
```

- `level` must be one of `ERROR`, `WARNING`, `INFO` or `DEBUG`; anything else raises
  `ValueError`.
- Extra keyword arguments become extra keys. Pass only values `json.dumps` can handle (str,
  int, float, bool, None, lists, dicts); convert paths with `str()`.
- Log `DEBUG` messages only when `args.debug` is set.

## File paths under the exchange area

Tools that take a path from a user or a payload should restrict it to the exchange area with
`relpath_validate` from `gdexws.utils.file_validation`. It resolves the path relative to
`/glade/campaign/collections/gdex/data/exchange/` and refuses anything that escapes it (such as
`../../etc`).

## Making a tool usable from `transform` payloads

`gdexws transform -p payload.json` runs each entry in `Commands` against each entry in
`Files`, by running `gdexws <command> ...` as a separate process:

```json
{
  "Files": ["Web-services/test.nc"],
  "Commands": [
    {"command": "add_global_meta", "global-attr-name": "gdex_dsid",
     "global-attr-value": "d123456", "debug": true}
  ]
}
```

becomes:

```bash
gdexws add-global-meta -f Web-services/test.nc --global-attr-name gdex_dsid --global-attr-value d123456 --debug
```

For your tool to work in a payload, it must follow how `build_command` in
[utils/parse_payload.py](utils/parse_payload.py) builds that command:

| Rule | Why |
|---|---|
| Accept the file as **`-f` / `--file`** | Every file is passed as `-f <file>`. |
| Every other input is a **long option** (`--name`), not a positional argument | Each payload key `k` becomes `--k`. |
| Boolean options use `action="store_true"` | `true` adds the flag, `false` leaves it out. |
| Use `type=int`, `type=float`, etc. for non-string options | Values arrive as strings (`str(value)`). |
| Don't take list or dict values | They would arrive as their Python text form, e.g. `"['a', 'b']"`. |

Underscores in payload keys become hyphens (`global_attr_name` → `--global-attr-name`), and
`"command"` can use either `add_global_meta` or `add-global-meta`.

A tool that uses positional arguments (like `create-exchange-dir <username> <dirname>`) or no
`--file` can't be used from a payload. That's fine for tools that are only run by hand.

`transform` finds `gdexws` through `PATH`, so the venv must be activated (or its `bin/` on
`PATH`) where `transform` runs. The PBS script does this with `source .../bin/activate`.

## Dependencies

If your tool needs a package that isn't already a dependency, add it to `dependencies` in
[pyproject.toml](pyproject.toml):

```toml
dependencies = [
    "netCDF4>=1.5.0",
    "httpx>=0.24.0",
    "xarray>=2023.1",   # new
]
```

Without this, the tool works in your own venv but fails for everyone who installs the release
from PyPI. Dependencies are also imported every time `gdexws` runs (see
[Rules](#rules-that-keep-the-whole-cli-working)), so prefer the standard library when it's
enough.

External programs (like `setfacl`) can't be listed in `pyproject.toml`. Check for them with
`shutil.which()` and fail with a clear error message, and mention them in the README.

## New folders and files

The installed package includes **only** the packages listed in [pyproject.toml](pyproject.toml):

```toml
[tool.setuptools]
packages = ["gdexws", "gdexws.tools", "gdexws.utils", "gdexws.composers"]
```

- **A new tool in `tools/` or `composers/`:** nothing to change.
- **A new package folder** (e.g. `gdexws/readers/`): add `__init__.py` to it **and** add
  `"gdexws.readers"` to `packages`. Otherwise it works in your editable install but is missing
  from the PyPI release, and imports of it fail.
- **A new folder of tools** (a third place to scan): also add it to `TOOL_PACKAGES` in
  [cli.py](cli.py).
- **Non-Python files** (templates, JSON, etc.) aren't included in the release. Avoid them or
  ask a maintainer to set up `package-data`.

## Testing

Put tests in `tests/test_<tool>.py` at the repository root. Test the plain function directly,
and test the command line through `cli.main()`:

```python
from unittest.mock import patch

from gdexws import cli
from gdexws.tools import my_tool


def test_my_tool(tmp_path):
    ...  # call my_tool.my_tool(...) directly


def test_cli_runs_my_tool():
    with patch("sys.argv", ["gdexws", "my-tool", "-f", "x.nc"]), \
         patch.object(my_tool, "my_tool") as fake:
        cli.main()
    fake.assert_called_once_with("x.nc", 1, debug=False)
```

Run them:

```bash
pip install pytest
pytest tests/test_my_tool.py -v
```

The GitHub workflows only run the test files they name. Add your test file to the `pytest`
line in **both** `.github/workflows/test-gdexws.yaml` and
`.github/workflows/publish-gdexws.yaml`, otherwise CI never runs it.

## Checklist before opening a PR

### Critical

If any of these is missed, the tool, the whole `gdexws` command, or the PyPI release breaks.

- [ ] File is directly in `tools/` or `composers/`, name doesn't start with `_`, and the
      command name doesn't clash with an existing one.
- [ ] Module defines both `add_arguments(parser)` and `run(args)`, spelled exactly.
- [ ] No `-d`/`--debug`, `-h`/`--help`, `--tool` or `--func` options.
- [ ] Nothing at module level can fail or has side effects.
- [ ] Failures call `sys.exit(1)`, so `transform` and PBS jobs can detect them.
- [ ] New packages added to `dependencies`, and new package folders to `packages`, in
      `pyproject.toml`.
- [ ] `gdexws --help` still works and lists the tool, and `gdexws my-tool --help` shows its
      options.
- [ ] If it should work in payloads: accepts `-f/--file`, uses only long options, and was run
      once through `gdexws transform`.
- [ ] If it takes paths from users or payloads: they're checked with `relpath_validate`.

### Good to have

These keep the tool easy to use, maintain and debug.

- [ ] `DESCRIPTION` is set, so the tool has a summary in `gdexws --help`.
- [ ] Heavy imports (`netCDF4`, `numpy`, ...) are inside functions, so `gdexws --help` stays
      fast.
- [ ] Messages go through `service_log` (not `print`), errors are logged at `ERROR` before
      `sys.exit(1)`, and `DEBUG` messages only appear with `--debug`.
- [ ] `COMMAND_NAME` matches the command name, so log entries match what the user typed.
- [ ] `run` is thin, and the work is done in a plain function that tests can call directly.
- [ ] Tests added and listed in both workflow files.
- [ ] README's **CLI Commands** section has an entry for the tool.

Merging a PR that changes `gdexws/` into `main` publishes a new release to PyPI automatically.
