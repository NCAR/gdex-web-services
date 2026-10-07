"""Add global attribute to a netCDF file."""
import os
import time
from gdexws.utils import service_log
from gdexws.utils.file_validation import relpath_validate

DESCRIPTION = "Add a global attribute to a netCDF file"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_NAME = os.path.basename(__file__)


def add_global_meta(file_path, key, value, debug=False):
    """Add a global attribute to a netCDF file."""
    import netCDF4  # deferred so `gdexws --help` stays fast

    full_path = relpath_validate([file_path])[0]  # Validate the file path
    if debug:
        service_log("add-global-meta", "DEBUG", f"Adding global attribute {key}={value} to {file_path}")

    with netCDF4.Dataset(full_path, mode="r+") as ds:
        ds.setncattr(key, value)

    for i in range(6):
        time.sleep(10)
        if debug:
            service_log("add-global-meta", "DEBUG" , "Loop iteration", iteration=i)

    if debug:
        service_log("add-global-meta", "DEBUG", "Process completed")

def add_arguments(parser):
    """Declare CLI arguments on the `add-global-meta` subparser."""
    parser.add_argument("-f", "--file", help="Path to the netCDF file", required=True)
    parser.add_argument("-n", "--global-attr-name", help="Name of the global attribute to add", required=True)
    parser.add_argument("-v", "--global-attr-value", help="Value of the global attribute to add", required=True)


def run(args):
    """Execute the tool with parsed arguments."""
    if args.debug:
        service_log("add-global-meta", "DEBUG", "Beginning process")
        service_log("add-global-meta", "DEBUG", "Script info", script_dir=SCRIPT_DIR, script_name=SCRIPT_NAME)

    add_global_meta(args.file, args.global_attr_name, args.global_attr_value, args.debug)
