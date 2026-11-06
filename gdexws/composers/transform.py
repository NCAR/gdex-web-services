"""Transform script that executes commands on files based on a payload JSON."""
import sys
from gdexws.utils import load_payload, build_command, execute_command, service_log

DESCRIPTION = "Execute commands on files based on a payload JSON"


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
    # Load the payload JSON
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

    # Parse files and commands from the payload
    files = payload.get("Files", [])
    commands = payload.get("Commands", [])

    if not commands:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message="No commands specified in payload"
        )
        sys.exit(1)

    # Process commands that do not have any files specified
    if not files:
        for command_spec in commands:
            _run_command(command_spec, None)

    # Start processing each file and loop through the commands
    for file_path in files:
        service_log(
            command_name="transform",
            level="INFO",
            process_message="Processing file",
            file_path=file_path
        )

        for command_spec in commands:
            _run_command(command_spec, file_path)


def _run_command(command_spec: dict, file_path: str | None) -> None:
    """
    Build and execute a single command from its spec.

    Exits the process with status 1 if the spec is missing the
    'command' key or if the command returns a non-zero exit code.

    Parameters
    ----------
    command_spec : dict
        Command specification from the payload's "Commands" list
    file_path : str or None
        File to run the command on, or None for commands that take no file
    """
    if "command" not in command_spec:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message=f"Command spec missing 'command' key: {command_spec}"
        )
        sys.exit(1)

    # Convert underscore to hyphen for CLI command name
    cli_command = command_spec["command"].replace("_", "-")

    cmd = build_command(cli_command, command_spec, file_path)
    return_code = execute_command(cmd)

    if return_code != 0:
        service_log(
            command_name="transform",
            level="ERROR",
            process_message=f"Command failed with return code {return_code}",
            exec_command=" ".join(cmd)
        )
        sys.exit(1)


def add_arguments(parser):
    """Declare CLI arguments on the `transform` subparser."""
    parser.add_argument("-p", "--payload", help="Path or URL of the payload JSON file", required=True)


def run(args):
    """Execute the composer with parsed arguments."""
    service_log(
        command_name="transform",
        level="INFO",
        process_message="composer.transform started",
        payload_path=args.payload
    )
    transform(args.payload)
