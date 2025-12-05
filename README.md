# Simstech Odoo Operator

A Kubernetes operator that manages multi-tenant Odoo deployments. Each `OdooCluster` custom resource provisions a complete, isolated Odoo environment.

## Quick Start

```bash
# Install the operator
helm install odoo-operator ./charts/operator \
  --namespace odoo.simstech.cloud-operator \
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
- **Tailscale Integration** (optional) - Private tailnet access or Tailscale Funnel
- **Cloudflare Tunnel** (optional) - Public internet access with custom domains

## Prerequisites

- Kubernetes 1.28+
- [CloudNative-PG Operator](https://cloudnative-pg.io/) installed
- [Tailscale](https://tailscale.com/) account (if using Tailscale networking)
- [Cloudflare](https://cloudflare.com/) account (if using Cloudflare Tunnel)

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
kubectl get pods -n odoo.simstech.cloud-operator
kubectl get crd odooclusters.odoo.simstech.cloud
```

## Usage

### Create an OdooCluster

#### Minimal Example

```yaml
apiVersion: odoo.simstech.cloud/v1alpha1
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
apiVersion: odoo.simstech.cloud/v1alpha1
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

## Cloudflare Tunnel Setup

Cloudflare Tunnel allows you to expose your Odoo and Metabase services to the public internet with your custom domain, without exposing any ports or using a public IP.

### Use Cases

| Access Type | Solution | Domain Example |
|-------------|----------|----------------|
| Internal/dev access | Tailscale (tailnet) | `odoo.tail123.ts.net` |
| Quick public share | Tailscale Funnel | `odoo.tail123.ts.net` |
| **Production with custom domain** | **Cloudflare Tunnel** | `www.simstech.cloud` |

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Internet Users                                                      │
│  www.simstech.cloud ──► Cloudflare Edge ──┐                        │
│  data.simstech.cloud ─►                   │                        │
└───────────────────────────────────────────┼─────────────────────────┘
                                            │
                                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Kubernetes Cluster                                                  │
│  ┌─────────────────────┐                                            │
│  │ cloudflared pod     │──► simstech-odoo:8069 ──► Odoo pods       │
│  │ (outbound only)     │──► simstech-metabase:3000 ──► Metabase    │
│  └─────────────────────┘                                            │
└─────────────────────────────────────────────────────────────────────┘
```

### Step 1: Install cloudflared CLI

```bash
# macOS
brew install cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```

### Step 2: Login to Cloudflare

```bash
cloudflared tunnel login
```

This opens your browser to authenticate with Cloudflare. After logging in, a certificate is saved to `~/.cloudflared/cert.pem`.

### Step 3: Create a Named Tunnel

```bash
cloudflared tunnel create simstech-odoo
```

**Output:**
```
Tunnel credentials written to /Users/you/.cloudflared/4d33f088-65c2-405e-a699-2224dceb5863.json
Created tunnel simstech-odoo with id 4d33f088-65c2-405e-a699-2224dceb5863
```

**Save these values:**
- **Tunnel ID**: `4d33f088-65c2-405e-a699-2224dceb5863`
- **Credentials file**: `~/.cloudflared/4d33f088-65c2-405e-a699-2224dceb5863.json`

### Step 4: Create the Kubernetes Secret

```bash
./scripts/create-cloudflare-tunnel-secret.sh \
  simstech-odoo \
  cloudflare-tunnel \
  "4d33f088-65c2-405e-a699-2224dceb5863" \
  ~/.cloudflared/4d33f088-65c2-405e-a699-2224dceb5863.json
```

This creates a secret with:
- `credentials.json` - Tunnel authentication credentials
- `TUNNEL_ID` - The tunnel UUID

### Step 5: Add DNS Records in Cloudflare

Go to your Cloudflare Dashboard → DNS → Records and add CNAME records:

| Type | Name | Target | Proxy |
|------|------|--------|-------|
| CNAME | `www` | `4d33f088-65c2-405e-a699-2224dceb5863.cfargotunnel.com` | Proxied ☁️ |
| CNAME | `data` | `4d33f088-65c2-405e-a699-2224dceb5863.cfargotunnel.com` | Proxied ☁️ |

### Step 6: Configure OdooCluster

Add the cloudflare section to your OdooCluster spec:

```yaml
apiVersion: odoo.simstech.cloud/v1alpha1
kind: OdooCluster
metadata:
  name: simstech
  namespace: simstech-odoo
spec:
  odoo:
    version: "19"
    replicas: 3
  database:
    storage: "100Gi"
  addons:
    bi:
      enabled: true
      tool: "metabase"
  networking:
    cloudflare:
      enabled: true
      tunnelSecretName: "cloudflare-tunnel"
      replicas: 1  # Increase to 2 for HA
      odoo:
        hostname: "www.simstech.cloud"
      bi:
        hostname: "data.simstech.cloud"
```

### Step 7: Verify the Tunnel

After deploying, check the tunnel pod:

```bash
# Check the cloudflared pod is running
kubectl get pods -n simstech-odoo -l odoo.simstech.cloud/component=cloudflare-tunnel

# Check logs
kubectl logs -n simstech-odoo -l odoo.simstech.cloud/component=cloudflare-tunnel

# Check the generated config
kubectl get configmap simstech-cloudflare-tunnel-config -n simstech-odoo -o yaml
```

The operator generates a config like this:

```yaml
tunnel: 4d33f088-65c2-405e-a699-2224dceb5863
credentials-file: /etc/cloudflared/credentials.json
ingress:
  - hostname: www.simstech.cloud
    service: http://simstech-odoo:8069
  - hostname: data.simstech.cloud
    service: http://simstech-metabase:3000
  - service: http_status:404
```

### Step 8: Test Access

```bash
# Test Odoo
curl -I https://www.simstech.cloud

# Test Metabase  
curl -I https://data.simstech.cloud
```

### Troubleshooting Cloudflare Tunnel

**Tunnel not connecting:**
```bash
# Check cloudflared logs
kubectl logs -n simstech-odoo deployment/simstech-cloudflare-tunnel

# Verify secret has correct data
kubectl get secret cloudflare-tunnel -n simstech-odoo -o jsonpath='{.data}' | base64 -d
```

**DNS not resolving:**
- Ensure CNAME records are proxied (orange cloud)
- Wait 5 minutes for DNS propagation
- Check the tunnel ID matches in both DNS and secret

**502 Bad Gateway:**
- The Odoo/Metabase pods may not be ready
- Check service endpoints: `kubectl get endpoints -n simstech-odoo`

### Cloudflare vs Tailscale Comparison

| Feature | Tailscale | Cloudflare Tunnel |
|---------|-----------|-------------------|
| Access type | Private tailnet | Public internet |
| Custom domain | No (*.ts.net only) | Yes |
| Authentication | Tailscale identity | Cloudflare Access (optional) |
| Setup complexity | Lower | Higher |
| Cost | Free tier available | Free tier available |
| Best for | Internal/dev | Production public access |

You can use **both** simultaneously:
- Tailscale for internal admin access
- Cloudflare for public customer access

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
kubectl logs -n odoo.simstech.cloud-operator deployment/odoo.simstech.cloud-operator -f
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

### Cloudflare Tunnel Not Working

Check the tunnel deployment:
```bash
# Check pod status
kubectl get pods -n simstech-odoo -l odoo.simstech.cloud/component=cloudflare-tunnel

# Check logs for connection errors
kubectl logs -n simstech-odoo -l odoo.simstech.cloud/component=cloudflare-tunnel --tail=50

# Verify secret exists and has required keys
kubectl get secret cloudflare-tunnel -n simstech-odoo -o jsonpath='{.data}' | jq -r 'keys'
# Should show: ["TUNNEL_ID", "credentials.json"]
```

Common issues:
- **ERR Invalid tunnel credentials**: The `credentials.json` doesn't match the tunnel ID
- **Connection refused**: Odoo/Metabase service not ready - check `kubectl get endpoints`
- **DNS error**: CNAME record not pointing to `<tunnel-id>.cfargotunnel.com`

## License

MIT

