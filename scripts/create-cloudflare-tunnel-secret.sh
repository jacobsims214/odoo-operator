#!/bin/bash
#
# Create Cloudflare Tunnel Secret for OdooCluster
#
# This script creates the Kubernetes secret required for Cloudflare Tunnel
# using the CONFIG FILE approach (supports multiple hostnames per tunnel).
#
# Prerequisites:
#   1. Install cloudflared CLI: brew install cloudflared
#   2. Login: cloudflared tunnel login
#   3. Create tunnel: cloudflared tunnel create <tunnel-name>
#      This creates ~/.cloudflared/<tunnel-id>.json (credentials file)
#   4. Note the Tunnel ID (UUID) from the output
#
# Usage:
#   ./create-cloudflare-tunnel-secret.sh <namespace> <secret-name> <tunnel-id> <credentials-file>
#
# Example:
#   ./create-cloudflare-tunnel-secret.sh simstech-odoo cloudflare-tunnel \
#     "a1b2c3d4-e5f6-7890-abcd-ef1234567890" \
#     ~/.cloudflared/a1b2c3d4-e5f6-7890-abcd-ef1234567890.json
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
    echo "  $0 <namespace> <secret-name> <tunnel-id> <credentials-file>"
    echo ""
    echo "Example:"
    echo "  $0 simstech-odoo cloudflare-tunnel \\"
    echo "    'a1b2c3d4-e5f6-7890-abcd-ef1234567890' \\"
    echo "    ~/.cloudflared/a1b2c3d4-e5f6-7890-abcd-ef1234567890.json"
    echo ""
    echo "To create a tunnel:"
    echo "  1. Install cloudflared: brew install cloudflared"
    echo "  2. Login: cloudflared tunnel login"
    echo "  3. Create: cloudflared tunnel create my-tunnel"
    echo "  4. Note the Tunnel ID and credentials file path"
}

if [ "$#" -ne 4 ]; then
    echo -e "${RED}Error: Expected 4 arguments${NC}"
    print_usage
    exit 1
fi

NAMESPACE=$1
SECRET_NAME=$2
TUNNEL_ID=$3
CREDENTIALS_FILE=$4

# Validate credentials file exists
if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo -e "${RED}Error: Credentials file not found: $CREDENTIALS_FILE${NC}"
    echo ""
    echo "Create a tunnel first:"
    echo "  cloudflared tunnel login"
    echo "  cloudflared tunnel create my-tunnel"
    exit 1
fi

# Validate tunnel ID format (UUID)
if [[ ! "$TUNNEL_ID" =~ ^[a-f0-9-]{36}$ ]]; then
    echo -e "${YELLOW}Warning: Tunnel ID doesn't look like a UUID: $TUNNEL_ID${NC}"
fi

echo -e "${BLUE}Creating Cloudflare Tunnel secret...${NC}"
echo "  Namespace: $NAMESPACE"
echo "  Secret name: $SECRET_NAME"
echo "  Tunnel ID: $TUNNEL_ID"
echo "  Credentials: $CREDENTIALS_FILE"

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

# Create secret with credentials.json and TUNNEL_ID
kubectl create secret generic "$SECRET_NAME" \
    --namespace="$NAMESPACE" \
    --from-file=credentials.json="$CREDENTIALS_FILE" \
    --from-literal=TUNNEL_ID="$TUNNEL_ID"

echo ""
echo -e "${GREEN}✓ Secret created successfully!${NC}"
echo ""
echo -e "${BLUE}=== Next Steps ===${NC}"
echo ""
echo "1. Add DNS CNAME records in Cloudflare Dashboard:"
echo "   ┌─────────────────────────────────────────────────────────────┐"
echo "   │ Type: CNAME                                                 │"
echo "   │ Name: www                                                   │"
echo "   │ Target: ${TUNNEL_ID}.cfargotunnel.com                       │"
echo "   │ Proxied: Yes (orange cloud)                                 │"
echo "   ├─────────────────────────────────────────────────────────────┤"
echo "   │ Type: CNAME                                                 │"
echo "   │ Name: data                                                  │"
echo "   │ Target: ${TUNNEL_ID}.cfargotunnel.com                       │"
echo "   │ Proxied: Yes (orange cloud)                                 │"
echo "   └─────────────────────────────────────────────────────────────┘"
echo ""
echo "2. Add to your OdooCluster spec:"
echo ""
echo "   networking:"
echo "     cloudflare:"
echo "       enabled: true"
echo "       tunnelSecretName: \"$SECRET_NAME\""
echo "       odoo:"
echo "         hostname: \"www.yourdomain.com\""
echo "       bi:"
echo "         hostname: \"data.yourdomain.com\""
echo ""
echo -e "${GREEN}Done!${NC}"
