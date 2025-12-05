#!/bin/bash
#
# Create Cloudflare Tunnel Secret for OdooCluster
#
# This script creates the Kubernetes secret required for Cloudflare Tunnel.
# You need to first create a tunnel in the Cloudflare Dashboard.
#
# Prerequisites:
#   1. Go to Cloudflare Dashboard → Zero Trust → Networks → Tunnels
#   2. Create a new tunnel (e.g., "simstech-odoo")
#   3. Save the Tunnel ID (UUID)
#   4. Download the credentials.json file OR copy the tunnel token
#
# Usage:
#   Method 1 - Using credentials.json file:
#     ./create-cloudflare-tunnel-secret.sh <namespace> <secret-name> --credentials-file /path/to/credentials.json
#
#   Method 2 - Using tunnel token (simpler):
#     ./create-cloudflare-tunnel-secret.sh <namespace> <secret-name> --token <tunnel-token>
#
# Example:
#   ./create-cloudflare-tunnel-secret.sh simstech-odoo cloudflare-tunnel --credentials-file ~/Downloads/abc123.json
#   ./create-cloudflare-tunnel-secret.sh simstech-odoo cloudflare-tunnel --token eyJhIjoiNzM0...
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_usage() {
    echo "Usage:"
    echo "  $0 <namespace> <secret-name> --credentials-file <path>"
    echo "  $0 <namespace> <secret-name> --token <tunnel-token>"
    echo ""
    echo "Examples:"
    echo "  $0 simstech-odoo cloudflare-tunnel --credentials-file ~/Downloads/abc123.json"
    echo "  $0 simstech-odoo cloudflare-tunnel --token eyJhIjoiNzM0..."
    echo ""
    echo "To get these values:"
    echo "  1. Go to Cloudflare Dashboard → Zero Trust → Networks → Tunnels"
    echo "  2. Create a new tunnel or select existing"
    echo "  3. Download credentials.json OR copy the tunnel token"
}

if [ "$#" -lt 4 ]; then
    echo -e "${RED}Error: Not enough arguments${NC}"
    print_usage
    exit 1
fi

NAMESPACE=$1
SECRET_NAME=$2
shift 2

# Parse arguments
CREDENTIALS_FILE=""
TOKEN=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --credentials-file)
            CREDENTIALS_FILE="$2"
            shift 2
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            print_usage
            exit 1
            ;;
    esac
done

# Validate inputs
if [ -z "$CREDENTIALS_FILE" ] && [ -z "$TOKEN" ]; then
    echo -e "${RED}Error: Must provide either --credentials-file or --token${NC}"
    print_usage
    exit 1
fi

echo -e "${YELLOW}Creating Cloudflare Tunnel secret...${NC}"
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

if [ -n "$CREDENTIALS_FILE" ]; then
    # Method 1: Using credentials.json file
    if [ ! -f "$CREDENTIALS_FILE" ]; then
        echo -e "${RED}Error: Credentials file not found: $CREDENTIALS_FILE${NC}"
        exit 1
    fi

    # Extract tunnel ID from credentials.json
    TUNNEL_ID=$(jq -r '.TunnelID' "$CREDENTIALS_FILE" 2>/dev/null)
    if [ -z "$TUNNEL_ID" ] || [ "$TUNNEL_ID" == "null" ]; then
        echo -e "${RED}Error: Could not extract TunnelID from credentials file${NC}"
        exit 1
    fi

    echo "  Tunnel ID: $TUNNEL_ID"

    kubectl create secret generic "$SECRET_NAME" \
        --namespace="$NAMESPACE" \
        --from-file=credentials.json="$CREDENTIALS_FILE"

    echo ""
    echo -e "${GREEN}Secret created successfully!${NC}"
    echo ""
    echo "Add this to your OdooCluster spec:"
    echo ""
    echo "  networking:"
    echo "    cloudflare:"
    echo "      enabled: true"
    echo "      tunnelSecretName: \"$SECRET_NAME\""
    echo "      tunnelId: \"$TUNNEL_ID\""
    echo "      odoo:"
    echo "        hostname: \"www.yourdomain.com\""
    echo "      bi:"
    echo "        hostname: \"data.yourdomain.com\""

elif [ -n "$TOKEN" ]; then
    # Method 2: Using tunnel token (newer method)
    # The token is a base64-encoded JSON containing the credentials
    
    # Decode token to extract tunnel ID (token is base64 encoded JSON)
    DECODED=$(echo "$TOKEN" | base64 -d 2>/dev/null || echo "")
    if [ -n "$DECODED" ]; then
        TUNNEL_ID=$(echo "$DECODED" | jq -r '.t' 2>/dev/null || echo "")
        ACCOUNT_TAG=$(echo "$DECODED" | jq -r '.a' 2>/dev/null || echo "")
        TUNNEL_SECRET=$(echo "$DECODED" | jq -r '.s' 2>/dev/null || echo "")
        
        if [ -n "$TUNNEL_ID" ] && [ "$TUNNEL_ID" != "null" ]; then
            echo "  Tunnel ID: $TUNNEL_ID"
            
            # Create credentials.json from token components
            CREDENTIALS_JSON=$(cat <<EOF
{
  "AccountTag": "$ACCOUNT_TAG",
  "TunnelID": "$TUNNEL_ID",
  "TunnelSecret": "$TUNNEL_SECRET"
}
EOF
)
            kubectl create secret generic "$SECRET_NAME" \
                --namespace="$NAMESPACE" \
                --from-literal=credentials.json="$CREDENTIALS_JSON"
        else
            # Fallback: store token directly (for --token method with cloudflared)
            echo "  (Could not parse token, storing as-is)"
            kubectl create secret generic "$SECRET_NAME" \
                --namespace="$NAMESPACE" \
                --from-literal=tunnel-token="$TOKEN"
        fi
    else
        # Store token directly
        kubectl create secret generic "$SECRET_NAME" \
            --namespace="$NAMESPACE" \
            --from-literal=tunnel-token="$TOKEN"
    fi

    echo ""
    echo -e "${GREEN}Secret created successfully!${NC}"
    echo ""
    echo "Add this to your OdooCluster spec:"
    echo ""
    echo "  networking:"
    echo "    cloudflare:"
    echo "      enabled: true"
    echo "      tunnelSecretName: \"$SECRET_NAME\""
    echo "      tunnelId: \"<your-tunnel-id>\"  # Get from CF Dashboard"
    echo "      odoo:"
    echo "        hostname: \"www.yourdomain.com\""
    echo "      bi:"
    echo "        hostname: \"data.yourdomain.com\""
fi

echo ""
echo -e "${YELLOW}Don't forget to add DNS records in Cloudflare:${NC}"
echo "  1. Go to Cloudflare Dashboard → DNS"
echo "  2. Add CNAME record: www.yourdomain.com → <tunnel-id>.cfargotunnel.com"
echo "  3. Add CNAME record: data.yourdomain.com → <tunnel-id>.cfargotunnel.com"
echo "  (Or configure hostnames in the Tunnel settings)"

