"""Add global attribute to a netCDF file."""
import os
import time
import argparse
import netCDF4 as nc
from gdexws.utils import service_log

def add_global_meta(file_path, key, value, debug=False):
    """Add a global attribute to a netCDF file."""
    service_log("add-global-meta", "DEBUG", "Adding global attribute")

    with nc.Dataset(file_path, mode="r+") as ds:
        ds.setncattr(key, value)

    for i in range(6):
        time.sleep(10)
        service_log("add-global-meta", "DEBUG" , "Loop iteration", iteration=i)

    service_log("add-global-meta", "DEBUG", "Process completed")

def main():
    """CLI entry point."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_name = os.path.basename(__file__)

    parser = argparse.ArgumentParser(description="Add a global attribute to a netCDF file")
    parser.add_argument("-f", "--file", help="Path to the netCDF file", required=True)
    parser.add_argument("-n", "--global-attr-name", help="Name of the global attribute to add", required=True)
    parser.add_argument("-v", "--global-attr-value", help="Value of the global attribute to add", required=True)
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    service_log("add-global-meta", "DEBUG", "Beginning process")
    service_log("add-global-meta", "DEBUG", "Script info", script_dir=script_dir, script_name=script_name)

    add_global_meta(args.file, args.global_attr_name, args.global_attr_value, args.debug)

if __name__ == "__main__":
    main()
