#!/bin/bash
# Install the Simstech Odoo Operator

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"

echo "Installing Simstech Odoo Operator..."

echo "1. Installing CRD..."
kubectl apply -f "${ROOT_DIR}/crds/odoocluster-crd.yaml"

echo "2. Installing RBAC..."
kubectl apply -f "${ROOT_DIR}/config/rbac.yaml"

echo "3. Deploying operator..."
kubectl apply -f "${ROOT_DIR}/config/manager.yaml"

echo ""
echo "Waiting for operator to be ready..."
kubectl rollout status deployment/simstech-odoo-operator -n simstech-odoo-operator --timeout=120s

echo ""
echo "Installation complete!"
echo ""
echo "Verify with:"
echo "  kubectl get pods -n simstech-odoo-operator"
echo "  kubectl get crd odooclusters.simstech-odoo"
echo ""
echo "Create your first cluster:"
echo "  kubectl apply -f examples/minimal.yaml"

