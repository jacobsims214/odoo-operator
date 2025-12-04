#!/bin/bash
# Create a deploy key secret for private Git repositories

set -e

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <cluster-name> <secret-name> [key-file]"
    echo ""
    echo "This script creates a Kubernetes secret containing an SSH deploy key"
    echo "for cloning private Git repositories."
    echo ""
    echo "Arguments:"
    echo "  cluster-name  - Name of the OdooCluster (used for namespace)"
    echo "  secret-name   - Name for the Kubernetes secret"
    echo "  key-file      - Optional: existing private key file"
    echo ""
    echo "Examples:"
    echo "  # Generate new key and create secret:"
    echo "  $0 acme-corp github-deploy-key"
    echo ""
    echo "  # Use existing key:"
    echo "  $0 acme-corp github-deploy-key ~/.ssh/my-deploy-key"
    exit 1
fi

CLUSTER_NAME=$1
SECRET_NAME=$2
KEY_FILE=${3:-}

NAMESPACE="odoo-${CLUSTER_NAME}"

# Create namespace if it doesn't exist
echo "Ensuring namespace exists: ${NAMESPACE}"
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -

if [ -z "${KEY_FILE}" ]; then
    # Generate new key
    TEMP_DIR=$(mktemp -d)
    KEY_FILE="${TEMP_DIR}/deploy-key"
    
    echo "Generating new ED25519 deploy key..."
    ssh-keygen -t ed25519 -C "${CLUSTER_NAME}-deploy-key" -f "${KEY_FILE}" -N ""
    
    echo ""
    echo "=========================================="
    echo "PUBLIC KEY (add this to your Git repo):"
    echo "=========================================="
    cat "${KEY_FILE}.pub"
    echo ""
    echo "=========================================="
    echo ""
    echo "Add this public key as a deploy key in your Git repository:"
    echo "  - GitHub: Settings → Deploy keys → Add deploy key"
    echo "  - GitLab: Settings → Repository → Deploy keys"
    echo "  - Bitbucket: Settings → Access keys → Add key"
    echo ""
fi

# Verify key file exists
if [ ! -f "${KEY_FILE}" ]; then
    echo "Error: Key file not found: ${KEY_FILE}"
    exit 1
fi

echo "Creating secret: ${SECRET_NAME} in namespace: ${NAMESPACE}"
kubectl -n ${NAMESPACE} create secret generic ${SECRET_NAME} \
    --from-file=ssh-privatekey="${KEY_FILE}" \
    --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "Secret created successfully!"
echo ""
echo "Reference this in your OdooCluster:"
echo ""
echo "spec:"
echo "  odoo:"
echo "    addons:"
echo "      - name: \"my-addons\""
echo "        repo: \"git@github.com:org/repo.git\""
echo "        branch: \"17.0\""
echo "        deployKeySecret: \"${SECRET_NAME}\""

# Cleanup temp files if we generated a key
if [ -n "${TEMP_DIR}" ]; then
    echo ""
    echo "Temporary key files are in: ${TEMP_DIR}"
    echo "You may want to save these securely or delete them."
fi

