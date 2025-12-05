"""
Database Initialization Job handler.

Phase 1: Creates a Kubernetes Job that initializes the Odoo database.
This runs ONCE before any Odoo pods are created, preventing race conditions.
"""

import secrets
import string
from kubernetes import client
from kubernetes.client.rest import ApiException
from typing import Optional


def generate_password(length: int = 32) -> str:
    """Generate a secure random password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def create_db_init_job(
    namespace: str,
    name: str,
    odoo_image: str,
    db_host: str,
    db_secret: str,
    admin_secret_name: str,
    owner_ref: Optional[dict] = None
) -> str:
    """Create a Job to initialize the Odoo database.

    Returns the Job name for status tracking.
    """
    core_api = client.CoreV1Api()
    batch_api = client.BatchV1Api()

    job_name = f"{name}-db-init"

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

    # Build the init script
    init_script = f"""
echo "=== Odoo Database Initialization Job ==="
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
    echo "Database already initialized, skipping init"
else
    echo "Database not initialized, running odoo -i base..."
    odoo --database=odoo --db_host={db_host} --db_port=5432 --db_user=odoo --db_password="$DB_PASSWORD" --stop-after-init --init=base --without-demo=True --no-http
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
                        ]
                    }]
                }
            }
        }
    }

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

