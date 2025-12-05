"""
Simstech Odoo Operator - Main Entry Point

Three-Phase Architecture:
- Phase 1: DB Init Job - Initializes database once before pods start
- Phase 2: Scalable Pods - Odoo deployment can scale freely
- Phase 3: Module Sync - Synchronizes modules across pods

This operator manages OdooCluster custom resources, creating:
- CloudNative-PG PostgreSQL cluster
- Odoo deployment with filestore
- Optional: Valkey (Redis) for caching
- Optional: Metabase for BI
- Optional: Tailscale sidecars for private access
- Optional: Cloudflare Tunnel for public access with custom domains
"""

import kopf
import logging
from datetime import datetime, timezone

from handlers.database import create_database, delete_database, check_database_ready
from handlers.odoo import create_odoo, delete_odoo
from handlers.valkey import create_valkey, delete_valkey
from handlers.metabase import create_metabase, delete_metabase
from handlers.db_init import (
    create_db_init_job,
    check_db_init_job_status,
    delete_db_init_job
)
from handlers.module_sync import sync_modules_for_cluster
from handlers.cloudflare import (
    create_cloudflare_tunnel,
    delete_cloudflare_tunnel,
    check_cloudflare_tunnel_ready
)

logger = logging.getLogger(__name__)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings."""
    settings.posting.level = logging.INFO
    settings.watching.server_timeout = 60
    logger.info("Simstech Odoo Operator starting (Three-Phase Architecture)...")


def build_owner_reference(name: str, uid: str) -> dict:
    """Build ownerReference for child resources to show in ArgoCD."""
    return {
        "apiVersion": "odoo.simstech.cloud/v1alpha1",
        "kind": "OdooCluster",
        "name": name,
        "uid": uid,
        "controller": True,
        "blockOwnerDeletion": True
    }


@kopf.on.create('odoo.simstech.cloud', 'v1alpha1', 'odooclusters')
async def on_create(spec, name, namespace, logger, patch, meta, **kwargs):
    """Handle OdooCluster creation.

    Phase 1: Create infrastructure (DB, Valkey, Metabase)
    Phase 2: Create DB Init Job, wait for completion
    Phase 3: Create Odoo Deployment (scalable)
    """
    logger.info(f"Creating OdooCluster: {name} in namespace: {namespace}")

    # Build owner reference for ArgoCD visibility
    owner_ref = build_owner_reference(name, meta.get('uid'))

    # Update status to Creating
    patch.status['phase'] = 'Creating'
    patch.status['message'] = 'Initializing cluster resources'
    patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

    cluster_namespace = namespace
    patch.status['namespace'] = cluster_namespace

    try:
        # =====================================================================
        # INFRASTRUCTURE SETUP
        # =====================================================================

        # 1. Create PostgreSQL cluster
        logger.info(f"Creating PostgreSQL cluster for: {name}")
        db_spec = spec.get('database', {})
        backup_spec = db_spec.get('backup', {})
        await create_database(
            namespace=cluster_namespace,
            name=name,
            storage=db_spec.get('storage', '20Gi'),
            instances=db_spec.get('instances', 1),
            resources=db_spec.get('resources', {}),
            backup=backup_spec if backup_spec.get('enabled') else None,
            owner_ref=owner_ref
        )
        patch.status['database'] = {
            'host': f"{name}-db-rw.{cluster_namespace}.svc.cluster.local",
            'ready': False
        }

        # 2. Create Valkey if enabled
        addons = spec.get('addons', {})
        valkey_spec = addons.get('valkey', {})
        if valkey_spec.get('enabled'):
            logger.info(f"Creating Valkey for: {name}")
            await create_valkey(
                namespace=cluster_namespace,
                name=name,
                storage=valkey_spec.get('storage', '1Gi'),
                resources=valkey_spec.get('resources', {}),
                owner_ref=owner_ref
            )

        # 3. Create Metabase if enabled
        bi_spec = addons.get('bi', {})
        if bi_spec.get('enabled'):
            logger.info(f"Creating Metabase BI for: {name}")
            networking = spec.get('networking', {})
            tailscale = networking.get('tailscale', {})
            bi_tailscale = tailscale.get('bi', {})

            await create_metabase(
                namespace=cluster_namespace,
                name=name,
                storage=bi_spec.get('storage', '5Gi'),
                resources=bi_spec.get('resources', {}),
                tailscale=bi_tailscale if bi_tailscale.get('enabled') else None,
                tailscale_auth_secret=tailscale.get('authSecretName', 'tailscale-auth'),
                owner_ref=owner_ref
            )

            if bi_tailscale.get('enabled'):
                hostname = bi_tailscale.get('hostname', 'bi')
                patch.status.setdefault('endpoints', {})['bi'] = f"https://{hostname}.tail108d23.ts.net"

        # =====================================================================
        # PHASE 1: DATABASE INITIALIZATION JOB
        # =====================================================================
        logger.info(f"Phase 1: Creating DB init job for: {name}")
        odoo_spec = spec.get('odoo', {})
        version = odoo_spec.get('version', '17.0')
        odoo_image = odoo_spec.get('image') or f"odoo:{version}"

        await create_db_init_job(
            namespace=cluster_namespace,
            name=name,
            odoo_image=odoo_image,
            db_host=f"{name}-db-rw",
            db_secret=f"{name}-db-app",
            admin_secret_name=f"{name}-odoo-admin",
            owner_ref=owner_ref
        )

        patch.status['dbInit'] = {
            'jobName': f"{name}-db-init",
            'status': 'running'
        }

        # =====================================================================
        # PHASE 2: ODOO DEPLOYMENT (Scalable)
        # =====================================================================
        logger.info(f"Phase 2: Creating Odoo deployment for: {name}")
        networking = spec.get('networking', {})
        tailscale = networking.get('tailscale', {})
        odoo_tailscale = tailscale.get('odoo', {})

        await create_odoo(
            namespace=cluster_namespace,
            name=name,
            version=version,
            image=odoo_spec.get('image'),
            replicas=odoo_spec.get('replicas', 1),
            storage=odoo_spec.get('storage', '10Gi'),
            resources=odoo_spec.get('resources', {}),
            addons=odoo_spec.get('addons', []),
            db_host=f"{name}-db-rw",
            db_secret=f"{name}-db-app",
            valkey_enabled=valkey_spec.get('enabled', False),
            valkey_host=f"{name}-valkey" if valkey_spec.get('enabled') else None,
            tailscale=odoo_tailscale if odoo_tailscale.get('enabled') else None,
            tailscale_auth_secret=tailscale.get('authSecretName', 'tailscale-auth'),
            owner_ref=owner_ref
        )

        if odoo_tailscale.get('enabled'):
            hostname = odoo_tailscale.get('hostname', 'odoo')
            patch.status.setdefault('endpoints', {})['odoo'] = f"https://{hostname}.tail108d23.ts.net"

        # =====================================================================
        # CLOUDFLARE TUNNEL (Public Access)
        # Uses config file approach to support multiple hostnames
        # =====================================================================
        cloudflare = networking.get('cloudflare', {})
        if cloudflare.get('enabled'):
            logger.info(f"Creating Cloudflare Tunnel for: {name}")
            cf_odoo = cloudflare.get('odoo', {})
            cf_bi = cloudflare.get('bi', {})

            await create_cloudflare_tunnel(
                namespace=cluster_namespace,
                name=name,
                tunnel_secret_name=cloudflare.get('tunnelSecretName', 'cloudflare-tunnel'),
                odoo_hostname=cf_odoo.get('hostname'),
                metabase_hostname=cf_bi.get('hostname'),
                metabase_enabled=bi_spec.get('enabled', False),
                replicas=cloudflare.get('replicas', 1),
                owner_ref=owner_ref
            )

            # Set public endpoints in status (from CRD spec for display)
            if cf_odoo.get('hostname'):
                patch.status.setdefault('endpoints', {})['odooPublic'] = f"https://{cf_odoo.get('hostname')}"
            if cf_bi.get('hostname') and bi_spec.get('enabled'):
                patch.status.setdefault('endpoints', {})['biPublic'] = f"https://{cf_bi.get('hostname')}"

            patch.status['cloudflare'] = {
                'ready': False
            }

        # Note: Phase 3 (Module Sync) runs via timer after pods are ready

        patch.status['phase'] = 'Initializing'
        patch.status['message'] = 'Waiting for database initialization to complete'
        patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

        logger.info(f"OdooCluster {name} infrastructure created, waiting for DB init")

    except Exception as e:
        logger.error(f"Error creating OdooCluster {name}: {e}")
        patch.status['phase'] = 'Error'
        patch.status['message'] = str(e)
        patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        raise kopf.PermanentError(str(e))


@kopf.on.update('odoo.simstech.cloud', 'v1alpha1', 'odooclusters')
async def on_update(spec, name, namespace, logger, patch, meta, old, new, **kwargs):
    """Handle OdooCluster updates."""
    logger.info(f"Updating OdooCluster: {name} in namespace: {namespace}")

    patch.status['phase'] = 'Updating'
    patch.status['message'] = 'Updating cluster resources'
    patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

    # Re-run creation logic (handlers are idempotent)
    await on_create(spec, name, namespace, logger, patch, meta, **kwargs)


@kopf.on.delete('odoo.simstech.cloud', 'v1alpha1', 'odooclusters')
async def on_delete(spec, name, namespace, logger, **kwargs):
    """Handle OdooCluster deletion."""
    logger.info(f"Deleting OdooCluster: {name} in namespace: {namespace}")

    cluster_namespace = namespace

    try:
        # Delete in reverse order
        addons = spec.get('addons', {})
        networking = spec.get('networking', {})

        # Delete Cloudflare Tunnel
        if networking.get('cloudflare', {}).get('enabled'):
            await delete_cloudflare_tunnel(cluster_namespace, name)

        # Delete Metabase
        if addons.get('bi', {}).get('enabled'):
            await delete_metabase(cluster_namespace, name)

        # Delete Odoo
        await delete_odoo(cluster_namespace, name)

        # Delete DB Init Job
        await delete_db_init_job(cluster_namespace, name)

        # Delete Valkey
        if addons.get('valkey', {}).get('enabled'):
            await delete_valkey(cluster_namespace, name)

        # Delete Database
        await delete_database(cluster_namespace, name)

        logger.info(f"OdooCluster {name} deleted successfully")

    except Exception as e:
        logger.error(f"Error deleting OdooCluster {name}: {e}")
        raise


@kopf.timer('odoo.simstech.cloud', 'v1alpha1', 'odooclusters', interval=15.0)
async def reconcile_status(spec, name, namespace, logger, patch, status, **kwargs):
    """Periodic reconciliation loop.

    - Checks DB init job status
    - Monitors database readiness
    - Phase 3: Syncs modules across pods
    """
    cluster_namespace = namespace
    current_phase = status.get('phase', 'Unknown')

    try:
        # =====================================================================
        # CHECK DB INIT JOB STATUS
        # =====================================================================
        if current_phase == 'Initializing':
            job_status = await check_db_init_job_status(cluster_namespace, name)

            if job_status['completed']:
                logger.info(f"DB init job completed for: {name}")
                patch.status['dbInit'] = {
                    'jobName': f"{name}-db-init",
                    'status': 'completed'
                }
                patch.status['phase'] = 'Ready'
                patch.status['message'] = 'Database initialized, cluster ready'
                patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

            elif job_status['failed']:
                logger.error(f"DB init job failed for: {name}")
                patch.status['dbInit'] = {
                    'jobName': f"{name}-db-init",
                    'status': 'failed'
                }
                patch.status['phase'] = 'Error'
                patch.status['message'] = 'Database initialization failed'
                patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

        # =====================================================================
        # CHECK DATABASE READINESS
        # =====================================================================
        if current_phase in ['Ready', 'Initializing']:
            db_ready = await check_database_ready(cluster_namespace, name)

            if status.get('database', {}).get('ready') != db_ready:
                patch.status['database'] = {
                    'host': f"{name}-db-rw.{cluster_namespace}.svc.cluster.local",
                    'ready': db_ready
                }
                patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

        # =====================================================================
        # CHECK CLOUDFLARE TUNNEL READINESS
        # =====================================================================
        networking = spec.get('networking', {})
        cloudflare = networking.get('cloudflare', {})
        if cloudflare.get('enabled') and current_phase in ['Ready', 'Initializing']:
            cf_ready = await check_cloudflare_tunnel_ready(cluster_namespace, name)

            if status.get('cloudflare', {}).get('ready') != cf_ready:
                patch.status['cloudflare'] = {
                    'ready': cf_ready
                }
                patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

        # =====================================================================
        # PHASE 3: MODULE SYNC
        # =====================================================================
        if current_phase == 'Ready':
            odoo_spec = spec.get('odoo', {})
            odoo_addons = odoo_spec.get('addons', [])

            if odoo_addons:
                logger.debug(f"Phase 3: Checking module sync for: {name}")
                module_status = await sync_modules_for_cluster(
                    namespace=cluster_namespace,
                    name=name,
                    addons=odoo_addons,
                    db_host=f"{name}-db-rw",
                    db_secret=f"{name}-db-app"
                )

                # Update module status
                patch.status['modules'] = module_status
                patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        logger.warning(f"Error in reconcile loop for {name}: {e}")
