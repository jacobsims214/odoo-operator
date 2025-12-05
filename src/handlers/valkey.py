"""
Valkey handler - Creates Valkey (Redis-compatible) StatefulSet for caching/sessions.
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf
from typing import Optional


def build_owner_references(owner_ref: Optional[dict]) -> Optional[list]:
    """Convert owner_ref dict to V1OwnerReference list."""
    if not owner_ref:
        return None
    return [
        client.V1OwnerReference(
            api_version=owner_ref.get('apiVersion'),
            kind=owner_ref.get('kind'),
            name=owner_ref.get('name'),
            uid=owner_ref.get('uid'),
            controller=owner_ref.get('controller', True),
            block_owner_deletion=owner_ref.get('blockOwnerDeletion', True)
        )
    ]


async def create_valkey(
    namespace: str,
    name: str,
    storage: str = "1Gi",
    resources: dict = None,
    owner_ref: Optional[dict] = None
) -> None:
    """Create Valkey StatefulSet for Odoo caching."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    res = resources or {}
    requests = res.get('requests', {})
    limits = res.get('limits', {})

    # Ensure resource values are strings (Kubernetes requires string quantities)
    def ensure_str(val, default):
        if val is None:
            return default
        return str(val)

    resource_name = f"{name}-valkey"

    owner_refs = build_owner_references(owner_ref)

    # Create Service (headless for StatefulSet)
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "valkey"
            },
            owner_references=owner_refs
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "valkey"
            },
            ports=[
                client.V1ServicePort(
                    name="redis",
                    port=6379,
                    target_port=6379
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

    # Create StatefulSet
    statefulset = client.V1StatefulSet(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "valkey"
            },
            owner_references=owner_refs
        ),
        spec=client.V1StatefulSetSpec(
            service_name=resource_name,
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "valkey"
                }
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "valkey"
                    }
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="valkey",
                            image="valkey/valkey:8-alpine",
                            ports=[
                                client.V1ContainerPort(
                                    container_port=6379,
                                    name="redis"
                                )
                            ],
                            args=[
                                "valkey-server",
                                "--appendonly", "yes",
                                "--maxmemory", "256mb",
                                "--maxmemory-policy", "allkeys-lru"
                            ],
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name="data",
                                    mount_path="/data"
                                )
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={
                                    "cpu": ensure_str(requests.get('cpu'), '100m'),
                                    "memory": ensure_str(requests.get('memory'), '128Mi')
                                },
                                limits={
                                    "cpu": ensure_str(limits.get('cpu'), '500m'),
                                    "memory": ensure_str(limits.get('memory'), '512Mi')
                                }
                            ),
                            liveness_probe=client.V1Probe(
                                tcp_socket=client.V1TCPSocketAction(port=6379),
                                initial_delay_seconds=30,
                                period_seconds=10
                            ),
                            readiness_probe=client.V1Probe(
                                tcp_socket=client.V1TCPSocketAction(port=6379),
                                initial_delay_seconds=5,
                                period_seconds=5
                            )
                        )
                    ]
                )
            ),
            volume_claim_templates=[
                client.V1PersistentVolumeClaim(
                    metadata=client.V1ObjectMeta(
                        name="data",
                        labels={
                            "odoo.simstech.cloud/cluster": name,
                            "odoo.simstech.cloud/component": "valkey"
                        }
                    ),
                    spec=client.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOnce"],
                        resources=client.V1VolumeResourceRequirements(
                            requests={"storage": storage}
                        )
                    )
                )
            ]
        )
    )

    try:
        apps_api.create_namespaced_stateful_set(namespace=namespace, body=statefulset)
    except ApiException as e:
        if e.status == 409:
            apps_api.patch_namespaced_stateful_set(
                name=resource_name,
                namespace=namespace,
                body=statefulset
            )
        else:
            raise kopf.PermanentError(f"Failed to create Valkey: {e}")


async def delete_valkey(namespace: str, name: str) -> None:
    """Delete Valkey StatefulSet and related resources."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()

    resource_name = f"{name}-valkey"

    # Delete StatefulSet
    try:
        apps_api.delete_namespaced_stateful_set(name=resource_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete Service
    try:
        core_api.delete_namespaced_service(name=resource_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Note: PVCs from StatefulSet need manual cleanup or use cascade delete
    # The namespace deletion will handle this

