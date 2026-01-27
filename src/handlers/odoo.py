"""
Odoo handler - Creates Odoo deployment and related resources.
"""

import hashlib
import json
import secrets
import string
from kubernetes import client
from kubernetes.client.rest import ApiException
import kopf
from typing import Optional, List
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


def build_git_clone_script(addons: List[dict]) -> str:
    """Build a shell script to clone all addon repositories.

    Handles concurrent pod starts by:
    1. Using a lock file to serialize git operations
    2. Removing stale git lock files
    3. Skipping update if repo is already present (pods don't need latest on every restart)
    """
    script_lines = [
        "#!/bin/sh",
        "set -e",
        "",
        "# Fix git dubious ownership errors (shared volumes between containers)",
        "git config --global --add safe.directory '*'",
        "",
        "# Configure SSH for private repos",
        "mkdir -p ~/.ssh",
        "chmod 700 ~/.ssh",
        "ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null || true",
        "ssh-keyscan gitlab.com >> ~/.ssh/known_hosts 2>/dev/null || true",
        "ssh-keyscan bitbucket.org >> ~/.ssh/known_hosts 2>/dev/null || true",
        "",
        "# Lock file for serializing git operations across pods",
        "LOCK_FILE=/mnt/addons/.clone.lock",
        "",
        "# Function to clean up stale git locks",
        "cleanup_git_locks() {",
        "  local dir=$1",
        "  if [ -f \"$dir/.git/index.lock\" ]; then",
        "    echo \"Removing stale git lock: $dir/.git/index.lock\"",
        "    rm -f \"$dir/.git/index.lock\"",
        "  fi",
        "}",
        "",
        "# Wait for any other clone operations to finish (max 60 seconds)",
        "wait_for_lock() {",
        "  local count=0",
        "  while [ -f \"$LOCK_FILE\" ] && [ $count -lt 60 ]; do",
        "    echo \"Waiting for other clone operation to finish...\"",
        "    sleep 2",
        "    count=$((count + 2))",
        "  done",
        "  # Remove stale lock if it's been too long",
        "  rm -f \"$LOCK_FILE\" 2>/dev/null || true",
        "}",
        "",
        "# Acquire lock",
        "wait_for_lock",
        "echo $$ > \"$LOCK_FILE\"",
        "trap 'rm -f $LOCK_FILE' EXIT",
        "",
    ]

    for addon in addons:
        name = addon['name']
        repo = addon['repo']
        branch = addon.get('branch', 'main')
        path = addon.get('path', '')
        deploy_key_secret = addon.get('deployKeySecret')

        target_dir = f"/mnt/addons/{name}"

        script_lines.append(f"echo 'Processing addon: {name}'")

        # If private repo with deploy key
        if deploy_key_secret:
            key_path = f"/keys/{deploy_key_secret}/ssh-privatekey"
            script_lines.append(f"export GIT_SSH_COMMAND='ssh -i {key_path} -o StrictHostKeyChecking=no'")

        # Check if repo exists and is a valid git repo
        script_lines.append(f"if [ -d '{target_dir}/.git' ]; then")
        script_lines.append(f"  echo 'Repo {name} already exists, checking branch...'")
        script_lines.append(f"  cleanup_git_locks {target_dir}")
        script_lines.append(f"  cd {target_dir}")
        # Just verify we're on the right branch, don't pull (avoids conflicts)
        script_lines.append("  CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo 'unknown')")
        script_lines.append(f"  if [ \"$CURRENT_BRANCH\" = \"{branch}\" ]; then")
        script_lines.append(f"    echo 'Already on branch {branch}, skipping update'")
        script_lines.append("  else")
        script_lines.append(f"    echo 'Switching to branch {branch}'")
        script_lines.append(f"    git fetch origin {branch} --depth 1 || true")
        script_lines.append(f"    git checkout {branch} || git checkout -b {branch} origin/{branch}")
        script_lines.append("  fi")
        script_lines.append(f"elif [ -d '{target_dir}' ]; then")
        script_lines.append(f"  echo 'Directory {name} exists but is not a git repo, removing and cloning fresh...'")
        script_lines.append(f"  rm -rf {target_dir}")
        script_lines.append(f"  git clone --depth 1 --branch {branch} {repo} {target_dir}")
        script_lines.append("else")
        script_lines.append(f"  echo 'Fresh clone of {name}...'")
        script_lines.append(f"  rm -rf {target_dir}")  # Clean up partial clones
        script_lines.append(f"  git clone --depth 1 --branch {branch} {repo} {target_dir}")
        script_lines.append("fi")

        # If path specified, note it
        if path:
            script_lines.append(f"# Module to install: {path}")

        script_lines.append("")

        # Clear SSH command for next iteration
        if deploy_key_secret:
            script_lines.append("unset GIT_SSH_COMMAND")
            script_lines.append("")

    script_lines.append("echo 'All addons processed successfully!'")
    script_lines.append("ls -la /mnt/addons/")

    return "\n".join(script_lines)


def build_addons_path(addons: List[dict]) -> str:
    """Build the addons_path for odoo.conf.

    Always adds the repo root to addons_path.
    The 'path' field specifies which module(s) to install, not the path.
    Odoo will discover all modules in each addons_path directory.
    """
    paths = ["/mnt/extra-addons"]  # Default Odoo addons

    for addon in addons:
        name = addon['name']
        # Always add repo root - Odoo discovers modules in addons_path directories
        # The 'path' field is for specifying which module to auto-install (future use)
        paths.append(f"/mnt/addons/{name}")

    return ",".join(paths)


def compute_config_hash(
    version: str,
    image: Optional[str],
    addons: List[dict],
    resources: dict,
    db_host: str,
    tailscale: Optional[dict]
) -> str:
    """Compute a hash of the configuration to trigger pod restarts on changes."""
    config = {
        "version": version,
        "image": image,
        "addons": addons,
        "resources": resources,
        "db_host": db_host,
        "tailscale": tailscale
    }
    config_json = json.dumps(config, sort_keys=True)
    return hashlib.sha256(config_json.encode()).hexdigest()[:16]


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


async def create_odoo(
    namespace: str,
    name: str,
    version: str,
    image: Optional[str] = None,
    replicas: int = 1,
    storage: str = "10Gi",
    storage_class_name: Optional[str] = None,
    resources: dict = None,
    addons: List[dict] = None,
    db_host: str = None,
    db_secret: str = None,
    valkey_enabled: bool = False,
    valkey_host: Optional[str] = None,
    tailscale: Optional[dict] = None,
    tailscale_auth_secret: str = "tailscale-auth",
    owner_ref: Optional[dict] = None
) -> None:
    """Create Odoo deployment with all related resources."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    odoo_image = image or f"odoo:{version}"
    res = resources or {}
    requests = res.get('requests', {})
    limits = res.get('limits', {})
    addons = addons or []

    # Ensure resource values are strings (Kubernetes requires string quantities)
    def ensure_str(val, default):
        if val is None:
            return default
        return str(val)

    resource_name = f"{name}-odoo"
    owner_refs = build_owner_references(owner_ref)

    # Create ServiceAccount
    sa = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            owner_references=owner_refs,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "odoo"
            }
        )
    )

    try:
        core_api.create_namespaced_service_account(namespace=namespace, body=sa)
    except ApiException as e:
        if e.status != 409:
            raise

    # Create PVC for filestore
    pvc_spec = client.V1PersistentVolumeClaimSpec(
        access_modes=["ReadWriteMany"],  # RWX for multi-replica support with NFS/EFS
        resources=client.V1VolumeResourceRequirements(
            requests={"storage": storage}
        )
    )
    # Set storage class if specified (e.g., efs-sc for AWS EFS)
    if storage_class_name:
        pvc_spec.storage_class_name = storage_class_name

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-filestore",
            namespace=namespace,
            owner_references=owner_refs,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "odoo"
            }
        ),
        spec=pvc_spec
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
            # Create new secret with generated password
            admin_password = generate_password(32)
            admin_secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=admin_secret_name,
                    namespace=namespace,
                    owner_references=owner_refs,
                    labels={
                        "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "odoo"
                    }
                ),
                string_data={
                    "admin-password": admin_password,
                }
            )
            core_api.create_namespaced_secret(namespace=namespace, body=admin_secret)
        else:
            raise

    # Create PVC for addons (if any addons defined)
    # Note: This PVC may already be created by the db_init job, which is fine (409 = skip)
    if addons:
        addons_pvc_spec = client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],  # RWX for multi-replica support with NFS/EFS
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": "5Gi"}
            )
        )
        if storage_class_name:
            addons_pvc_spec.storage_class_name = storage_class_name

        addons_pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=f"{resource_name}-addons",
                namespace=namespace,
                owner_references=owner_refs,
                labels={
                    "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "odoo"
                }
            ),
            spec=addons_pvc_spec
        )

        try:
            core_api.create_namespaced_persistent_volume_claim(namespace=namespace, body=addons_pvc)
        except ApiException as e:
            if e.status != 409:
                raise

    # Build addons path for odoo.conf
    addons_path = build_addons_path(addons) if addons else "/mnt/extra-addons"

    # Build Redis session store config if Valkey is enabled
    redis_config = ""
    if valkey_enabled and valkey_host:
        redis_config = f"""
; Redis session store (Valkey)
redis_session_store = True
redis_host = {valkey_host}
redis_port = 6379
"""

    # Create ConfigMap for odoo.conf
    # Note: admin_passwd is set via environment variable for security
    odoo_conf = f"""[options]
db_host = {db_host}
db_port = 5432
db_user = odoo
db_name = odoo
data_dir = /var/lib/odoo
addons_path = {addons_path}
proxy_mode = True
list_db = False
{redis_config}
"""

    configmap_data = {
        "odoo.conf": odoo_conf
    }

    # Add git clone script if we have addons
    if addons:
        configmap_data["clone-addons.sh"] = build_git_clone_script(addons)

    configmap = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=f"{resource_name}-config",
            namespace=namespace,
            owner_references=owner_refs,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "odoo"
            }
        ),
        data=configmap_data
    )

    try:
        core_api.create_namespaced_config_map(namespace=namespace, body=configmap)
    except ApiException as e:
        if e.status == 409:
            core_api.patch_namespaced_config_map(
                name=f"{resource_name}-config",
                namespace=namespace,
                body=configmap
            )
        else:
            raise

    # Setup Tailscale if enabled
    if tailscale:
        await create_tailscale_resources(
            namespace=namespace,
            name=name,
            component="odoo",
            target_port=8069,
            funnel=tailscale.get('funnel', True)
        )

        # Create RBAC for Tailscale
        role, role_binding = get_tailscale_rbac(namespace, name, "odoo")

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

    # Build init containers for cloning addons
    init_containers = []
    if addons:
        init_containers.append({
            "name": "clone-addons",
            "image": "alpine/git:latest",
            "command": ["/bin/sh", "/scripts/clone-addons.sh"],
            "volumeMounts": [
                {
                    "name": "addons",
                    "mountPath": "/mnt/addons"
                },
                {
                    "name": "config",
                    "mountPath": "/scripts"
                }
            ]
        })

        # Add deploy key mounts for private repos
        deploy_keys = set()
        for addon in addons:
            if addon.get('deployKeySecret'):
                deploy_keys.add(addon['deployKeySecret'])

        for key_secret in deploy_keys:
            init_containers[0]["volumeMounts"].append({
                "name": f"deploy-key-{key_secret}",
                "mountPath": f"/keys/{key_secret}",
                "readOnly": True
            })

    # NOTE: Database initialization is now handled by a separate Job (db_init.py)
    # This allows pods to scale freely without race conditions.
    # The Job runs ONCE before any pods are created.

    # Build main containers
    odoo_volume_mounts = [
        {
            "name": "filestore",
            "mountPath": "/var/lib/odoo"
        },
        {
            "name": "config",
            "mountPath": "/etc/odoo/odoo.conf",
            "subPath": "odoo.conf"
        }
    ]

    if addons:
        odoo_volume_mounts.append({
            "name": "addons",
            "mountPath": "/mnt/addons"
        })

    # Build environment variables
    # Odoo Docker image expects HOST, USER, PASSWORD env vars
    odoo_env = [
        # Database connection (Odoo Docker image standard env vars)
        {"name": "HOST", "value": db_host},
        {"name": "USER", "value": "odoo"},
        {
            "name": "PASSWORD",
            "valueFrom": {
                "secretKeyRef": {
                    "name": db_secret,
                    "key": "password"
                }
            }
        },
        # Also set PGPASSWORD for any direct psql usage
        {
            "name": "PGPASSWORD",
            "valueFrom": {
                "secretKeyRef": {
                    "name": db_secret,
                    "key": "password"
                }
            }
        },
        # Admin master password
        {
            "name": "ODOO_ADMIN_PASSWD",
            "valueFrom": {
                "secretKeyRef": {
                    "name": f"{resource_name}-admin",
                    "key": "admin-password"
                }
            }
        }
    ]

    # Add Redis env vars if Valkey is enabled
    if valkey_enabled and valkey_host:
        odoo_env.extend([
            {"name": "ODOO_REDIS_HOST", "value": valkey_host},
            {"name": "ODOO_REDIS_PORT", "value": "6379"},
        ])

    containers = [
        {
            "name": "odoo",
            "image": odoo_image,
            "imagePullPolicy": "Always",  # Always pull to get latest tag updates
            "ports": [{"containerPort": 8069, "name": "http"}],
            "env": odoo_env,
            "volumeMounts": odoo_volume_mounts,
            "resources": {
                "requests": {
                    "cpu": ensure_str(requests.get('cpu'), '500m'),
                    "memory": ensure_str(requests.get('memory'), '1Gi')
                },
                "limits": {
                    "cpu": ensure_str(limits.get('cpu'), '2'),
                    "memory": ensure_str(limits.get('memory'), '4Gi')
                }
            },
            "livenessProbe": {
                "httpGet": {
                    "path": "/web/health",
                    "port": 8069
                },
                "initialDelaySeconds": 60,
                "periodSeconds": 30
            },
            "readinessProbe": {
                "httpGet": {
                    "path": "/web/health",
                    "port": 8069
                },
                "initialDelaySeconds": 30,
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
                hostname=tailscale.get('hostname', 'odoo'),
                target_port=8069,
                funnel=tailscale.get('funnel', True),
                tags=tailscale.get('tags', 'tag:odoo-web'),
                auth_secret_name=tailscale_auth_secret
            )
        )

    # Build volumes
    volumes = [
        {
            "name": "filestore",
            "persistentVolumeClaim": {
                "claimName": f"{resource_name}-filestore"
            }
        },
        {
            "name": "config",
            "configMap": {
                "name": f"{resource_name}-config",
                "defaultMode": 0o755
            }
        }
    ]

    if addons:
        volumes.append({
            "name": "addons",
            "persistentVolumeClaim": {
                "claimName": f"{resource_name}-addons"
            }
        })

        # Add deploy key volumes
        deploy_keys = set()
        for addon in addons:
            if addon.get('deployKeySecret'):
                deploy_keys.add(addon['deployKeySecret'])

        for key_secret in deploy_keys:
            volumes.append({
                "name": f"deploy-key-{key_secret}",
                "secret": {
                    "secretName": key_secret,
                    "defaultMode": 0o400
                }
            })

    if tailscale:
        volumes.extend(get_tailscale_volumes(f"{name}-odoo"))

    # Compute config hash for rollout triggering
    config_hash = compute_config_hash(
        version=version,
        image=image,
        addons=addons,
        resources=res,
        db_host=db_host,
        tailscale=tailscale
    )

    # Build pod spec
    pod_spec = {
        "serviceAccountName": resource_name,
        "containers": containers,
        "volumes": volumes
    }

    if init_containers:
        pod_spec["initContainers"] = init_containers

    # Create Deployment (using raw dict for proper camelCase serialization)
    deployment_metadata = {
        "name": resource_name,
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
            "odoo.simstech.cloud/cluster": name,
            "odoo.simstech.cloud/component": "odoo"
        }
    }
    if owner_ref:
        deployment_metadata["ownerReferences"] = [owner_ref]

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": deployment_metadata,
        "spec": {
            "replicas": replicas,
            "selector": {
                "matchLabels": {
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "odoo"
                }
            },
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {
                    "maxUnavailable": 0,
                    "maxSurge": 1
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "odoo"
                    },
                    "annotations": {
                        "odoo.simstech.cloud/config-hash": config_hash
                    }
                },
                "spec": pod_spec
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
            raise kopf.PermanentError(f"Failed to create Odoo deployment: {e}")

    # Create Service
    service = client.V1Service(
        metadata=client.V1ObjectMeta(
            name=resource_name,
            namespace=namespace,
            owner_references=owner_refs,
            labels={
                "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "odoo"
            }
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={
                "odoo.simstech.cloud/cluster": name,
                "odoo.simstech.cloud/component": "odoo"
            },
            ports=[
                client.V1ServicePort(
                    name="http",
                    port=8069,
                    target_port=8069
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


async def delete_odoo(namespace: str, name: str) -> None:
    """Delete Odoo deployment and related resources."""
    core_api = client.CoreV1Api()
    apps_api = client.AppsV1Api()
    rbac_api = client.RbacAuthorizationV1Api()

    resource_name = f"{name}-odoo"

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

    # Delete configmap
    try:
        core_api.delete_namespaced_config_map(name=f"{resource_name}-config", namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete PVCs
    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=f"{resource_name}-filestore",
            namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            raise

    try:
        core_api.delete_namespaced_persistent_volume_claim(
            name=f"{resource_name}-addons",
            namespace=namespace
        )
    except ApiException as e:
        if e.status != 404:
            raise

    # Delete Tailscale resources
    await delete_tailscale_resources(namespace, name, "odoo")

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
