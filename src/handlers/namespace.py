"""
Namespace handler - Creates isolated namespace for each OdooCluster.
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf


async def create_namespace(namespace: str, cluster_name: str) -> None:
    """Create namespace for the OdooCluster."""
    api = client.CoreV1Api()

    ns = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            name=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": cluster_name,
            }
        )
    )

    try:
        api.create_namespace(body=ns)
    except ApiException as e:
        if e.status == 409:  # Already exists
            pass
        else:
            raise kopf.PermanentError(f"Failed to create namespace: {e}")


async def delete_namespace(namespace: str) -> None:
    """Delete the namespace (cascades to all resources)."""
    api = client.CoreV1Api()

    try:
        api.delete_namespace(name=namespace)
    except ApiException as e:
        if e.status == 404:  # Already deleted
            pass
        else:
            raise

