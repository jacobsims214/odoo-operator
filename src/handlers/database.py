"""
Database handler - Creates CloudNative-PG PostgreSQL clusters.

Supports:
- Fresh database creation (bootstrap.initdb)
- S3 backup configuration
- S3 restore/recovery (bootstrap.recovery)
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
    restore: Optional[dict] = None,
    owner_ref: Optional[dict] = None,
    postgres_version: str = "17",
    enable_pgvector: bool = False,
) -> None:
    """
    Create a CloudNative-PG PostgreSQL cluster.

    Args:
        restore: Optional restore config with:
            - enabled: bool - Whether to restore from S3
            - serverName: str - Original cluster name in backup (e.g., 'avware-odoo-db')
            - s3Path: str - S3 path to restore from (e.g., 's3://bucket/path')
    """
    api = client.CustomObjectsApi()

    # Build resource requirements
    res = resources or {}
    requests = res.get("requests", {})
    limits = res.get("limits", {})

    # Build storage spec
    storage_spec = {"size": storage}
    if storage_class_name:
        storage_spec["storageClass"] = storage_class_name

    # Determine PostgreSQL image
    # CloudNativePG base images include barman-cloud tools needed for S3 backup/restore
    # The base image also includes pgvector extension
    # See: https://github.com/cloudnative-pg/postgres-containers
    image_name = f"ghcr.io/cloudnative-pg/postgresql:{postgres_version}"

    # Base cluster spec
    cluster_spec = {
        "instances": instances,
        "imageName": image_name,
        "storage": storage_spec,
        "resources": {
            "requests": {"cpu": requests.get("cpu", "250m"), "memory": requests.get("memory", "512Mi")},
            "limits": {"cpu": limits.get("cpu", "1"), "memory": limits.get("memory", "2Gi")},
        },
        "postgresql": {"parameters": {"max_connections": "200", "shared_buffers": "256MB"}},
    }

    # Determine bootstrap method: restore from S3 or fresh initdb
    restore_enabled = restore and restore.get("enabled", False)

    if restore_enabled:
        # RESTORE MODE: Bootstrap from S3 backup
        s3_config = backup.get("s3", {}) if backup else {}
        secret_name = s3_config.get("secretName", "backup-s3-creds")

        # The serverName MUST match the original cluster's name in the backup
        # This is critical - CloudNativePG uses this to find the correct WAL files
        original_server_name = restore.get("serverName", f"{name}-db")

        # The restore path - where the backup was stored
        restore_path = restore.get("s3Path")
        if not restore_path and s3_config.get("bucket"):
            # Default: use the backup path (bucket/name)
            restore_path = f"s3://{s3_config['bucket']}/{name}"

        if not restore_path:
            raise kopf.PermanentError("Restore enabled but no s3Path or backup.s3.bucket specified")

        # Build external cluster reference for recovery source
        external_cluster_config = {
            "serverName": original_server_name,
            "destinationPath": restore_path,
            "s3Credentials": {
                "accessKeyId": {"name": secret_name, "key": "ACCESS_KEY_ID"},
                "secretAccessKey": {"name": secret_name, "key": "SECRET_ACCESS_KEY"},
            },
        }

        # Add endpoint if specified
        if s3_config.get("endpoint"):
            external_cluster_config["endpointURL"] = s3_config["endpoint"]

        cluster_spec["bootstrap"] = {"recovery": {"source": "restore-source"}}

        cluster_spec["externalClusters"] = [{"name": "restore-source", "barmanObjectStore": external_cluster_config}]

        kopf.info(
            {},
            reason="RestoreMode",
            message=f"Creating cluster {name}-db with S3 recovery from {restore_path} (serverName={original_server_name})",
        )
    else:
        # FRESH MODE: Bootstrap with initdb
        cluster_spec["bootstrap"] = {"initdb": {"database": "odoo", "owner": "odoo"}}

    # Add backup configuration for ongoing backups (separate from restore)
    if backup:
        s3_config = backup.get("s3", {})
        if s3_config.get("bucket"):
            # For NEW backups after restore, use a different path to avoid
            # "Expected empty archive" error
            backup_suffix = restore.get("backupSuffix", "") if restore_enabled else ""
            if backup_suffix:
                backup_path = f"s3://{s3_config['bucket']}/{name}-{backup_suffix}"
            else:
                backup_path = f"s3://{s3_config['bucket']}/{name}"

            barman_config = {
                "destinationPath": backup_path,
                "s3Credentials": {
                    "accessKeyId": {"name": s3_config.get("secretName", "backup-s3-creds"), "key": "ACCESS_KEY_ID"},
                    "secretAccessKey": {
                        "name": s3_config.get("secretName", "backup-s3-creds"),
                        "key": "SECRET_ACCESS_KEY",
                    },
                },
            }
            # Only add endpointURL if specified (not needed for standard AWS S3)
            if s3_config.get("endpoint"):
                barman_config["endpointURL"] = s3_config["endpoint"]

            cluster_spec["backup"] = {
                "barmanObjectStore": barman_config,
                "retentionPolicy": backup.get("retentionPolicy", "30d"),
            }

    cluster_metadata = {
        "name": f"{name}-db",
        "namespace": namespace,
        "labels": {"app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator", "odoo.simstech.cloud/cluster": name},
    }
    if owner_ref:
        cluster_metadata["ownerReferences"] = [owner_ref]

    cluster = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": cluster_metadata,
        "spec": cluster_spec,
    }

    try:
        api.create_namespaced_custom_object(
            group="postgresql.cnpg.io", version="v1", namespace=namespace, plural="clusters", body=cluster
        )
    except ApiException as e:
        if e.status == 409:  # Already exists
            # Cluster already exists - check if it's healthy and skip
            # CloudNativePG doesn't allow changing bootstrap method, so we
            # only patch non-bootstrap fields if needed
            try:
                existing = api.get_namespaced_custom_object(
                    group="postgresql.cnpg.io", version="v1", namespace=namespace, plural="clusters", name=f"{name}-db"
                )
                existing_phase = existing.get("status", {}).get("phase", "")

                # If cluster is healthy, just log and continue
                if existing_phase == "Cluster in healthy state":
                    kopf.info(
                        cluster,
                        reason="ClusterExists",
                        message=f"PostgreSQL cluster {name}-db already exists and is healthy, skipping update",
                    )
                else:
                    # Cluster exists but not healthy - only patch safe fields (no bootstrap)
                    # Create a patch without the bootstrap section
                    safe_patch = {
                        "spec": {
                            "instances": cluster_spec["instances"],
                            "imageName": cluster_spec["imageName"],
                            "storage": cluster_spec["storage"],
                            "resources": cluster_spec["resources"],
                            "postgresql": cluster_spec["postgresql"],
                        }
                    }
                    if "backup" in cluster_spec:
                        safe_patch["spec"]["backup"] = cluster_spec["backup"]

                    api.patch_namespaced_custom_object(
                        group="postgresql.cnpg.io",
                        version="v1",
                        namespace=namespace,
                        plural="clusters",
                        name=f"{name}-db",
                        body=safe_patch,
                    )
            except ApiException:
                # If we can't get the cluster, just skip
                kopf.info(cluster, reason="ClusterExists", message=f"PostgreSQL cluster {name}-db exists, skipping")
        else:
            raise kopf.PermanentError(f"Failed to create database: {e}")

    # Create scheduled backup if backup is enabled
    if backup and backup.get("s3", {}).get("bucket"):
        await create_scheduled_backup(
            namespace=namespace, name=name, schedule=backup.get("schedule", "0 2 * * *"), owner_ref=owner_ref
        )


async def create_scheduled_backup(namespace: str, name: str, schedule: str, owner_ref: Optional[dict] = None) -> None:
    """Create a ScheduledBackup CR for automatic backups."""
    api = client.CustomObjectsApi()

    backup_metadata = {
        "name": f"{name}-db-backup",
        "namespace": namespace,
        "labels": {"app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator", "odoo.simstech.cloud/cluster": name},
    }
    if owner_ref:
        backup_metadata["ownerReferences"] = [owner_ref]

    scheduled_backup = {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "ScheduledBackup",
        "metadata": backup_metadata,
        "spec": {"schedule": schedule, "backupOwnerReference": "self", "cluster": {"name": f"{name}-db"}},
    }

    try:
        api.create_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=namespace,
            plural="scheduledbackups",
            body=scheduled_backup,
        )
    except ApiException as e:
        if e.status == 409:  # Already exists
            api.patch_namespaced_custom_object(
                group="postgresql.cnpg.io",
                version="v1",
                namespace=namespace,
                plural="scheduledbackups",
                name=f"{name}-db-backup",
                body=scheduled_backup,
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
            name=f"{name}-db-backup",
        )
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete cluster
    try:
        api.delete_namespaced_custom_object(
            group="postgresql.cnpg.io", version="v1", namespace=namespace, plural="clusters", name=f"{name}-db"
        )
    except ApiException as e:
        if e.status != 404:
            raise


async def check_database_ready(namespace: str, name: str) -> bool:
    """Check if the PostgreSQL cluster is ready."""
    api = client.CustomObjectsApi()

    try:
        cluster = api.get_namespaced_custom_object(
            group="postgresql.cnpg.io", version="v1", namespace=namespace, plural="clusters", name=f"{name}-db"
        )

        status = cluster.get("status", {})
        phase = status.get("phase", "")

        return phase == "Cluster in healthy state"

    except ApiException as e:
        if e.status == 404:
            return False
        raise
