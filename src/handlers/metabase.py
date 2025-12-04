"""
Metabase handler - Creates Metabase BI deployment connected to Odoo's PostgreSQL.
"""

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
    containers = [
        {
            "name": "metabase",
            "image": "metabase/metabase:v0.50.26",
            "ports": [{"containerPort": 3000, "name": "http"}],
            "env": [
                # Use the Odoo PostgreSQL for Metabase app database
                {"name": "MB_DB_TYPE", "value": "postgres"},
                {"name": "MB_DB_HOST", "value": f"{name}-db-rw"},
                {"name": "MB_DB_PORT", "value": "5432"},
                {"name": "MB_DB_DBNAME", "value": "metabase"},
                {"name": "MB_DB_USER", "value": "odoo"},
                {
                    "name": "MB_DB_PASS",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": db_secret,
                            "key": "password"
                        }
                    }
                },
                # Jetty settings
                {"name": "JAVA_OPTS", "value": "-Xmx1g"}
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

    # Create Deployment
    deployment = client.V1Deployment(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "metabase"
            }
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "metabase"
                }
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "metabase"
                    }
                ),
                spec=client.V1PodSpec(
                    service_account_name=resource_name,
                    containers=[client.V1Container(**c) for c in containers],
                    volumes=[client.V1Volume(**v) for v in volumes]
                )
            )
        )
    )

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

