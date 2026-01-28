"""
Database handler - Creates CloudNative-PG PostgreSQL clusters.
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf
from typing import Optional


async def create_database(
    namespace: str,
    name: str,
    storage: str = "20Gi",
    storage_class_name: Optional[str] = None,
    instances: int = 1,
    resources: dict = None,
    backup: Optional[dict] = None,
    owner_ref: Optional[dict] = None
) -> None:
    """Create a CloudNative-PG PostgreSQL cluster."""
    api = client.CustomObjectsApi()

    # Build resource requirements
    res = resources or {}
    requests = res.get('requests', {})
    limits = res.get('limits', {})

    # Build storage spec
    storage_spec = {"size": storage}
    if storage_class_name:
        storage_spec["storageClass"] = storage_class_name

    # Base cluster spec
    cluster_spec = {
        "instances": instances,
        "storage": storage_spec,
        "resources": {
            "requests": {
                "cpu": requests.get('cpu', '250m'),
                "memory": requests.get('memory', '512Mi')
            },
            "limits": {
                "cpu": limits.get('cpu', '1'),
                "memory": limits.get('memory', '2Gi')
            }
        },
        "postgresql": {
            "parameters": {
                "max_connections": "200",
                "shared_buffers": "256MB"
            }
        },
        "bootstrap": {
            "initdb": {
                "database": "odoo",
                "owner": "odoo"
            }
        }
    }

    # Add backup configuration if enabled
    if backup:
        s3_config = backup.get('s3', {})
        if s3_config.get('bucket'):
            barman_config = {
                "destinationPath": f"s3://{s3_config['bucket']}/{name}",
                "s3Credentials": {
                    "accessKeyId": {
                        "name": s3_config.get('secretName', 'backup-s3-creds'),
                        "key": "ACCESS_KEY_ID"
                    },
                    "secretAccessKey": {
                        "name": s3_config.get('secretName', 'backup-s3-creds'),
                        "key": "SECRET_ACCESS_KEY"
                    }
                }
            }
            # Only add endpointURL if specified (not needed for standard AWS S3)
            if s3_config.get('endpoint'):
                barman_config["endpointURL"] = s3_config['endpoint']

            cluster_spec["backup"] = {
                "barmanObjectStore": barman_config,
                "retentionPolicy": backup.get('retentionPolicy', '30d')
            }

            # Note: ScheduledBackup is a separate CR, we'll create it below

    cluster_metadata = {
        "name": f"{name}-db",
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
            "odoo.simstech.cloud/cluster": name
        }
    }
    if owner_ref:
        cluster_metadata["ownerReferences"] = [owner_ref]

    cluster = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": cluster_metadata,
        "spec": cluster_spec
    }

    try:
        api.create_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="clusters",
            body=cluster
        )
    except ApiException as e:
        if e.status == 409:  # Already exists - update it
            api.patch_namespaced_custom_object(
                group="postgresql.cnpg.io",
                version="v1",
                namespace=namespace,
                plural="clusters",
                name=f"{name}-db",
                body=cluster
            )
        else:
            raise kopf.PermanentError(f"Failed to create database: {e}")

    # Create scheduled backup if backup is enabled
    if backup and backup.get('s3', {}).get('bucket'):
        await create_scheduled_backup(
            namespace=namespace,
            name=name,
            schedule=backup.get('schedule', '0 2 * * *'),
            owner_ref=owner_ref
        )


async def create_scheduled_backup(
    namespace: str,
    name: str,
    schedule: str,
    owner_ref: Optional[dict] = None
) -> None:
    """Create a ScheduledBackup CR for automatic backups."""
    api = client.CustomObjectsApi()

    backup_metadata = {
        "name": f"{name}-db-backup",
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
            "odoo.simstech.cloud/cluster": name
        }
    }
    if owner_ref:
        backup_metadata["ownerReferences"] = [owner_ref]

    scheduled_backup = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "ScheduledBackup",
        "metadata": backup_metadata,
        "spec": {
            "schedule": schedule,
            "backupOwnerReference": "self",
            "cluster": {
                "name": f"{name}-db"
            }
        }
    }

    try:
        api.create_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="scheduledbackups",
            body=scheduled_backup
        )
    except ApiException as e:
        if e.status == 409:  # Already exists
            api.patch_namespaced_custom_object(
                group="postgresql.cnpg.io",
                version="v1",
                namespace=namespace,
                plural="scheduledbackups",
                name=f"{name}-db-backup",
                body=scheduled_backup
            )
        else:
            raise kopf.PermanentError(f"Failed to create scheduled backup: {e}")


async def delete_database(namespace: str, name: str) -> None:
    """Delete the PostgreSQL cluster."""
    api = client.CustomObjectsApi()

    # Delete scheduled backup first
    try:
        api.delete_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="scheduledbackups",
            name=f"{name}-db-backup"
        )
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete cluster
    try:
        api.delete_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="clusters",
            name=f"{name}-db"
        )
    except ApiException as e:
        if e.status != 404:
            raise


async def check_database_ready(namespace: str, name: str) -> bool:
    """Check if the PostgreSQL cluster is ready."""
    api = client.CustomObjectsApi()

    try:
        cluster = api.get_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="clusters",
            name=f"{name}-db"
        )

        status = cluster.get('status', {})
        phase = status.get('phase', '')

        return phase == 'Cluster in healthy state'

    except ApiException as e:
        if e.status == 404:
            return False
        raise

