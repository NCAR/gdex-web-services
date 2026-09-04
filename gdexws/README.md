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

## Development

To install with development dependencies:

```bash
pip install -e ".[dev]"
```
