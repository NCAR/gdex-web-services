"""Utilities for generating and uploading PBS job scripts to Boreas."""

from app.utils.boreas import _OBJECT_STORE_ENDPOINT, s3_client

_BUCKET = "gdex-data"
_PBS_PREFIX = "services_tmp/pbs"


def _generate_pbs_script(
    payload_url: str,
    request_id: str = None,
    job_name: str = "gdex-web-service-transform",
    account: str = "P43713000",
    ncpus: int = 1,
    mem_gb: int = 1,
    walltime: str = "00:05:00",
    queue: str = "gdex",
    env_activation: str = "source /glade/u/home/chiaweih/gdex-web-services/test-gdexws-env/bin/activate"
) -> str:
    """Generate a PBS job script for transform processing.

    Parameters
    ----------
    payload_url : str
        HTTPS URL to the payload JSON file on Boreas.
    job_name : str, optional
        PBS job name. Default: "gdex-web-service-transform"
    account : str, optional
        Charging account code. Default: "P43713000"
    ncpus : int, optional
        Number of CPUs. Default: 1
    mem_gb : int, optional
        Memory in GB. Default: 1
    walltime : str, optional
        Job walltime limit (HH:MM:SS). Default: "00:05:00"
    queue : str, optional
        PBS queue name. Default: "gdex"
    env_activation : str, optional
        Environment activation command. Default: source conda environment

    Returns
    -------
    str
        Complete PBS script content.
    """
    pbs_script = f"""### Job Name
#PBS -N {job_name}
### Charging account
#PBS -A {account}
### Request one chunk of resources with {ncpus} CPU and {mem_gb}GB of memory
#PBS -l select=1:ncpus={ncpus}:mem={mem_gb}GB
### Allow job to run up to {walltime}
#PBS -l walltime={walltime}
### Route the job to the {queue} queue
#PBS -q {queue}
### Join output and error streams into single file
#PBS -j oe

# Environment Management
module --force purge
{env_activation}

### Parse positional arguments
PAYLOAD="{payload_url}"

### SETUP .jsonl
jsonl_dir="/glade/campaign/collections/gdex/data/exchange/Web-services/"
output_jsonl="$jsonl_dir/$REQUEST_ID.gdexws.jsonl"

# Clear or create file
> "$output_jsonl"

# Shell start message
time_iso=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
echo '{{"command": "pbs", "time_of_process": "'$time_iso'", "level": "INFO", "process_message": "PBS job started"}}' >> "$output_jsonl"
echo '{{"command": "pbs", "time_of_process": "'$time_iso'", "level": "INFO", "process_message": "This is the jsonl with REQUEST_ID:$REQUEST_ID "}}' >> "$output_jsonl"
echo '{{"command": "pbs", "time_of_process": "'$time_iso'", "level": "INFO", "process_message": "jsonl location: $output_jsonl "}}' >> "$output_jsonl"

# Execute transform (the payload can be Boreas link)
transform -p "$PAYLOAD" >> "$output_jsonl"
EXIT_CODE=$?

# Shell end message
time_iso=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
if [ $EXIT_CODE -eq 0 ]; then
    echo '{{"command": "pbs", "time_of_process": "'$time_iso'", "level": "INFO", "process_message": "PBS job completed"}}' >> "$output_jsonl"
else
    echo '{{"command": "pbs", "time_of_process": "'$time_iso'", "level": "ERROR", "process_message": "PBS job failed", "exit_code": '$EXIT_CODE'}}' >> "$output_jsonl"
fi
"""
    return pbs_script


def _upload_pbs_script(
    script_content: str,
    filename: str = "transform.pbs",
    prefix: str = _PBS_PREFIX
) -> str:
    """Upload PBS script to Boreas object store.

    Parameters
    ----------
    script_content : str
        The PBS script content to upload.
    filename : str, optional
        Name of the PBS script in the object store. Default: "transform.pbs"
    prefix : str, optional
        S3 prefix/directory for PBS storage. Default: "services_tmp/pbs"

    Returns
    -------
    str
        Public HTTPS URL to the uploaded PBS script.
    """
    key = f"{prefix}/{filename}"
    s3_client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=script_content.encode("utf-8"),
        ContentType="text/plain",
        ACL="public-read",
    )

    return f"{_OBJECT_STORE_ENDPOINT}/{_BUCKET}/{key}"


def create_pbs_script(
    payload_url: str,
    request_id: str,
    prefix: str = _PBS_PREFIX,
    job_name: str = "gdex-web-service-transform",
    account: str = "P43713000",
    ncpus: int = 1,
    mem_gb: int = 1,
    walltime: str = "00:05:00",
    queue: str = "gdex",
    env_activation: str = "source /glade/u/home/chiaweih/gdex-web-services/test-gdexws-env/bin/activate"
) -> str:
    """Generate PBS script and upload to Boreas object store.

    Parameters
    ----------
    payload_url : str
        HTTPS URL to the payload JSON file on Boreas.
    request_id : str
        Unique request identifier (UUID) used in filename and JSONL output.
    prefix : str, optional
        S3 prefix/directory for PBS storage. Default: "services_tmp/pbs"
    job_name : str, optional
        PBS job name. Default: "gdex-web-service-transform"
    account : str, optional
        Charging account code. Default: "P43713000"
    ncpus : int, optional
        Number of CPUs. Default: 1
    mem_gb : int, optional
        Memory in GB. Default: 1
    walltime : str, optional
        Job walltime limit (HH:MM:SS). Default: "00:05:00"
    queue : str, optional
        PBS queue name. Default: "gdex"
    env_activation : str, optional
        Environment activation command. Default: source conda environment

    Returns
    -------
    str
        Public HTTPS URL to the uploaded PBS script.
    """
    filename = f"transform.{request_id}.pbs"
    script_content = _generate_pbs_script(
        payload_url=payload_url,
        request_id=request_id,
        job_name=job_name,
        account=account,
        ncpus=ncpus,
        mem_gb=mem_gb,
        walltime=walltime,
        queue=queue,
        env_activation=env_activation
    )

    return _upload_pbs_script(script_content, filename=filename, prefix=prefix)
