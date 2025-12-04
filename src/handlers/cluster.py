"""
Cluster handler - Re-exports main handlers for convenience.

The actual cluster reconciliation logic is in main.py.
This module provides utility functions for cluster management.
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
from typing import Optional


def get_cluster_labels(name: str, component: Optional[str] = None) -> dict:
    """Generate standard labels for cluster resources."""
    labels = {
        "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
        "odoo.simstech.cloud/cluster": name
    }
    if component:
        labels["odoo.simstech.cloud/component"] = component
    return labels


async def get_cluster_status(namespace: str, name: str) -> dict:
    """Get aggregated status of all cluster components."""
    apps_api = client.AppsV1Api()
    custom_api = client.CustomObjectsApi()

    status = {
        "database": {"ready": False},
        "odoo": {"ready": False},
        "valkey": {"ready": False},
        "metabase": {"ready": False}
    }

    cluster_namespace = f"odoo-{name}"

    # Check database
    try:
        db = custom_api.get_namespaced_custom_object(
            group="postgresql.cnpg.io",
            version="v1",
            namespace=cluster_namespace,
            plural="clusters",
            name=f"{name}-db"
        )
        phase = db.get('status', {}).get('phase', '')
        status["database"]["ready"] = phase == 'Cluster in healthy state'
        status["database"]["phase"] = phase
    except ApiException:
        pass

    # Check Odoo deployment
    try:
        deployment = apps_api.read_namespaced_deployment(
            name=f"{name}-odoo",
            namespace=cluster_namespace
        )
        ready = deployment.status.ready_replicas or 0
        desired = deployment.spec.replicas or 1
        status["odoo"]["ready"] = ready >= desired
        status["odoo"]["replicas"] = f"{ready}/{desired}"
    except ApiException:
        pass

    # Check Valkey
    try:
        sts = apps_api.read_namespaced_stateful_set(
            name=f"{name}-valkey",
            namespace=cluster_namespace
        )
        ready = sts.status.ready_replicas or 0
        status["valkey"]["ready"] = ready >= 1
        status["valkey"]["exists"] = True
    except ApiException as e:
        if e.status == 404:
            status["valkey"]["exists"] = False

    # Check Metabase
    try:
        deployment = apps_api.read_namespaced_deployment(
            name=f"{name}-metabase",
            namespace=cluster_namespace
        )
        ready = deployment.status.ready_replicas or 0
        status["metabase"]["ready"] = ready >= 1
        status["metabase"]["exists"] = True
    except ApiException as e:
        if e.status == 404:
            status["metabase"]["exists"] = False

    return status

