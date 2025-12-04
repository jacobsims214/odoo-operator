# Simstech Odoo Operator Helm Chart

Deploys the Simstech Odoo Operator to manage OdooCluster resources.

## Prerequisites

- Kubernetes 1.28+
- Helm 3.x
- [CloudNative-PG Operator](https://cloudnative-pg.io/) installed

## Installation

```bash
# Add the chart repository (if hosted)
# helm repo add simstech https://charts.simstech.cloud

# Install the operator
helm install odoo-operator ./charts/operator \
  --namespace odoo.simstech.cloud-operator \
  --create-namespace
```

## Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `image.repository` | Operator image | `ghcr.io/jacobsims214/odoo-operator` |
| `image.tag` | Image tag | `latest` |
| `image.pullPolicy` | Pull policy | `Always` |
| `replicaCount` | Number of replicas | `1` |
| `resources.requests.cpu` | CPU request | `100m` |
| `resources.requests.memory` | Memory request | `256Mi` |
| `resources.limits.cpu` | CPU limit | `500m` |
| `resources.limits.memory` | Memory limit | `512Mi` |
| `serviceAccount.create` | Create service account | `true` |
| `installCRDs` | Install CRDs | `true` |

## Uninstallation

```bash
helm uninstall odoo-operator -n odoo.simstech.cloud-operator
```

Note: CRDs are not deleted automatically. To remove:

```bash
kubectl delete crd odooclusters.odoo.simstech.cloud
```

