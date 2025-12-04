"""
Tailscale helper - Generates sidecar containers and related resources.
"""

from kubernetes import client


def get_tailscale_sidecar(
    name: str,
    namespace: str,
    hostname: str,
    target_port: int,
    funnel: bool = True,
    tags: str = "tag:odoo",
    auth_secret_name: str = "tailscale-auth"
) -> dict:
    """Generate Tailscale sidecar container spec."""
    return {
        "name": "tailscale",
        "image": "tailscale/tailscale:v1.76.1",
        "imagePullPolicy": "IfNotPresent",
        "env": [
            {
                "name": "TS_AUTHKEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": auth_secret_name,
                        "key": "TS_AUTHKEY"
                    }
                }
            },
            {"name": "TS_HOSTNAME", "value": hostname},
            {"name": "TS_STATE_DIR", "value": "/var/lib/tailscale"},
            {"name": "TS_USERSPACE", "value": "false"},
            {"name": "TS_SERVE_CONFIG", "value": "/config/serve.json"},
            {"name": "TS_EXTRA_ARGS", "value": f"--advertise-tags={tags}"}
        ],
        "securityContext": {
            "capabilities": {
                "add": ["NET_ADMIN"]
            }
        },
        "volumeMounts": [
            {
                "name": "tailscale-state",
                "mountPath": "/var/lib/tailscale"
            },
            {
                "name": "tailscale-config",
                "mountPath": "/config"
            }
        ]
    }


def get_tailscale_volumes(name: str) -> list:
    """Generate volume specs for Tailscale."""
    return [
        {
            "name": "tailscale-state",
            "persistentVolumeClaim": {
                "claimName": f"{name}-tailscale-state"
            }
        },
        {
            "name": "tailscale-config",
            "configMap": {
                "name": f"{name}-tailscale-config"
            }
        }
    ]


def get_serve_config(target_port: int, funnel: bool = True) -> dict:
    """Generate Tailscale serve config."""
    config = {
        "TCP": {
            "443": {
                "HTTPS": True
            }
        },
        "Web": {
            "443": {
                "Handlers": {
                    "/": {
                        "Proxy": f"http://127.0.0.1:{target_port}"
                    }
                }
            }
        }
    }

    if funnel:
        config["AllowFunnel"] = {"443": True}

    return config


async def create_tailscale_resources(
    namespace: str,
    name: str,
    component: str,  # "odoo" or "bi"
    target_port: int,
    funnel: bool = True
) -> None:
    """Create Tailscale ConfigMap and PVC."""
    import json
    api = client.CoreV1Api()

    resource_name = f"{name}-{component}"

    # Create ConfigMap with serve config
    serve_config = get_serve_config(target_port, funnel)

    configmap = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-tailscale-config",
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "simstech-odoo-operator",
                "simstech-odoo/cluster": name,
                "simstech-odoo/component": component
            }
        ),
        data={
            "serve.json": json.dumps(serve_config, indent=2)
        }
    )

    try:
        api.create_namespaced_config_map(namespace=namespace, body=configmap)
    except client.rest.ApiException as e:
        if e.status == 409:
            api.patch_namespaced_config_map(
                name=f"{resource_name}-tailscale-config",
                namespace=namespace,
                body=configmap
            )
        else:
            raise

    # Create PVC for Tailscale state
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-tailscale-state",
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "simstech-odoo-operator",
                "simstech-odoo/cluster": name,
                "simstech-odoo/component": component
            }
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": "100Mi"}
            )
        )
    )

    try:
        api.create_namespaced_persistent_volume_claim(namespace=namespace, body=pvc)
    except client.rest.ApiException as e:
        if e.status != 409:  # Ignore already exists
            raise


async def delete_tailscale_resources(namespace: str, name: str, component: str) -> None:
    """Delete Tailscale resources."""
    api = client.CoreV1Api()
    resource_name = f"{name}-{component}"

    try:
        api.delete_namespaced_config_map(
            name=f"{resource_name}-tailscale-config",
            namespace=namespace
        )
    except client.rest.ApiException as e:
        if e.status != 404:
            raise

    try:
        api.delete_namespaced_persistent_volume_claim(
            name=f"{resource_name}-tailscale-state",
            namespace=namespace
        )
    except client.rest.ApiException as e:
        if e.status != 404:
            raise


def get_tailscale_rbac(namespace: str, name: str, component: str) -> tuple:
    """Generate RBAC resources for Tailscale state secret management."""
    resource_name = f"{name}-{component}"

    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {
            "name": f"{resource_name}-tailscale",
            "namespace": namespace
        },
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["create", "get", "update", "patch", "delete"]
            }
        ]
    }

    role_binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": f"{resource_name}-tailscale",
            "namespace": namespace
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": resource_name,
                "namespace": namespace
            }
        ],
        "roleRef": {
            "kind": "Role",
            "name": f"{resource_name}-tailscale",
            "apiGroup": "rbac.authorization.k8s.io"
        }
    }

    return role, role_binding

