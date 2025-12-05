"""
Cloudflare Tunnel handler - Creates a tunnel Deployment that routes to K8s Services.

Unlike Tailscale sidecars, Cloudflare Tunnel runs as a separate Deployment that
proxies to Kubernetes Services, enabling proper load balancing across replicas.

Architecture:
    Internet → Cloudflare Edge → cloudflared Deployment → K8s Service → Pod Replicas
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def build_tunnel_config(
    tunnel_id: str,
    odoo_service: str,
    odoo_hostname: Optional[str],
    metabase_service: Optional[str],
    metabase_hostname: Optional[str]
) -> str:
    """Build the cloudflared config.yaml content."""
    ingress_rules = []

    if odoo_hostname:
        ingress_rules.append({
            "hostname": odoo_hostname,
            "service": f"http://{odoo_service}:8069"
        })

    if metabase_hostname and metabase_service:
        ingress_rules.append({
            "hostname": metabase_hostname,
            "service": f"http://{metabase_service}:3000"
        })

    # Catch-all rule (required by cloudflared)
    ingress_rules.append({
        "service": "http_status:404"
    })

    # Convert to YAML format
    lines = [
        f"tunnel: {tunnel_id}",
        "credentials-file: /etc/cloudflared/credentials.json",
        "ingress:"
    ]

    for rule in ingress_rules:
        if "hostname" in rule:
            lines.append(f"  - hostname: {rule['hostname']}")
            lines.append(f"    service: {rule['service']}")
        else:
            lines.append(f"  - service: {rule['service']}")

    return "\n".join(lines)


async def create_cloudflare_tunnel(
    namespace: str,
    name: str,
    tunnel_id: str,
    tunnel_secret_name: str,
    odoo_hostname: Optional[str] = None,
    metabase_hostname: Optional[str] = None,
    metabase_enabled: bool = False,
    replicas: int = 1,
    owner_ref: Optional[dict] = None
) -> None:
    """Create Cloudflare Tunnel Deployment and ConfigMap.

    Args:
        namespace: Kubernetes namespace
        name: OdooCluster name
        tunnel_id: Cloudflare Tunnel UUID
        tunnel_secret_name: Name of secret containing credentials.json
        odoo_hostname: Public hostname for Odoo (e.g., www.simstech.cloud)
        metabase_hostname: Public hostname for Metabase (e.g., data.simstech.cloud)
        metabase_enabled: Whether Metabase is enabled
        replicas: Number of tunnel replicas (1 or 2 for HA)
        owner_ref: Owner reference for garbage collection
    """
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    resource_name = f"{name}-cloudflare-tunnel"
    odoo_service = f"{name}-odoo"
    metabase_service = f"{name}-metabase" if metabase_enabled else None

    labels = {
        "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
        "odoo.simstech.cloud/cluster": name,
        "odoo.simstech.cloud/component": "cloudflare-tunnel"
    }

    # Build tunnel config
    tunnel_config = build_tunnel_config(
        tunnel_id=tunnel_id,
        odoo_service=odoo_service,
        odoo_hostname=odoo_hostname,
        metabase_service=metabase_service,
        metabase_hostname=metabase_hostname if metabase_enabled else None
    )

    # Create ConfigMap with tunnel config
    configmap = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-config",
            namespace=namespace,
            owner_references=[client.V1OwnerReference(
                api_version=owner_ref.get('apiVersion'),
                kind=owner_ref.get('kind'),
                name=owner_ref.get('name'),
                uid=owner_ref.get('uid'),
                controller=True,
                block_owner_deletion=True
            )] if owner_ref else None,
            labels=labels
        ),
        data={
            "config.yaml": tunnel_config
        }
    )

    try:
        core_api.create_namespaced_config_map(namespace=namespace, body=configmap)
        logger.info(f"Created Cloudflare tunnel ConfigMap: {resource_name}-config")
    except ApiException as e:
        if e.status == 409:
            core_api.patch_namespaced_config_map(
                name=f"{resource_name}-config",
                namespace=namespace,
                body=configmap
            )
            logger.info(f"Updated Cloudflare tunnel ConfigMap: {resource_name}-config")
        else:
            raise

    # Create Deployment
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
                            "--config",
                            "/etc/cloudflared/config.yaml",
                            "--no-autoupdate",
                            "run"
                        ],
                        "volumeMounts": [
                            {
                                "name": "config",
                                "mountPath": "/etc/cloudflared/config.yaml",
                                "subPath": "config.yaml",
                                "readOnly": True
                            },
                            {
                                "name": "credentials",
                                "mountPath": "/etc/cloudflared/credentials.json",
                                "subPath": "credentials.json",
                                "readOnly": True
                            }
                        ],
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
                            "periodSeconds": 10
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
                    "volumes": [
                        {
                            "name": "config",
                            "configMap": {
                                "name": f"{resource_name}-config"
                            }
                        },
                        {
                            "name": "credentials",
                            "secret": {
                                "secretName": tunnel_secret_name
                            }
                        }
                    ]
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

