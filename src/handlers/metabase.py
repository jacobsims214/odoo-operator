"""
Metabase handler - Creates Metabase BI deployment connected to Odoo's PostgreSQL.
"""

import secrets
import string
from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf
from typing import Optional
from .tailscale import (
    get_tailscale_sidecar,
    get_tailscale_volumes,
    create_tailscale_resources,
    delete_tailscale_resources,
    get_tailscale_rbac
)


def generate_password(length: int = 32) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def create_metabase(
    namespace: str,
    name: str,
    storage: str = "5Gi",
    resources: dict = None,
    tailscale: Optional[dict] = None,
    tailscale_auth_secret: str = "tailscale-auth"
) -> None:
    """Create Metabase BI deployment."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    res = resources or {}
    requests = res.get('requests', {})
    limits = res.get('limits', {})

    resource_name = f"{name}-metabase"
    db_secret = f"{name}-db-app"

    # Create ServiceAccount
    sa = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            }
        )
    )

    try:
        core_api.create_namespaced_service_account(namespace=namespace, body=sa)
    except ApiException as e:
        if e.status != 409:
            raise

    # Create PVC for Metabase H2 database (or use app DB for production)
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-data",
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            }
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": storage}
            )
        )
    )

    try:
        core_api.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc)
    except ApiException as e:
        if e.status != 409:
            raise

    # Create admin secret (only if it doesn't exist - don't overwrite existing passwords)
    admin_secret_name = f"{resource_name}-admin"
    try:
        core_api.read_namespaced_secret(name=admin_secret_name, namespace=namespace)
        # Secret exists, don't overwrite
    except ApiException as e:
        if e.status == 404:
            # Create new secret with generated credentials
            # Note: Metabase requires completing setup wizard, these are suggested creds
            admin_email = f"admin@{name}.local"
            admin_password = generate_password(24)
            admin_secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=admin_secret_name,
                    namespace=namespace,
                    labels={
                        "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "metabase"
                    },
                    annotations={
                        "odoo.simstech.cloud/note": "Use these credentials when completing Metabase setup wizard"
                    }
                ),
                string_data={
                    "admin-email": admin_email,
                    "admin-password": admin_password,
                    "odoo-db-host": f"{name}-db-rw",
                    "odoo-db-name": "odoo",
                    "odoo-db-user": "odoo",
                }
            )
            core_api.create_namespaced_secret(namespace=namespace, body=admin_secret)
        else:
            raise

    # Setup Tailscale if enabled
    if tailscale:
        await create_tailscale_resources(
            namespace=namespace,
            name=name,
            component="metabase",
            target_port=3000,
            funnel=tailscale.get('funnel', False)
        )

        # Create RBAC for Tailscale
        role, role_binding = get_tailscale_rbac(namespace, name, "metabase")

        try:
            rbac_api.create_namespaced_role(namespace=namespace, body=role)
        except ApiException as e:
            if e.status == 409:
                rbac_api.patch_namespaced_role(
                    name=role['metadata']['name'],
                    namespace=namespace,
                    body=role
                )
            else:
                raise

        try:
            rbac_api.create_namespaced_role_binding(namespace=namespace, body=role_binding)
        except ApiException as e:
            if e.status == 409:
                rbac_api.patch_namespaced_role_binding(
                    name=role_binding['metadata']['name'],
                    namespace=namespace,
                    body=role_binding
                )
            else:
                raise

    # Build containers
    # Note: Using H2 embedded database for Metabase app data (simpler, stored in PVC)
    # The Odoo PostgreSQL connection is added as a data source during setup
    containers = [
        {
            "name": "metabase",
            "image": "metabase/metabase:v0.50.26",
            "ports": [{"containerPort": 3000, "name": "http"}],
            "env": [
                # Use embedded H2 database for Metabase app data (stored in /metabase-data)
                {"name": "MB_DB_FILE", "value": "/metabase-data/metabase.db"},
                # Jetty settings
                {"name": "JAVA_OPTS", "value": "-Xmx1g"},
                # Store Odoo DB connection info in env for reference during setup
                {"name": "ODOO_DB_HOST", "value": f"{name}-db-rw"},
                {"name": "ODOO_DB_PORT", "value": "5432"},
                {"name": "ODOO_DB_NAME", "value": "odoo"},
                {"name": "ODOO_DB_USER", "value": "odoo"},
                {
                    "name": "ODOO_DB_PASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_secret,
                            "key": "password"
                        }
                    }
                },
            ],
            "volumeMounts": [
                {
                    "name": "data",
                    "mountPath": "/metabase-data"
                }
            ],
            "resources": {
                "requests": {
                    "cpu": requests.get('cpu', '500m'),
                    "memory": requests.get('memory', '1Gi')
                },
                "limits": {
                    "cpu": limits.get('cpu', '2'),
                    "memory": limits.get('memory', '2Gi')
                }
            },
            "livenessProbe": {
                "httpGet": {
                    "path": "/api/health",
                    "port": 3000
                },
                "initialDelaySeconds": 120,
                "periodSeconds": 30
            },
            "readinessProbe": {
                "httpGet": {
                    "path": "/api/health",
                    "port": 3000
                },
                "initialDelaySeconds": 60,
                "periodSeconds": 10
            }
        }
    ]

    # Add Tailscale sidecar if enabled
    if tailscale:
        containers.append(
            get_tailscale_sidecar(
                name=name,
                namespace=namespace,
                hostname=tailscale.get('hostname', 'bi'),
                target_port=3000,
                funnel=tailscale.get('funnel', False),
                tags=tailscale.get('tags', 'tag:odoo-bi'),
                auth_secret_name=tailscale_auth_secret
            )
        )

    # Build volumes
    volumes = [
        {
            "name": "data",
            "persistentVolumeClaim": {
                "claimName": f"{resource_name}-data"
            }
        }
    ]

    if tailscale:
        volumes.extend(get_tailscale_volumes(f"{name}-metabase"))

    # Create Deployment (using raw dict for proper camelCase serialization)
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": resource_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            }
        },
        "spec": {
            "replicas": 1,
            "selector": {
                "matchLabels": {
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "metabase"
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "metabase"
                    }
                },
                "spec": {
                    "serviceAccountName": resource_name,
                    "containers": containers,
                    "volumes": volumes
                }
            }
        }
    }

    try:
        apps_api.create_namespaced_deployment(namespace=namespace, body=deployment)
    except ApiException as e:
        if e.status == 409:
            apps_api.patch_namespaced_deployment(
                name=resource_name,
                namespace=namespace,
                body=deployment
            )
        else:
            raise kopf.PermanentError(f"Failed to create Metabase: {e}")

    # Create Service
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            }
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            },
            ports=[
                client.V1ServicePort(
                    name="http",
                    port=3000,
                    target_port=3000
                )
            ]
        )
    )

    try:
        core_api.create_namespaced_service(namespace=namespace, body=service)
    except ApiException as e:
        if e.status == 409:
            core_api.patch_namespaced_service(
                name=resource_name,
                namespace=namespace,
                body=service
            )
        else:
            raise


async def delete_metabase(namespace: str, name: str) -> None:
    """Delete Metabase deployment and related resources."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    resource_name = f"{name}-metabase"

    # Delete deployment
    try:
        apps_api.delete_namespaced_deployment(name=resource_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete service
    try:
        core_api.delete_namespaced_service(name=resource_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete PVC
    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=f"{resource_name}-data",
            namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete Tailscale resources
    await delete_tailscale_resources(namespace, name, "metabase")

    # Delete RBAC
    try:
        rbac_api.delete_namespaced_role(name=f"{resource_name}-tailscale", namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    try:
        rbac_api.delete_namespaced_role_binding(name=f"{resource_name}-tailscale", namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete ServiceAccount
    try:
        core_api.delete_namespaced_service_account(name=resource_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

