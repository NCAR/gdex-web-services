# gdexws

Service tools for data processing and management.

## Installation

### Release version (PyPI)

Install the latest published release:

```bash
pip install gdexws
```

Upgrade an existing install to the newest release:

```bash
pip install --upgrade gdexws
```

Runtime dependencies (`netCDF4`, `httpx`) are installed automatically.

### Developer version (GitHub)

Clone the repository and install `gdexws` in editable mode, so changes to the source take effect without reinstalling:

```bash
git clone https://github.com/NCAR/gdex-web-services.git
cd gdex-web-services
pip install -e ./gdexws
```

The version is derived from the repository's git tags, so install from a git clone (not a copied folder) to get a meaningful version number.

## CLI Commands

All tools run through the single `gdexws` command: `gdexws <tool> [options]`. Run `gdexws --help` to list tools and `gdexws <tool> --help` for a tool's options. `-d/--debug` is available on every tool.

Adding a tool: drop a module into `tools/` or `composers/` that defines `DESCRIPTION`, `add_arguments(parser)` and `run(args)`; it is discovered automatically as `gdexws <module-name-with-hyphens>`.

### add-global-meta

Add a global attribute to a netCDF file.

```bash
gdexws add-global-meta -f /path/to/file.nc -n "attribute_name" -v "attribute_value"
```

### create-exchange-dir

Create a directory directly under `/glade/campaign/collections/gdex/data/exchange` and grant an HPC user read/write access (`rwx`, plus a default ACL so new contents inherit it) via POSIX ACLs. Requires `setfacl`/`getfacl`.

```bash
gdexws create-exchange-dir <username> <dirname>
```

The directory name must be a single path component (`[A-Za-z0-9._-]`, starting with a letter or digit), the user must exist on the machine (and not be uid 0), and the command fails if the directory already exists. If the ACL cannot be applied and verified, the new directory is removed.

## Development

From the repository root, with the developer version installed:

```bash
pip install pytest
pytest tests/test_create_exchange_dir.py -v
```
