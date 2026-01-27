"""
Database Initialization Job handler.

Phase 1: Creates a Kubernetes Job that initializes the Odoo database.
This runs ONCE before any Odoo pods are created, preventing race conditions.

The job also:
- Clones addon repositories
- Installs modules that have a 'path' specified (e.g., session_db)
"""

import secrets
import string
from kubernetes import client
from kubernetes.client.rest import ApiException
from typing import Optional, List


def generate_password(length: int = 32) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def build_clone_script(addons: List[dict]) -> str:
    """Build a shell script to clone addon repositories."""
    if not addons:
        return ""

    lines = [
        "echo '=== Cloning addon repositories ==='",
        "mkdir -p /mnt/addons",
        "",
        "# Fix git dubious ownership errors (shared volumes between containers)",
        "git config --global --add safe.directory '*'",
        "",
        "# Configure SSH for private repos",
        "mkdir -p ~/.ssh",
        "chmod 700 ~/.ssh",
        "ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null || true",
        "ssh-keyscan gitlab.com >> ~/.ssh/known_hosts 2>/dev/null || true",
        "",
    ]

    for addon in addons:
        addon_name = addon['name']
        repo = addon['repo']
        branch = addon.get('branch', 'main')
        deploy_key = addon.get('deployKeySecret')

        lines.append(f"echo 'Cloning {addon_name}...'")

        if deploy_key:
            key_path = f"/keys/{deploy_key}/ssh-privatekey"
            lines.append(f"export GIT_SSH_COMMAND='ssh -i {key_path} -o StrictHostKeyChecking=no'")

        lines.append(f"if [ -d '/mnt/addons/{addon_name}/.git' ]; then")
        lines.append(f"  echo '{addon_name} already exists, skipping'")
        lines.append("else")
        lines.append(f"  git clone --depth 1 --branch {branch} {repo} /mnt/addons/{addon_name}")
        lines.append("fi")

        if deploy_key:
            lines.append("unset GIT_SSH_COMMAND")
        lines.append("")

    lines.append("echo 'Addon cloning complete'")
    lines.append("ls -la /mnt/addons/")
    return "\n".join(lines)


def get_modules_to_install(addons: List[dict]) -> List[str]:
    """Extract module names from addons that have install: true."""
    modules = []
    for addon in addons:
        # Only install if explicitly requested with install: true
        if addon.get('install', False):
            path = addon.get('path')
            if path:
                # The 'path' field specifies the module name to install
                modules.append(path)
    return modules


def build_addons_path(addons: List[dict]) -> str:
    """Build the addons_path for Odoo CLI."""
    paths = ["/mnt/extra-addons"]
    for addon in addons:
        paths.append(f"/mnt/addons/{addon['name']}")
    return ",".join(paths)


async def create_db_init_job(
    namespace: str,
    name: str,
    odoo_image: str,
    db_host: str,
    db_secret: str,
    admin_secret_name: str,
    addons: List[dict] = None,
    storage_class_name: Optional[str] = None,
    owner_ref: Optional[dict] = None
) -> str:
    """Create a Job to initialize the Odoo database and install addon modules.

    Returns the Job name for status tracking.
    """
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    job_name = f"{name}-db-init"
    addons = addons or []

    # Create admin secret if it doesn't exist
    admin_password = generate_password(32)
    try:
        core_api.read_namespaced_secret(name=admin_secret_name, namespace=namespace)
        # Secret exists, don't overwrite
    except ApiException as e:
        if e.status == 404:
            admin_secret = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=admin_secret_name,
                    namespace=namespace,
                    owner_references=[
                        client.V1OwnerReference(
                            api_version=owner_ref.get('apiVersion'),
                            kind=owner_ref.get('kind'),
                            name=owner_ref.get('name'),
                            uid=owner_ref.get('uid'),
                            controller=True,
                            block_owner_deletion=True
                        )
                    ] if owner_ref else None,
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

    # Create addons PVC if we have addons (shared with Odoo deployment)
    if addons:
        pvc_spec = client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteMany"],
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": "5Gi"}
            )
        )
        if storage_class_name:
            pvc_spec.storage_class_name = storage_class_name

        addons_pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=f"{name}-odoo-addons",
                namespace=namespace,
                owner_references=[
                    client.V1OwnerReference(
                        api_version=owner_ref.get('apiVersion'),
                        kind=owner_ref.get('kind'),
                        name=owner_ref.get('name'),
                        uid=owner_ref.get('uid'),
                        controller=True,
                        block_owner_deletion=True
                    )
                ] if owner_ref else None,
                labels={
                    "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
                    "odoo.simstech.cloud/cluster": name,
                    "odoo.simstech.cloud/component": "odoo"
                }
            ),
            spec=pvc_spec
        )

        try:
            core_api.create_namespaced_persistent_volume_claim(namespace=namespace, body=addons_pvc)
        except ApiException as e:
            if e.status != 409:
                raise

    # Build clone script for addons
    clone_script = build_clone_script(addons)

    # Get modules to install from addons with 'path' specified
    modules_to_install = get_modules_to_install(addons)
    modules_str = ",".join(["base"] + modules_to_install) if modules_to_install else "base"

    # Build addons path for Odoo
    addons_path = build_addons_path(addons) if addons else "/mnt/extra-addons"

    # Build the init script
    init_script = f"""
echo "=== Odoo Database Initialization Job ==="

{clone_script}

echo "Waiting for database to be ready..."

for i in $(seq 1 120); do
    if python3 -c "import psycopg2; psycopg2.connect(host='{db_host}', port=5432, user='odoo', password='$DB_PASSWORD', dbname='odoo')" 2>/dev/null; then
        echo "Database is ready!"
        break
    fi
    echo "Waiting for database... attempt $i/120"
    sleep 5
done

echo "Checking if Odoo database needs initialization..."

# Check if ir_module_module table exists (indicates initialized DB)
if python3 -c "
import psycopg2
import os
conn = psycopg2.connect(host='{db_host}', port=5432, user='odoo', password=os.environ['DB_PASSWORD'], dbname='odoo')
cur = conn.cursor()
cur.execute('SELECT 1 FROM ir_module_module LIMIT 1')
print('DB_INITIALIZED')
conn.close()
" 2>/dev/null | grep -q "DB_INITIALIZED"; then
    echo "Database already initialized, checking for modules to install..."

    # Install additional modules if needed (modules not yet installed)
    echo "Installing modules: {modules_str}"
    odoo --database=odoo --db_host={db_host} --db_port=5432 --db_user=odoo --db_password="$DB_PASSWORD" \\
         --addons-path={addons_path} \\
         --stop-after-init --init={modules_str} --without-demo=True --no-http || true
else
    echo "Database not initialized, running odoo -i {modules_str}..."
    odoo --database=odoo --db_host={db_host} --db_port=5432 --db_user=odoo --db_password="$DB_PASSWORD" \\
         --addons-path={addons_path} \\
         --stop-after-init --init={modules_str} --without-demo=True --no-http
    echo "Database initialization complete"
fi

# Set admin user password
echo "Setting admin user password..."
python3 << 'PYTHON_SCRIPT'
import psycopg2
import os
from passlib.context import CryptContext

try:
    conn = psycopg2.connect(
        host='{db_host}',
        port=5432,
        user='odoo',
        password=os.environ.get('DB_PASSWORD'),
        dbname='odoo'
    )
    cur = conn.cursor()

    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin')
    ctx = CryptContext(schemes=['pbkdf2_sha512'])
    hashed = ctx.hash(admin_password)

    cur.execute("UPDATE res_users SET password=%s WHERE login='admin'", (hashed,))
    conn.commit()
    print("Admin user password set successfully")
    conn.close()
except Exception as e:
    print(f"Warning: Could not set admin password: {{e}}")
PYTHON_SCRIPT

echo "=== Database initialization job complete ==="
echo "Installed modules: {modules_str}"
"""

    # Build Job spec
    job_metadata = {
        "name": job_name,
        "namespace": namespace,
        "labels": {
            "app.kubernetes.io/managed-by": "odoo.simstech.cloud-operator",
            "odoo.simstech.cloud/cluster": name,
            "odoo.simstech.cloud/component": "db-init"
        }
    }
    if owner_ref:
        job_metadata["ownerReferences"] = [owner_ref]

    # Build volume mounts for the container
    volume_mounts = []
    volumes = []

    if addons:
        volume_mounts.append({
            "name": "addons",
            "mountPath": "/mnt/addons"
        })
        volumes.append({
            "name": "addons",
            "persistentVolumeClaim": {
                "claimName": f"{name}-odoo-addons"
            }
        })

        # Add deploy key volumes for private repos
        deploy_keys = set()
        for addon in addons:
            if addon.get('deployKeySecret'):
                deploy_keys.add(addon['deployKeySecret'])

        for key_secret in deploy_keys:
            volume_mounts.append({
                "name": f"deploy-key-{key_secret}",
                "mountPath": f"/keys/{key_secret}",
                "readOnly": True
            })
            volumes.append({
                "name": f"deploy-key-{key_secret}",
                "secret": {
                    "secretName": key_secret,
                    "defaultMode": 0o400
                }
            })

    # Use alpine/git for init container if we have addons to clone
    init_containers = []
    if addons:
        init_containers.append({
            "name": "clone-addons",
            "image": "alpine/git:latest",
            "command": ["/bin/sh", "-c", build_clone_script(addons)],
            "volumeMounts": volume_mounts
        })

    job_body = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": job_metadata,
        "spec": {
            "ttlSecondsAfterFinished": 300,  # Clean up 5 min after completion
            "backoffLimit": 3,
            "template": {
                "metadata": {
                    "labels": {
                        "odoo.simstech.cloud/cluster": name,
                        "odoo.simstech.cloud/component": "db-init"
                    }
                },
                "spec": {
                    "restartPolicy": "OnFailure",
                    "initContainers": init_containers if init_containers else None,
                    "containers": [{
                        "name": "db-init",
                        "image": odoo_image,
                        "command": ["/bin/bash", "-c"],
                        "args": [init_script],
                        "env": [
                            {
                                "name": "DB_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": db_secret,
                                        "key": "password"
                                    }
                                }
                            },
                            {
                                "name": "ADMIN_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": admin_secret_name,
                                        "key": "admin-password"
                                    }
                                }
                            }
                        ],
                        "volumeMounts": volume_mounts if volume_mounts else None
                    }],
                    "volumes": volumes if volumes else None
                }
            }
        }
    }

    # Remove None values from pod spec
    pod_spec = job_body["spec"]["template"]["spec"]
    if pod_spec.get("initContainers") is None:
        del pod_spec["initContainers"]
    if pod_spec.get("volumes") is None:
        del pod_spec["volumes"]
    if pod_spec["containers"][0].get("volumeMounts") is None:
        del pod_spec["containers"][0]["volumeMounts"]

    try:
        batch_api.create_namespaced_job(namespace=namespace, body=job_body)
    except ApiException as e:
        if e.status == 409:
            # Job already exists - check if it's completed
            pass
        else:
            raise

    return job_name


async def check_db_init_job_status(namespace: str, name: str) -> dict:
    """Check the status of the DB init job.

    Returns:
        {
            "exists": bool,
            "completed": bool,
            "failed": bool,
            "message": str
        }
    """
    batch_api = client.BatchV1Api()
    job_name = f"{name}-db-init"

    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        status = job.status

        if status.succeeded and status.succeeded > 0:
            return {
                "exists": True,
                "completed": True,
                "failed": False,
                "message": "Database initialization completed"
            }
        elif status.failed and status.failed >= 3:
            return {
                "exists": True,
                "completed": False,
                "failed": True,
                "message": "Database initialization failed after 3 attempts"
            }
        else:
            return {
                "exists": True,
                "completed": False,
                "failed": False,
                "message": "Database initialization in progress"
            }

    except ApiException as e:
        if e.status == 404:
            return {
                "exists": False,
                "completed": False,
                "failed": False,
                "message": "Database initialization job not found"
            }
        raise


async def delete_db_init_job(namespace: str, name: str) -> None:
    """Delete the DB init job."""
    batch_api = client.BatchV1Api()
    job_name = f"{name}-db-init"

    try:
        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background"
        )
    except ApiException as e:
        if e.status != 404:
            raise

