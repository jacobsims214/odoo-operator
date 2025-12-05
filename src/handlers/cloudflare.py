"""
Cloudflare Tunnel handler - Creates a tunnel Deployment that routes to K8s Services.

Unlike Tailscale sidecars, Cloudflare Tunnel runs as a separate Deployment that
proxies to Kubernetes Services, enabling proper load balancing across replicas.

Supports TWO modes:
1. Token-based (Dashboard configured) - hostnames configured in CF Dashboard
2. Credentials-based (Config file) - hostnames configured in config.yaml

Architecture:
    Internet → Cloudflare Edge → cloudflared Deployment → K8s Service → Pod Replicas
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
from typing import Optional
import logging

logger = logging.getLogger(__name__)


async def create_cloudflare_tunnel(
    namespace: str,
    name: str,
    tunnel_secret_name: str,
    replicas: int = 1,
    owner_ref: Optional[dict] = None
) -> None:
    """Create Cloudflare Tunnel Deployment.

    This creates a simple deployment that runs cloudflared with the tunnel token.
    Hostnames and services are configured in the Cloudflare Dashboard.

    Args:
        namespace: Kubernetes namespace
        name: OdooCluster name
        tunnel_secret_name: Name of secret containing TUNNEL_TOKEN
        replicas: Number of tunnel replicas (1 or 2 for HA)
        owner_ref: Owner reference for garbage collection
    """
    apps_api = client.AppsV1Api()

    resource_name = f"{name}-cloudflare-tunnel"

    labels = {
        "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
        "odoo.simstech.cloud/cluster": name,
        "odoo.simstech.cloud/component": "cloudflare-tunnel"
    }

    # Create Deployment using token-based authentication
    # Hostnames are configured in Cloudflare Dashboard, not in config file
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": resource_name,
            "namespace": namespace,
            "labels": labels
        },
        "spec": {
            "replicas": replicas,
            "selector": {
                "matchLabels": {
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "cloudflare-tunnel"
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "cloudflare-tunnel"
                    }
                },
                "spec": {
                    "containers": [{
                        "name": "cloudflared",
                        "image": "cloudflare/cloudflared:latest",
                        "args": [
                            "tunnel",
                            "--no-autoupdate",
                            "run",
                            "--token",
                            "$(TUNNEL_TOKEN)"
                        ],
                        "env": [{
                            "name": "TUNNEL_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": tunnel_secret_name,
                                    "key": "TUNNEL_TOKEN"
                                }
                            }
                        }],
                        "resources": {
                            "requests": {
                                "cpu": "10m",
                                "memory": "64Mi"
                            },
                            "limits": {
                                "cpu": "100m",
                                "memory": "128Mi"
                            }
                        },
                        "livenessProbe": {
                            "httpGet": {
                                "path": "/ready",
                                "port": 2000
                            },
                            "initialDelaySeconds": 10,
                            "periodSeconds": 10,
                            "failureThreshold": 3
                        },
                        "readinessProbe": {
                            "httpGet": {
                                "path": "/ready",
                                "port": 2000
                            },
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5
                        }
                    }],
                }
            }
        }
    }

    if owner_ref:
        deployment["metadata"]["ownerReferences"] = [owner_ref]

    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=deployment)
        logger.info(f"Created Cloudflare tunnel Deployment: {resource_name}")
    except ApiException as e:
        if e.status == 409:
            apps_api.patch_namespaced_deployment(
                name=resource_name,
                namespace=namespace,
                body=deployment
            )
            logger.info(f"Updated Cloudflare tunnel Deployment: {resource_name}")
        else:
            raise


async def delete_cloudflare_tunnel(namespace: str, name: str) -> None:
    """Delete Cloudflare Tunnel Deployment and ConfigMap."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    resource_name = f"{name}-cloudflare-tunnel"

    # Delete Deployment
    try:
        apps_api.delete_namespaced_deployment(name=resource_name, namespace=namespace)
        logger.info(f"Deleted Cloudflare tunnel Deployment: {resource_name}")
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete ConfigMap
    try:
        core_api.delete_namespaced_config_map(
            name=f"{resource_name}-config",
            namespace=namespace
        )
        logger.info(f"Deleted Cloudflare tunnel ConfigMap: {resource_name}-config")
    except ApiException as e:
        if e.status != 404:
            raise


async def check_cloudflare_tunnel_ready(namespace: str, name: str) -> bool:
    """Check if Cloudflare Tunnel Deployment is ready."""
    apps_api = client.AppsV1Api()
    resource_name = f"{name}-cloudflare-tunnel"

    try:
        deployment = apps_api.read_namespaced_deployment(
            name=resource_name,
            namespace=namespace
        )
        ready = deployment.status.ready_replicas or 0
        desired = deployment.spec.replicas or 1
        return ready >= desired
    except ApiException as e:
        if e.status == 404:
            return False
        raise

