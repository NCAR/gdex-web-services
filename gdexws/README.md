# gdexws

Service tools for data processing and management.

## Installation

Install the package in development mode:

```bash
pip install -e .
```

Or install normally:

```bash
pip install .
```

## CLI Commands

### add-global-meta

Add a global attribute to a netCDF file.

```bash
add-global-meta -file /path/to/file.nc -global_attr_name "attribute_name" -global_attr_value "attribute_value"
```

### create-exchange-dir

Create a directory directly under `/glade/campaign/collections/gdex/data/exchange` and grant an HPC user read/write access (`rwx`, plus a default ACL so new contents inherit it) via POSIX ACLs. Requires `setfacl`/`getfacl`.

```bash
create-exchange-dir <username> <dirname>
```

The directory name must be a single path component (`[A-Za-z0-9._-]`, starting with a letter or digit), the user must exist on the machine (and not be uid 0), and the command fails if the directory already exists. If the ACL cannot be applied and verified, the new directory is removed.

## Development

To install with development dependencies:

```bash
pip install -e ".[dev]"
```
