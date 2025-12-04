"""
Simstech Odoo Operator - Main Entry Point

This operator manages OdooCluster custom resources, creating:
- Namespace for each cluster
- CloudNative-PG PostgreSQL cluster
- Odoo deployment with filestore
- Optional: Valkey (Redis) for caching
- Optional: Metabase for BI
- Optional: Tailscale sidecars for secure access
"""

import kopf
import logging
from datetime import datetime, timezone

from handlers.database import create_database, delete_database, check_database_ready
from handlers.odoo import create_odoo, delete_odoo
from handlers.valkey import create_valkey, delete_valkey
from handlers.metabase import create_metabase, delete_metabase

logger = logging.getLogger(__name__)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    """Configure operator settings."""
    settings.posting.level = logging.INFO
    settings.watching.server_timeout = 60
    logger.info("Simstech Odoo Operator starting...")


@kopf.on.create('simstech-odoo', 'v1alpha1', 'odooclusters')
async def on_create(spec, name, namespace, logger, patch, **kwargs):
    """Handle OdooCluster creation."""
    logger.info(f"Creating OdooCluster: {name} in namespace: {namespace}")
    
    # Update status to Creating
    patch.status['phase'] = 'Creating'
    patch.status['message'] = 'Initializing cluster resources'
    patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
    
    # Use the CR's namespace (user controls this via helm install -n)
    cluster_namespace = namespace
    patch.status['namespace'] = cluster_namespace
    
    try:
        # Namespace is managed by user/helm, not the operator
        logger.info(f"Using namespace: {cluster_namespace}")
        
        # 2. Create PostgreSQL cluster
        logger.info(f"Creating PostgreSQL cluster for: {name}")
        db_spec = spec.get('database', {})
        backup_spec = db_spec.get('backup', {})
        await create_database(
            namespace=cluster_namespace,
            name=name,
            storage=db_spec.get('storage', '20Gi'),
            instances=db_spec.get('instances', 1),
            resources=db_spec.get('resources', {}),
            backup=backup_spec if backup_spec.get('enabled') else None
        )
        patch.status['database'] = {
            'host': f"{name}-db-rw.{cluster_namespace}.svc.cluster.local",
            'ready': False
        }
        
        # 3. Create Valkey if enabled
        addons = spec.get('addons', {})
        valkey_spec = addons.get('valkey', {})
        if valkey_spec.get('enabled'):
            logger.info(f"Creating Valkey for: {name}")
            await create_valkey(
                namespace=cluster_namespace,
                name=name,
                storage=valkey_spec.get('storage', '1Gi'),
                resources=valkey_spec.get('resources', {})
            )
        
        # 4. Create Metabase if enabled
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
                tailscale_auth_secret=tailscale.get('authSecretName', 'tailscale-auth')
            )
            
            if bi_tailscale.get('enabled'):
                hostname = bi_tailscale.get('hostname', 'bi')
                patch.status.setdefault('endpoints', {})['bi'] = f"https://{hostname}.tail108d23.ts.net"
        
        # 5. Create Odoo deployment
        logger.info(f"Creating Odoo deployment for: {name}")
        odoo_spec = spec.get('odoo', {})
        networking = spec.get('networking', {})
        tailscale = networking.get('tailscale', {})
        odoo_tailscale = tailscale.get('odoo', {})
        
        await create_odoo(
            namespace=cluster_namespace,
            name=name,
            version=odoo_spec.get('version', '17.0'),
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
            tailscale_auth_secret=tailscale.get('authSecretName', 'tailscale-auth')
        )
        
        if odoo_tailscale.get('enabled'):
            hostname = odoo_tailscale.get('hostname', 'odoo')
            patch.status.setdefault('endpoints', {})['odoo'] = f"https://{hostname}.tail108d23.ts.net"
        
        patch.status['phase'] = 'Ready'
        patch.status['message'] = 'All resources created successfully'
        patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        
        logger.info(f"OdooCluster {name} created successfully")
        
    except Exception as e:
        logger.error(f"Error creating OdooCluster {name}: {e}")
        patch.status['phase'] = 'Error'
        patch.status['message'] = str(e)
        patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        raise kopf.PermanentError(str(e))


@kopf.on.update('simstech-odoo', 'v1alpha1', 'odooclusters')
async def on_update(spec, name, namespace, logger, patch, old, new, **kwargs):
    """Handle OdooCluster updates."""
    logger.info(f"Updating OdooCluster: {name} in namespace: {namespace}")
    
    patch.status['phase'] = 'Creating'
    patch.status['message'] = 'Updating cluster resources'
    patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
    
    # Re-run creation logic (handlers are idempotent)
    await on_create(spec, name, namespace, logger, patch, **kwargs)


@kopf.on.delete('simstech-odoo', 'v1alpha1', 'odooclusters')
async def on_delete(spec, name, namespace, logger, **kwargs):
    """Handle OdooCluster deletion."""
    logger.info(f"Deleting OdooCluster: {name} in namespace: {namespace}")
    
    # Use the CR's namespace
    cluster_namespace = namespace
    
    try:
        # Delete in reverse order
        addons = spec.get('addons', {})
        
        # Delete Metabase
        if addons.get('bi', {}).get('enabled'):
            await delete_metabase(cluster_namespace, name)
        
        # Delete Odoo
        await delete_odoo(cluster_namespace, name)
        
        # Delete Valkey
        if addons.get('valkey', {}).get('enabled'):
            await delete_valkey(cluster_namespace, name)
        
        # Delete Database
        await delete_database(cluster_namespace, name)
        
        # Note: Namespace is NOT deleted - managed by user/helm
        
        logger.info(f"OdooCluster {name} deleted successfully")
        
    except Exception as e:
        logger.error(f"Error deleting OdooCluster {name}: {e}")
        raise


@kopf.timer('simstech-odoo', 'v1alpha1', 'odooclusters', interval=30.0)
async def check_status(spec, name, namespace, logger, patch, status, **kwargs):
    """Periodic status check for database readiness."""
    if status.get('phase') != 'Ready':
        return
    
    cluster_namespace = namespace
    
    try:
        db_ready = await check_database_ready(cluster_namespace, name)
        
        if status.get('database', {}).get('ready') != db_ready:
            patch.status['database'] = {
                'host': f"{name}-db-rw.{cluster_namespace}.svc.cluster.local",
                'ready': db_ready
            }
            patch.status['lastUpdated'] = datetime.now(timezone.utc).isoformat()
            
    except Exception as e:
        logger.warning(f"Error checking status for {name}: {e}")

