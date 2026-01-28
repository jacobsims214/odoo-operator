"""
Filestore backup handler - Creates a CronJob to backup Odoo filestore to S3.
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf
from typing import Optional


async def create_filestore_backup_job(
    namespace: str,
    name: str,
    schedule: str = "0 3 * * *",  # 3 AM daily (1 hour after DB backup)
    s3_bucket: str = None,
    s3_endpoint: str = None,
    s3_secret_name: str = "backup-s3-creds",
    retention_days: int = 30,
    owner_ref: Optional[dict] = None
) -> None:
    """Create a CronJob to backup Odoo filestore to S3."""
    if not s3_bucket:
        return  # No bucket configured, skip

    batch_api = client.BatchV1Api()

    resource_name = f"{name}-filestore-backup"
    destination_path = f"s3://{s3_bucket}/{name}/filestore"

    # Build the backup script
    backup_script = f"""#!/bin/bash
set -e

echo "Starting filestore backup at $(date)"

# Install AWS CLI
pip install awscli --quiet --break-system-packages --user 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"

# Configure AWS credentials from environment
export AWS_ACCESS_KEY_ID="$ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$SECRET_ACCESS_KEY"

# Create timestamped backup
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_PATH="{destination_path}/$TIMESTAMP"

echo "Syncing filestore to $BACKUP_PATH"
aws s3 sync /var/lib/odoo/filestore/odoo/ "$BACKUP_PATH/" --quiet

# Also maintain a "latest" symlink-like copy
echo "Updating latest backup"
aws s3 sync /var/lib/odoo/filestore/odoo/ "{destination_path}/latest/" --delete --quiet

# Clean up old backups (keep last {retention_days} days)
echo "Cleaning up backups older than {retention_days} days"
CUTOFF_DATE=$(date -d "-{retention_days} days" +%Y%m%d 2>/dev/null || date -v-{retention_days}d +%Y%m%d)
aws s3 ls "{destination_path}/" | while read -r line; do
    FOLDER=$(echo "$line" | awk '{{print $2}}' | tr -d '/')
    if [[ "$FOLDER" =~ ^[0-9]{{8}}_[0-9]{{6}}$ ]]; then
        FOLDER_DATE=$(echo "$FOLDER" | cut -d'_' -f1)
        if [[ "$FOLDER_DATE" < "$CUTOFF_DATE" ]]; then
            echo "Deleting old backup: $FOLDER"
            aws s3 rm "{destination_path}/$FOLDER/" --recursive --quiet
        fi
    fi
done

echo "Filestore backup completed at $(date)"
"""

    # Environment variables from secret
    env_vars = [
        {
            "name": "ACCESS_KEY_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": s3_secret_name,
                    "key": "ACCESS_KEY_ID"
                }
            }
        },
        {
            "name": "SECRET_ACCESS_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": s3_secret_name,
                    "key": "SECRET_ACCESS_KEY"
                }
            }
        }
    ]

    # Add endpoint URL if specified
    if s3_endpoint:
        env_vars.append({
            "name": "AWS_ENDPOINT_URL",
            "value": s3_endpoint
        })

    cronjob_metadata = {
        "name": resource_name,
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
            "odoo.simstech.cloud/cluster": name,
            "odoo.simstech.cloud/component": "filestore-backup"
        }
    }
    if owner_ref:
        cronjob_metadata["ownerReferences"] = [owner_ref]

    cronjob = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": cronjob_metadata,
        "spec": {
            "schedule": schedule,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 3,
            "jobTemplate": {
                "spec": {
                    "backoffLimit": 2,
                    "template": {
                        "metadata": {
                            "labels": {
                                "odoo.simstech.cloud/cluster": name,
                                "odoo.simstech.cloud/component": "filestore-backup"
                            }
                        },
                        "spec": {
                            "restartPolicy": "OnFailure",
                            "containers": [
                                {
                                    "name": "backup",
                                    "image": "python:3.12-slim",
                                    "command": ["/bin/bash", "-c", backup_script],
                                    "env": env_vars,
                                    "volumeMounts": [
                                        {
                                            "name": "filestore",
                                            "mountPath": "/var/lib/odoo/filestore",
                                            "readOnly": True
                                        }
                                    ],
                                    "resources": {
                                        "requests": {
                                            "cpu": "100m",
                                            "memory": "256Mi"
                                        },
                                        "limits": {
                                            "cpu": "500m",
                                            "memory": "512Mi"
                                        }
                                    }
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "filestore",
                                    "persistentVolumeClaim": {
                                        "claimName": f"{name}-odoo-filestore"
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }
    }

    try:
        batch_api.create_namespaced_cron_job(namespace=namespace, body=cronjob)
    except ApiException as e:
        if e.status == 409:  # Already exists
            batch_api.patch_namespaced_cron_job(
                name=resource_name,
                namespace=namespace,
                body=cronjob
            )
        else:
            raise kopf.PermanentError(f"Failed to create filestore backup job: {e}")


async def delete_filestore_backup_job(namespace: str, name: str) -> None:
    """Delete the filestore backup CronJob."""
    batch_api = client.BatchV1Api()

    try:
        batch_api.delete_namespaced_cron_job(
            name=f"{name}-filestore-backup",
            namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            raise


async def trigger_filestore_backup(namespace: str, name: str) -> str:
    """Manually trigger a filestore backup job."""
    batch_api = client.BatchV1Api()

    import time
    job_name = f"{name}-filestore-backup-manual-{int(time.time())}"

    # Get the CronJob to use its spec
    try:
        cronjob = batch_api.read_namespaced_cron_job(
            name=f"{name}-filestore-backup",
            namespace=namespace
        )
    except ApiException as e:
        raise kopf.PermanentError(f"CronJob not found: {e}")

    # Create a Job from the CronJob spec
    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=namespace,
            labels={
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "filestore-backup"
            }
        ),
        spec=cronjob.spec.job_template.spec
    )

    batch_api.create_namespaced_job(namespace=namespace, body=job)
    return job_name
