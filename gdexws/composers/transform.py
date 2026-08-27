"""Transform script that executes commands on files based on a payload JSON."""
import sys
import argparse
from gdexws.utils import load_payload, build_command, execute_command, service_log


def transform(payload_path: str) -> None:
    """
    Main transform function that processes files 
    based on the commands listed in the payload configuration.

    payload format:
    {
        "Files": ["Web-services/test.nc"],
        "Commands": [
            {
            "command": "add_global_meta",
            "global-attr-name": "gdex_dsid",
            "global-attr-value": "dPayLoadTest",
            "debug": true
            }
        ]
    }


    Parameters
    ----------
    payload_path : str
        Path to the payload JSON file
    """
    try:
        payload = load_payload(payload_path)
    except FileNotFoundError:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message="Payload file not found",
            payload_path=payload_path
        )
        sys.exit(1)
    except Exception as e:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message="Failed to load payload",
            error=str(e)
        )
        sys.exit(1)

    files = payload.get("Files", [])
    commands = payload.get("Commands", [])

    if not files:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message="No files specified in payload"
        )
        sys.exit(1)

    if not commands:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message="No commands specified in payload"
        )
        sys.exit(1)

    for file_path in files:
        service_log(
            command_name="transform",
            level="INFO",
            process_message="Processing file",
            file_path=file_path
        )

        for command_spec in commands:
            # Extract command name and parameters
            if "command" not in command_spec:
                service_log(
                    command_name="transform",
                    level="ERROR",
                    process_message=f"Command spec missing 'command' key: {command_spec}"
                )
                sys.exit(1)

            command_name = command_spec["command"]

            # Convert underscore to hyphen for CLI command name
            cli_command = command_name.replace("_", "-")

            # Build the full command
            cmd = build_command(cli_command, command_spec, file_path)

            # Execute the command
            return_code = execute_command(cmd)

            if return_code != 0:
                service_log(
                    command_name="transform",
                    level="ERROR",
                    process_message=f"Command failed with return code {return_code}",
                    command=" ".join(cmd)
                )
                sys.exit(1)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Execute commands on files based on a payload JSON"
    )
    parser.add_argument("-p", "--payload", help="Path to the payload JSON file", required=True)
    args = parser.parse_args()

    transform(args.payload)


if __name__ == "__main__":
    main()
