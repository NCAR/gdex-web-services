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

## HPC setup and continuous update

The HPC environment that PBS jobs use lives at `/glade/u/home/gdexdata/gdexwsenv`. The packages it needs are listed in [requirements_hpc.txt](requirements_hpc.txt).

- **Python:** 3.11 from `ncarenv/25.10` (`/glade/u/apps/casper/25.10/opt/view/bin/python3`).
- **User:** run every command below as `gdexdata` (`sudo -iu gdexdata`).
- **System tools:** `setfacl`/`getfacl` (OS package `acl`) must be on `PATH`.

### 1. Create the virtual environment

```bash
python3 -m venv /glade/u/home/gdexdata/gdexwsenv
source /glade/u/home/gdexdata/gdexwsenv/bin/activate        # bash/zsh
source /glade/u/home/gdexdata/gdexwsenv/bin/activate.csh    # csh/tcsh
```

### 2. Install the packages

```bash
/glade/u/home/gdexdata/gdexwsenv/bin/python -m pip install -r https://raw.githubusercontent.com/NCAR/gdex-web-services/main/gdexws/requirements_hpc.txt
```

### 3. Install the setuid wrappers

These let specialists run tools such as `gdexcp` as `gdexdata`, so copied files get the right owner and permissions.

```bash
pywrapper-install -c                              # compile pywrapper (owned by gdexdata, setuid, mode 4750)
pywrapper-install --pgstart --username gdexdata   # create pgstart_gdexdata
pywrapper-install -l all                          # create the dsarch/gdexcp symlinks through pgstart_gdexdata
```

### Daily automatic upgrade (gdexws only)

Add this to the `gdexdata` crontab to upgrade `gdexws` every day at 03:00:

```cron
0 3 * * * (date; /glade/u/home/gdexdata/gdexwsenv/bin/python -m pip install --upgrade --disable-pip-version-check "gdexws>=0.1.1") >> /glade/u/home/gdexdata/gdexwsenv-upgrade.log 2>&1
```

Check `/glade/u/home/gdexdata/gdexwsenv-upgrade.log` for the result of each run.

### Manual upgrade (all packages)

When the other packages (`rda-python-*`) need upgrading, run as `gdexdata` (`sudo -iu gdexdata`):

```bash
# 1. Upgrade every package to the versions required in requirements_hpc.txt
/glade/u/home/gdexdata/gdexwsenv/bin/python -m pip install --upgrade -r https://raw.githubusercontent.com/NCAR/gdex-web-services/main/gdexws/requirements_hpc.txt

# 2. Rebuild the setuid wrappers (no sudo needed again)
/glade/u/home/gdexdata/gdexwsenv/bin/pywrapper-install -u       # recompile pywrapper and reinstall all pgstart_USER binaries
/glade/u/home/gdexdata/gdexwsenv/bin/pywrapper-install -l all   # link any new setuid_* program the upgrade added
```

## CLI Commands

All tools run through the single `gdexws` command: `gdexws <tool> [options]`. Run `gdexws --help` to list tools and `gdexws <tool> --help` for a tool's options. `-d/--debug` is available on every tool.

Adding a tool: drop a module into `tools/` or `composers/` that defines `DESCRIPTION`, `add_arguments(parser)` and `run(args)`; it is discovered automatically as `gdexws <module-name-with-hyphens>`.

### add-global-meta

Add a global attribute to a netCDF file.

```bash
gdexws add-global-meta -f /path/to/file.nc -n "attribute_name" -v "attribute_value"
```

### create-exchange-dir

Create a directory directly under `/glade/campaign/collections/gdex/data/exchange` and grant an HPC user read/write access (`rwx`, plus a default ACL so new contents inherit it) via POSIX ACLs. The directory's group (`decs`, which includes `gdexdata`) also gets `rwx` the same way, so `copy-files`/`gdexcp` can write into it; the directory is setgid (mode `2775`) so new contents stay in that group, and others can read (`r-x`). Requires `setfacl`/`getfacl`.

```bash
gdexws create-exchange-dir --username <username> --dirname <dirname>
```

The directory name must be a single path component (`[A-Za-z0-9._-]`, starting with a letter or digit), the user must exist on the machine (and not be uid 0), and the command fails if the directory already exists. If the ACL cannot be applied and verified, the new directory is removed.

## Development

From the repository root, with the developer version installed:

```bash
pip install pytest
pytest tests/gdexws_tests -v
```
