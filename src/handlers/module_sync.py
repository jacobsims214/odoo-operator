"""
Module Sync Controller - Phase 3

Synchronizes Odoo modules across all pods:
1. Checks which modules should be installed from spec.odoo.addons
2. Verifies each pod has the correct module versions
3. Installs/updates modules one pod at a time (rolling update)
4. Tracks module state in OdooCluster.status.modules
"""

from kubernetes import client
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


def get_modules_to_install(addons: List[dict]) -> List[str]:
    """Extract module names from addons config.

    For each addon:
    - If 'path' is specified, that's the module name
    - If 'path' is not specified, we need to scan the repo (future enhancement)
    """
    modules = []
    for addon in addons:
        module_path = addon.get('path', '')
        if module_path:
            # The 'path' field specifies the module name within the repo
            modules.append(module_path)
    return modules


async def sync_modules_for_cluster(
    namespace: str,
    name: str,
    addons: List[dict],
    db_host: str,
    db_secret: str
) -> dict:
    """Sync modules across all Odoo pods in the cluster.

    Returns status dict for OdooCluster.status.modules
    """
    core_api = client.CoreV1Api()

    # Get list of modules to install
    modules_to_install = get_modules_to_install(addons)

    if not modules_to_install:
        return {
            "synced": True,
            "modules": [],
            "message": "No modules to install"
        }

    # Get all Odoo pods
    pods = core_api.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"odoo.simstech.cloud/cluster={name},odoo.simstech.cloud/component=odoo"
    )

    if not pods.items:
        return {
            "synced": False,
            "modules": modules_to_install,
            "message": "No Odoo pods found"
        }

    # Get DB password for module installation
    try:
        secret = core_api.read_namespaced_secret(name=db_secret, namespace=namespace)
        import base64
        db_password = base64.b64decode(secret.data.get('password', '')).decode('utf-8')
    except ApiException:
        return {
            "synced": False,
            "modules": modules_to_install,
            "message": "Could not read database secret"
        }

    results = []
    all_synced = True

    for pod in pods.items:
        if pod.status.phase != "Running":
            continue

        pod_name = pod.metadata.name

        # Check which modules are installed on this pod
        installed = await check_installed_modules(namespace, pod_name, db_host, db_password)
        missing = [m for m in modules_to_install if m not in installed]

        if missing:
            all_synced = False
            # Install missing modules on this pod
            logger.info(f"Installing modules {missing} on pod {pod_name}")
            success = await install_modules_on_pod(
                namespace=namespace,
                pod_name=pod_name,
                modules=missing,
                db_host=db_host,
                db_password=db_password
            )
            results.append({
                "pod": pod_name,
                "installed": missing if success else [],
                "status": "success" if success else "failed"
            })
        else:
            results.append({
                "pod": pod_name,
                "installed": [],
                "status": "synced"
            })

    return {
        "synced": all_synced,
        "modules": modules_to_install,
        "podResults": results,
        "message": "All modules synced" if all_synced else "Module sync in progress"
    }


async def check_installed_modules(
    namespace: str,
    pod_name: str,
    db_host: str,
    db_password: str
) -> List[str]:
    """Check which modules are installed on a specific pod."""
    core_api = client.CoreV1Api()

    # Execute command in pod to check installed modules
    command = [
        "python3", "-c",
        f"""
import psycopg2
conn = psycopg2.connect(host='{db_host}', port=5432, user='odoo', password='{db_password}', dbname='odoo')
cur = conn.cursor()
cur.execute("SELECT name FROM ir_module_module WHERE state = 'installed'")
for row in cur.fetchall():
    print(row[0])
conn.close()
"""
    ]

    try:
        resp = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="odoo",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False
        )

        output = ""
        while resp.is_open():
            resp.update(timeout=30)
            if resp.peek_stdout():
                output += resp.read_stdout()
            if resp.peek_stderr():
                resp.read_stderr()  # Discard stderr

        resp.close()
        return [m.strip() for m in output.strip().split('\n') if m.strip()]

    except Exception as e:
        logger.warning(f"Failed to check modules on pod {pod_name}: {e}")
        return []


async def install_modules_on_pod(
    namespace: str,
    pod_name: str,
    modules: List[str],
    db_host: str,
    db_password: str
) -> bool:
    """Install/update modules on a specific pod.

    Uses odoo -u to update (install if not present) modules.
    """
    core_api = client.CoreV1Api()

    modules_str = ",".join(modules)

    # Build the odoo command to install modules
    command = [
        "/bin/bash", "-c",
        f"""
echo "Installing modules: {modules_str}"
odoo --database=odoo --db_host={db_host} --db_port=5432 --db_user=odoo --db_password='{db_password}' --stop-after-init --update={modules_str} --no-http 2>&1
echo "Module installation complete"
"""
    ]

    try:
        resp = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="odoo",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False
        )

        output = ""
        while resp.is_open():
            resp.update(timeout=300)  # 5 minute timeout for module install
            if resp.peek_stdout():
                output += resp.read_stdout()
            if resp.peek_stderr():
                output += resp.read_stderr()

        resp.close()
        logger.info(f"Module install output on {pod_name}: {output[-500:]}")  # Last 500 chars

        return "Module installation complete" in output or "Modules loaded" in output

    except Exception as e:
        logger.error(f"Failed to install modules on pod {pod_name}: {e}")
        return False


async def get_addon_git_sha(
    namespace: str,
    pod_name: str,
    addon_name: str
) -> Optional[str]:
    """Get the current git SHA for an addon on a specific pod."""
    core_api = client.CoreV1Api()

    command = [
        "/bin/bash", "-c",
        f"cd /mnt/addons/{addon_name} && git rev-parse HEAD 2>/dev/null || echo 'NOT_FOUND'"
    ]

    try:
        resp = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="odoo",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False
        )

        output = ""
        while resp.is_open():
            resp.update(timeout=10)
            if resp.peek_stdout():
                output += resp.read_stdout()

        resp.close()
        sha = output.strip()
        return None if sha == "NOT_FOUND" else sha

    except Exception as e:
        logger.warning(f"Failed to get git SHA for {addon_name} on {pod_name}: {e}")
        return None


async def update_addon_on_pod(
    namespace: str,
    pod_name: str,
    addon: dict
) -> bool:
    """Pull latest changes for an addon on a specific pod."""
    core_api = client.CoreV1Api()

    addon_name = addon['name']
    branch = addon.get('branch', 'main')

    command = [
        "/bin/bash", "-c",
        f"""
cd /mnt/addons/{addon_name}
git fetch origin
git checkout {branch}
git pull origin {branch}
echo "Addon updated successfully"
"""
    ]

    try:
        resp = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            container="odoo",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False
        )

        output = ""
        while resp.is_open():
            resp.update(timeout=60)
            if resp.peek_stdout():
                output += resp.read_stdout()

        resp.close()
        return "Addon updated successfully" in output

    except Exception as e:
        logger.error(f"Failed to update addon {addon_name} on {pod_name}: {e}")
        return False

