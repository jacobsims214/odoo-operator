#!/bin/bash
#
# Create Cloudflare Tunnel Secret for OdooCluster
#
# This script creates the Kubernetes secret required for Cloudflare Tunnel.
#
# Prerequisites:
#   1. Go to Cloudflare Dashboard → Zero Trust → Networks → Tunnels
#   2. Create a new tunnel (e.g., "simstech-odoo")
#   3. Copy the tunnel token from the "Install connector" step
#   4. Add Public Hostnames in the dashboard:
#      - www.simstech.cloud → http://simstech-odoo:8069
#      - data.simstech.cloud → http://simstech-metabase:3000
#
# Usage:
#   ./create-cloudflare-tunnel-secret.sh <namespace> <secret-name> <tunnel-token>
#
# Example:
#   ./create-cloudflare-tunnel-secret.sh simstech-odoo cloudflare-tunnel "eyJhIjoiNzM0..."
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_usage() {
    echo "Usage:"
    echo "  $0 <namespace> <secret-name> <tunnel-token>"
    echo ""
    echo "Example:"
    echo "  $0 simstech-odoo cloudflare-tunnel 'eyJhIjoiNzM0...'"
    echo ""
    echo "To get your tunnel token:"
    echo "  1. Go to Cloudflare Dashboard → Zero Trust → Networks → Tunnels"
    echo "  2. Create a new tunnel or select existing"
    echo "  3. In 'Install connector' step, copy the token (starts with 'eyJ...')"
}

if [ "$#" -ne 3 ]; then
    echo -e "${RED}Error: Expected 3 arguments${NC}"
    print_usage
    exit 1
fi

NAMESPACE=$1
SECRET_NAME=$2
TUNNEL_TOKEN=$3

# Validate token looks correct
if [[ ! "$TUNNEL_TOKEN" =~ ^eyJ ]]; then
    echo -e "${YELLOW}Warning: Token doesn't start with 'eyJ' - make sure you copied the full token${NC}"
fi

echo -e "${BLUE}Creating Cloudflare Tunnel secret...${NC}"
echo "  Namespace: $NAMESPACE"
echo "  Secret name: $SECRET_NAME"

# Ensure namespace exists
if ! kubectl get namespace "$NAMESPACE" &>/dev/null; then
    echo -e "${YELLOW}Creating namespace: $NAMESPACE${NC}"
    kubectl create namespace "$NAMESPACE"
fi

# Delete existing secret if it exists
if kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" &>/dev/null; then
    echo -e "${YELLOW}Deleting existing secret: $SECRET_NAME${NC}"
    kubectl delete secret "$SECRET_NAME" -n "$NAMESPACE"
fi

# Create secret with tunnel token
kubectl create secret generic "$SECRET_NAME" \
    --namespace="$NAMESPACE" \
    --from-literal=TUNNEL_TOKEN="$TUNNEL_TOKEN"

echo ""
echo -e "${GREEN}✓ Secret created successfully!${NC}"
echo ""
echo -e "${BLUE}=== Next Steps ===${NC}"
echo ""
echo "1. Configure Public Hostnames in Cloudflare Dashboard:"
echo "   Zero Trust → Networks → Tunnels → Your Tunnel → Public Hostname"
echo ""
echo "   For Odoo:"
echo "   ┌─────────────────────────────────────────────┐"
echo "   │ Subdomain: www (or @)                       │"
echo "   │ Domain: simstech.cloud                      │"
echo "   │ Service Type: HTTP                          │"
echo "   │ URL: ${SECRET_NAME%-tunnel}-odoo:8069              │"
echo "   └─────────────────────────────────────────────┘"
echo ""
echo "   For Metabase:"
echo "   ┌─────────────────────────────────────────────┐"
echo "   │ Subdomain: data                             │"
echo "   │ Domain: simstech.cloud                      │"
echo "   │ Service Type: HTTP                          │"
echo "   │ URL: ${SECRET_NAME%-tunnel}-metabase:3000          │"
echo "   └─────────────────────────────────────────────┘"
echo ""
echo "2. Add to your OdooCluster spec:"
echo ""
echo "   networking:"
echo "     cloudflare:"
echo "       enabled: true"
echo "       tunnelSecretName: \"$SECRET_NAME\""
echo "       odoo:"
echo "         hostname: \"www.simstech.cloud\""
echo "       bi:"
echo "         hostname: \"data.simstech.cloud\""
echo ""
echo -e "${GREEN}Done!${NC}"
