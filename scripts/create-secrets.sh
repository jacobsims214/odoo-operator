#!/bin/bash
# Create secrets for an OdooCluster

set -e

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <cluster-name> <tailscale-auth-key> [s3-access-key] [s3-secret-key]"
    echo ""
    echo "Examples:"
    echo "  # Just Tailscale:"
    echo "  $0 acme-corp tskey-auth-xxx"
    echo ""
    echo "  # Tailscale + S3 backups:"
    echo "  $0 acme-corp tskey-auth-xxx minio-access-key minio-secret-key"
    exit 1
fi

CLUSTER_NAME=$1
TS_AUTHKEY=$2
S3_ACCESS_KEY=${3:-}
S3_SECRET_KEY=${4:-}

NAMESPACE="odoo-${CLUSTER_NAME}"

echo "Creating namespace: ${NAMESPACE}"
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -

echo "Creating Tailscale auth secret..."
kubectl -n ${NAMESPACE} create secret generic tailscale-auth \
    --from-literal=TS_AUTHKEY="${TS_AUTHKEY}" \
    --dry-run=client -o yaml | kubectl apply -f -

if [ -n "${S3_ACCESS_KEY}" ] && [ -n "${S3_SECRET_KEY}" ]; then
    echo "Creating S3 backup credentials secret..."
    kubectl -n ${NAMESPACE} create secret generic backup-s3-creds \
        --from-literal=ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
        --from-literal=SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
        --dry-run=client -o yaml | kubectl apply -f -
fi

echo ""
echo "Secrets created in namespace: ${NAMESPACE}"
echo "You can now apply your OdooCluster:"
echo "  kubectl apply -f your-cluster.yaml"

