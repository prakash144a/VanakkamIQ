#!/bin/bash
# One-time setup so GitHub Actions can deploy to Azure without stored passwords (OIDC).
#
#   bash scripts/setup-github-deploy.sh <github-owner/repo> <resource-group> <acr-name> [container-app]
#
# Creates an Entra app registration "vanakkamiq-github-deploy" that trusts only this repo's
# "production" environment, gives it Contributor on the resource group (build images, update
# the Container App), and saves the IDs as GitHub secrets/variables with the gh CLI.
set -euo pipefail

REPO=$1
RG=$2
ACR=$3
APP=${4:-vanakkamiq}
NAME=vanakkamiq-github-deploy

SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)
RG_ID=$(az group show -n "$RG" --query id -o tsv)

CLIENT_ID=$(az ad app list --display-name "$NAME" --query "[0].appId" -o tsv)
if [ -z "$CLIENT_ID" ]; then
  CLIENT_ID=$(az ad app create --display-name "$NAME" --query appId -o tsv)
  echo "Created app registration $NAME"
fi
az ad sp show --id "$CLIENT_ID" >/dev/null 2>&1 || az ad sp create --id "$CLIENT_ID" >/dev/null

SUBJECT="repo:${REPO}:environment:production"
if ! az ad app federated-credential list --id "$CLIENT_ID" --query "[?subject=='$SUBJECT']" -o tsv | grep -q .; then
  az ad app federated-credential create --id "$CLIENT_ID" --parameters "{
    \"name\": \"github-production\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$SUBJECT\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" >/dev/null
  echo "Trusted $SUBJECT"
fi

az role assignment create --assignee "$CLIENT_ID" --role Contributor --scope "$RG_ID" >/dev/null
echo "Granted Contributor on $RG"

gh api -X PUT "repos/$REPO/environments/production" >/dev/null
gh secret set AZURE_CLIENT_ID --repo "$REPO" --body "$CLIENT_ID"
gh secret set AZURE_TENANT_ID --repo "$REPO" --body "$TENANT"
gh secret set AZURE_SUBSCRIPTION_ID --repo "$REPO" --body "$SUB"
gh variable set AZURE_RESOURCE_GROUP --repo "$REPO" --body "$RG"
gh variable set AZURE_ACR_NAME --repo "$REPO" --body "$ACR"
gh variable set AZURE_CONTAINER_APP --repo "$REPO" --body "$APP"
echo "GitHub secrets and variables saved for $REPO"
