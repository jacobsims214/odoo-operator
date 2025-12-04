# Simstech Odoo Operator

A Kubernetes operator that manages multi-tenant Odoo deployments. Each `OdooCluster` custom resource provisions a complete, isolated Odoo environment.

## Quick Start

```bash
# Install the operator
helm install odoo-operator ./charts/operator \
  --namespace simstech-odoo-operator \
  --create-namespace

# Deploy a customer's Odoo
helm install acme ./charts/odoocluster \
  --namespace odoo-acme \
  --create-namespace \
  --set odoo.version="17"
```

## Features

- **Automated PostgreSQL** - Creates CloudNative-PG clusters with automatic backups
- **Odoo Deployment** - Configurable version, resources, and storage
- **Valkey Cache** (optional) - Redis-compatible cache for sessions/queues
- **Metabase BI** (optional) - Business intelligence dashboards connected to Odoo data
- **Tailscale Integration** (optional) - Secure access without exposing to the internet

## Prerequisites

- Kubernetes 1.28+
- [CloudNative-PG Operator](https://cloudnative-pg.io/) installed
- [Tailscale](https://tailscale.com/) account (if using Tailscale networking)

## Installation

### 1. Install the CRD

```bash
kubectl apply -f crds/odoocluster-crd.yaml
```

### 2. Deploy the Operator

```bash
kubectl apply -f config/rbac.yaml
kubectl apply -f config/manager.yaml
```

### 3. Verify Installation

```bash
kubectl get pods -n simstech-odoo-operator
kubectl get crd odooclusters.simstech-odoo
```

## Usage

### Create an OdooCluster

#### Minimal Example

```yaml
apiVersion: simstech-odoo/v1alpha1
kind: OdooCluster
metadata:
  name: demo
spec:
  odoo:
    version: "17.0"
    storage: "5Gi"
  database:
    storage: "10Gi"
```

#### Full Example with All Features

```yaml
apiVersion: simstech-odoo/v1alpha1
kind: OdooCluster
metadata:
  name: acme-corp
spec:
  odoo:
    version: "17.0"
    replicas: 1
    storage: "10Gi"
    resources:
      requests:
        cpu: "500m"
        memory: "1Gi"
      limits:
        cpu: "2"
        memory: "4Gi"
    
    # Git repositories containing Odoo addons
    addons:
      - name: "oca-web"
        repo: "https://github.com/OCA/web.git"
        branch: "17.0"
      - name: "custom-addons"
        repo: "git@github.com:acme/odoo-addons.git"
        branch: "main"
        deployKeySecret: "github-deploy-key"
  
  database:
    storage: "20Gi"
    instances: 1
    backup:
      enabled: true
      schedule: "0 2 * * *"
      retentionPolicy: "30d"
      s3:
        endpoint: "https://minio.example.com"
        bucket: "odoo-backups"
        secretName: "backup-s3-creds"
  
  addons:
    valkey:
      enabled: true
      storage: "1Gi"
    bi:
      enabled: true
      tool: "metabase"
      storage: "5Gi"
  
  networking:
    tailscale:
      authSecretName: "tailscale-auth"
      odoo:
        enabled: true
        hostname: "acme-odoo"
        funnel: true
        tags: "tag:odoo-web"
      bi:
        enabled: true
        hostname: "acme-bi"
        funnel: false
        tags: "tag:odoo-bi"
```

### Pre-requisite Secrets

Before creating an OdooCluster with Tailscale, backups, or private Git repos, create the required secrets:

```bash
# Create the namespace first
kubectl create namespace odoo-acme-corp

# Tailscale auth (get key from https://login.tailscale.com/admin/settings/keys)
kubectl -n odoo-acme-corp create secret generic tailscale-auth \
  --from-literal=TS_AUTHKEY="tskey-auth-xxx"

# S3/Minio backup credentials (if using backups)
kubectl -n odoo-acme-corp create secret generic backup-s3-creds \
  --from-literal=ACCESS_KEY_ID="xxx" \
  --from-literal=SECRET_ACCESS_KEY="xxx"
```

### Git Deploy Keys (for Private Addon Repos)

For private Git repositories containing Odoo addons, create deploy key secrets:

```bash
# Option 1: Use the helper script (generates key and creates secret)
./scripts/create-deploy-key.sh acme-corp github-deploy-key

# Option 2: Manual creation
# Generate a new ED25519 deploy key
ssh-keygen -t ed25519 -C "odoo-deploy-key" -f deploy-key -N ""

# Add the PUBLIC key (deploy-key.pub) to your Git repo:
#   GitHub: Settings → Deploy keys → Add deploy key
#   GitLab: Settings → Repository → Deploy keys

# Create the Kubernetes secret with the PRIVATE key
kubectl -n odoo-acme-corp create secret generic github-deploy-key \
  --from-file=ssh-privatekey=deploy-key
```

Then reference in your OdooCluster:

```yaml
spec:
  odoo:
    addons:
      - name: "my-addons"
        repo: "git@github.com:company/odoo-addons.git"
        branch: "17.0"
        deployKeySecret: "github-deploy-key"
```

### Check Status

```bash
# List all OdooClusters
kubectl get odooclusters

# Get detailed status
kubectl describe odoocluster acme-corp

# Check created resources
kubectl get all -n odoo-acme-corp
```

### Delete an OdooCluster

```bash
kubectl delete odoocluster acme-corp
```

This will delete the namespace and all resources within it.

## Architecture

When you create an OdooCluster, the operator provisions:

```
OdooCluster: acme-corp
└── Namespace: odoo-acme-corp
    ├── CloudNative-PG Cluster (acme-corp-db)
    │   └── ScheduledBackup (if backup enabled)
    ├── Odoo Deployment (acme-corp-odoo)
    │   ├── Service
    │   ├── PVC (filestore)
    │   ├── ConfigMap (odoo.conf)
    │   └── Tailscale sidecar (if enabled)
    ├── Valkey StatefulSet (acme-corp-valkey) [optional]
    │   └── Service
    └── Metabase Deployment (acme-corp-metabase) [optional]
        ├── Service
        ├── PVC
        └── Tailscale sidecar (if enabled)
```

## Tailscale ACLs

If using Tailscale Funnel for public access, add these to your Tailscale ACL:

```json
{
  "tagOwners": {
    "tag:odoo-web": ["autogroup:admin"],
    "tag:odoo-bi": ["autogroup:admin"]
  },
  "nodeAttrs": [
    {
      "target": ["tag:odoo-web"],
      "attr": ["funnel"]
    }
  ]
}
```

## Development

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (uses your kubeconfig)
kopf run src/main.py --verbose
```

### Build Docker Image

The image is automatically built and pushed to GHCR on tag releases:

```bash
# Create a release tag
git tag v0.1.0
git push origin v0.1.0
```

The GitHub Action will build and push:
- `ghcr.io/jacobsims214/odoo-operator:0.1.0`
- `ghcr.io/jacobsims214/odoo-operator:0.1`
- `ghcr.io/jacobsims214/odoo-operator:latest`

#### Manual Build

```bash
docker build -t ghcr.io/jacobsims214/odoo-operator:latest .
docker push ghcr.io/jacobsims214/odoo-operator:latest
```

### Making the Package Public

After the first push, make the package public in GitHub:

1. Go to your GitHub profile → Packages
2. Find `odoo-operator`
3. Package settings → Change visibility → Public

## Troubleshooting

### Operator Logs

```bash
kubectl logs -n simstech-odoo-operator deployment/simstech-odoo-operator -f
```

### Database Not Ready

Check CloudNative-PG cluster status:
```bash
kubectl get clusters -n odoo-acme-corp
kubectl describe cluster acme-corp-db -n odoo-acme-corp
```

### Odoo Pod Failing

Check logs:
```bash
kubectl logs -n odoo-acme-corp deployment/acme-corp-odoo -c odoo
```

### Tailscale Not Connecting

Check sidecar logs:
```bash
kubectl logs -n odoo-acme-corp deployment/acme-corp-odoo -c tailscale
```

Verify the auth key is valid and has the correct tags.

## License

MIT

