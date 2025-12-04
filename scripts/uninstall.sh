#!/bin/bash
# Uninstall the Simstech Odoo Operator

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"

echo "Uninstalling Simstech Odoo Operator..."

# Check for existing clusters
CLUSTERS=$(kubectl get odooclusters -o name 2>/dev/null || true)
if [ -n "${CLUSTERS}" ]; then
    echo ""
    echo "WARNING: The following OdooClusters still exist:"
    echo "${CLUSTERS}"
    echo ""
    read -p "Delete all clusters before uninstalling? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        kubectl delete odooclusters --all
        echo "Waiting for cluster deletion..."
        sleep 10
    else
        echo "Please delete clusters manually before uninstalling."
        exit 1
    fi
fi

echo "1. Deleting operator deployment..."
kubectl delete -f "${ROOT_DIR}/config/manager.yaml" --ignore-not-found

echo "2. Deleting RBAC..."
kubectl delete -f "${ROOT_DIR}/config/rbac.yaml" --ignore-not-found

echo "3. Deleting CRD..."
kubectl delete -f "${ROOT_DIR}/crds/odoocluster-crd.yaml" --ignore-not-found

echo ""
echo "Uninstallation complete!"

