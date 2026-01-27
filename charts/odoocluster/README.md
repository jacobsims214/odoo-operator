# OdooCluster Helm Chart

Deploy an Odoo environment using the Simstech Odoo Operator.

## Prerequisites

- Simstech Odoo Operator installed in the cluster
- CloudNative-PG Operator installed

## Installation

### Basic Installation

```bash
helm install acme ./charts/odoocluster \
  --namespace odoo-acme \
  --create-namespace \
  --set odoo.version="17"
```

### With Tailscale Access

```bash
helm install acme ./charts/odoocluster \
  --namespace odoo-acme \
  --create-namespace \
  --set odoo.version="17" \
  --set networking.tailscale.odoo.enabled=true \
  --set secrets.tailscale.create=true \
  --set secrets.tailscale.authKey="tskey-auth-xxx"
```

### AWS EKS with ALB Ingress

Deploy production Odoo on AWS EKS with Application Load Balancer and ACM certificates:

```bash
# First, create the S3 backup credentials secret
kubectl create secret generic backup-s3-creds \
  --namespace odoo-acme \
  --from-literal=ACCESS_KEY_ID=AKIAXXXXXXXX \
  --from-literal=SECRET_ACCESS_KEY=xxxxx

# Deploy with ALB Ingress
helm install acme ./charts/odoocluster \
  --namespace odoo-acme \
  --create-namespace \
  -f values-aws.yaml \
  --set name=acme \
  --set networking.ingress.odoo.host=odoo.example.com \
  --set networking.ingress.bi.host=bi.example.com \
  --set 'networking.ingress.annotations.alb\.ingress\.kubernetes\.io/certificate-arn=arn:aws:acm:us-east-1:123456789:certificate/xxxxx' \
  --set database.backup.s3.bucket=my-odoo-backups
```

**Prerequisites for AWS EKS:**
- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) installed
- ACM certificate provisioned and validated for your domains
- EBS CSI driver for gp3 storage (database, Valkey)
- EFS CSI driver for RWX storage (recommended for multi-replica Odoo filestore)

See `values-aws.yaml` for a complete AWS production configuration.

### Full Production Setup (Self-hosted / Tailscale)

```bash
helm install acme ./charts/odoocluster \
  --namespace odoo-acme \
  --create-namespace \
  -f values-acme.yaml
```

Example `values-acme.yaml`:

```yaml
odoo:
  version: "19"
  replicas: 2
  storage: "20Gi"
  resources:
    requests:
      cpu: "1"
      memory: "2Gi"
    limits:
      cpu: "4"
      memory: "8Gi"
  addons:
    - name: "oca-web"
      repo: "https://github.com/OCA/web.git"
      branch: "19.0"
    - name: "custom"
      repo: "git@github.com:acme/odoo-addons.git"
      branch: "main"
      deployKeySecret: "github-deploy-key"

database:
  storage: "50Gi"
  instances: 2  # HA
  backup:
    enabled: true
    schedule: "0 2 * * *"
    s3:
      endpoint: "https://minio.example.com"
      bucket: "odoo-backups"

addons:
  valkey:
    enabled: true
  bi:
    enabled: true

networking:
  tailscale:
    odoo:
      enabled: true
      hostname: "acme-odoo"
      funnel: true
    bi:
      enabled: true
      hostname: "acme-bi"
      funnel: false

secrets:
  tailscale:
    create: true
    authKey: "tskey-auth-xxx"
  backup:
    create: true
    accessKeyId: "minio-key"
    secretAccessKey: "minio-secret"
  deployKeys:
    - name: "github-deploy-key"
      privateKey: |
        -----BEGIN OPENSSH PRIVATE KEY-----
        ...
        -----END OPENSSH PRIVATE KEY-----
```

## Configuration

### Odoo Settings

| Parameter | Description | Default |
|-----------|-------------|---------|
| `odoo.version` | Odoo version (17, 18, etc.) | `"17"` |
| `odoo.image` | Custom image | `""` (uses `odoo:<version>`) |
| `odoo.replicas` | Number of replicas | `1` |
| `odoo.storage` | Filestore size | `"10Gi"` |
| `odoo.addons` | Git addon repos | `[]` |

### Database Settings

| Parameter | Description | Default |
|-----------|-------------|---------|
| `database.storage` | PostgreSQL storage | `"20Gi"` |
| `database.instances` | Replicas (1=dev, 2+=HA) | `1` |
| `database.backup.enabled` | Enable backups | `false` |
| `database.backup.schedule` | Cron schedule | `"0 2 * * *"` |

### Add-ons

| Parameter | Description | Default |
|-----------|-------------|---------|
| `addons.valkey.enabled` | Enable Valkey cache | `false` |
| `addons.bi.enabled` | Enable Metabase BI | `false` |

### Networking - Ingress

| Parameter | Description | Default |
|-----------|-------------|---------|
| `networking.ingress.enabled` | Enable Kubernetes Ingress | `false` |
| `networking.ingress.className` | Ingress class (nginx, alb, etc.) | `""` |
| `networking.ingress.annotations` | Global ingress annotations | `{}` |
| `networking.ingress.tls.enabled` | Enable TLS | `false` |
| `networking.ingress.tls.secretName` | TLS secret (empty for ACM) | `""` |
| `networking.ingress.odoo.enabled` | Create Odoo ingress | `true` |
| `networking.ingress.odoo.host` | Odoo hostname (required) | `""` |
| `networking.ingress.bi.enabled` | Create Metabase ingress | `false` |
| `networking.ingress.bi.host` | Metabase hostname | `""` |

### Networking - Tailscale

| Parameter | Description | Default |
|-----------|-------------|---------|
| `networking.tailscale.odoo.enabled` | Enable Tailscale for Odoo | `false` |
| `networking.tailscale.odoo.funnel` | Public access via Funnel | `true` |
| `networking.tailscale.bi.enabled` | Enable Tailscale for BI | `false` |

## Uninstallation

```bash
helm uninstall acme --namespace odoo-acme

# Optionally delete the namespace (WARNING: deletes all data!)
kubectl delete namespace odoo-acme
```

This removes the OdooCluster CR. The operator will clean up:
- PostgreSQL cluster
- All deployments and services
- PVCs (data remains until namespace is deleted)

## Upgrading Odoo Version

```bash
helm upgrade acme ./charts/odoocluster \
  --namespace odoo-acme \
  --set odoo.version="18"
```

The operator will perform a rolling update to the new version.

