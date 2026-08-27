import sys
from datetime import datetime
from typing import Dict, Any


def log_format(
    command_name: str,
    level: str,
    process_message: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Create a structured log dictionary in the required format.

    Parameters
    ----------
    command_name : str
        Name of the command being executed
    level : str
        Log level (ERROR, WARNING, INFO, DEBUG)
    process_message : str
        Free format message for status tracking
    **kwargs
        Additional key=value pairs to include in the dict

    Returns
    -------
    dict
        Dictionary with the exact format:
        {
            "command": "...",
            "time_of_process": "...",
            "level": "...",
            "process_message": "...",
            ... (additional kwargs)
        }

    Raises
    ------
    ValueError
        If level is not ERROR, WARNING, INFO, or DEBUG
    """
    valid_levels = {"ERROR", "WARNING", "INFO", "DEBUG"}
    if level not in valid_levels:
        raise ValueError(f"level must be one of {valid_levels}, got '{level}'")

    log_dict = {
        "command": command_name,
        "time_of_process": datetime.now().isoformat(),
        "level": level,
        "process_message": process_message,
    }

    log_dict.update(kwargs)

    return log_dict



def service_log(command_name: str, level: str, process_message: str, **kwargs) -> None:
    """
    Log a message at the specified level and print it.

    Parameters
    ----------
    command_name : str
        Name of the command being executed
    level : str
        Log level (ERROR, WARNING, INFO, DEBUG)
    process_message : str
        Process message to log
    **kwargs
        Additional key=value pairs to include in the log
    """
    log_dict = log_format(
        command_name=command_name,
        level=level,
        process_message=process_message,
        **kwargs
    )
    print(log_dict, file=sys.stdout, flush=True)
