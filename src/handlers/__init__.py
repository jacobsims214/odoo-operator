# Simstech Odoo Operator Handlers
# Explicit imports to satisfy linters

from .cluster import get_cluster_labels, get_cluster_status
from .namespace import create_namespace, delete_namespace
from .database import create_database, delete_database, check_database_ready, create_scheduled_backup
from .odoo import create_odoo, delete_odoo
from .valkey import create_valkey, delete_valkey
from .metabase import create_metabase, delete_metabase
from .tailscale import (
    get_tailscale_sidecar,
    get_tailscale_volumes,
    get_serve_config,
    create_tailscale_resources,
    delete_tailscale_resources,
    get_tailscale_rbac,
)
from .db_init import (
    create_db_init_job,
    check_db_init_job_status,
    delete_db_init_job,
)
from .module_sync import (
    sync_modules_for_cluster,
    get_modules_to_install,
    check_installed_modules,
    install_modules_on_pod,
)

__all__ = [
    "get_cluster_labels",
    "get_cluster_status",
    "create_namespace",
    "delete_namespace",
    "create_database",
    "delete_database",
    "check_database_ready",
    "create_scheduled_backup",
    "create_odoo",
    "delete_odoo",
    "create_valkey",
    "delete_valkey",
    "create_metabase",
    "delete_metabase",
    "get_tailscale_sidecar",
    "get_tailscale_volumes",
    "get_serve_config",
    "create_tailscale_resources",
    "delete_tailscale_resources",
    "get_tailscale_rbac",
    "create_db_init_job",
    "check_db_init_job_status",
    "delete_db_init_job",
    "sync_modules_for_cluster",
    "get_modules_to_install",
    "check_installed_modules",
    "install_modules_on_pod",
]
