// VanakkamIQ on Azure: Container Apps + Container Registry.
// Uses your EXISTING PostgreSQL server and database (nothing database-related is created here).
// Deploy: see docs/azure-deploy.md

@description('Short lowercase name used as a prefix for every resource.')
param appName string = 'vanakkamiq'
param location string = resourceGroup().location

@description('First run: false (creates registry, environment, identity). Second run: true, with image set.')
param deployApp bool = false

@description('Container image, e.g. myacr.azurecr.io/vanakkamiq:v1 (required when deployApp is true).')
param image string = ''

// Secrets are only needed for the app (deployApp=true). Keep them in a git-ignored
// parameters file (infra/secrets.local.json), never on the command line or in git.
@secure()
@description('Existing database: postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME?ssl=require (URL-encode special characters in the password).')
param databaseUrl string = ''

@description('Schema inside that database for VanakkamIQ tables; created if missing. Empty = default schema (public).')
param databaseSchema string = 'vanakkamiq'

@secure()
param secretKey string = ''
@secure()
param openaiApiKey string = ''
param openaiProjectId string = ''
@secure()
param openaiWebhookSecret string = ''
@secure()
param telnyxApiKey string = ''
@description('Telnyx Voice API public key (webhook signatures; milestone 6b routing). Not secret, but kept with the other settings.')
param telnyxPublicKey string = ''

@secure()
@description('Azure Communication Services email: endpoint=https://...;accesskey=... (sign-in codes).')
param acsConnectionString string = ''
@description('Sender address for sign-in code emails (must be a verified ACS sender).')
param acsSender string = 'DoNotReply@nativemindsai.com'

@description('Business dashboard domain, e.g. owner.vanakkamiq.example.com. Empty = only the default Azure address.')
param ownerHost string = ''
@description('Platform admin domain, e.g. admin.vanakkamiq.example.com.')
param adminHost string = ''
@description('Managed certificate names in the environment for ownerHost and adminHost (created by `az containerapp hostname bind`; see docs/azure-deploy.md).')
param ownerCertificate string = ''
param adminCertificate string = ''
@description('Public website domain, e.g. vanakkamiq.example.com, and its managed certificate name.')
param siteHost string = ''
param siteCertificate string = ''
@description('Where website demo requests are emailed.')
param demoRequestTo string = 'info@nativemindsai.com'

var suffix = uniqueString(resourceGroup().id)
var acrName = toLower('${appName}${suffix}')

// Container Apps secrets can't be empty: only the ones provided are added.
var secretDefs = [
  { name: 'database-url', env: 'DATABASE_URL', value: databaseUrl }
  { name: 'secret-key', env: 'SECRET_KEY', value: secretKey }
  { name: 'openai-api-key', env: 'OPENAI_API_KEY', value: openaiApiKey }
  { name: 'openai-webhook-secret', env: 'OPENAI_WEBHOOK_SECRET', value: openaiWebhookSecret }
  { name: 'telnyx-api-key', env: 'TELNYX_API_KEY', value: telnyxApiKey }
  { name: 'acs-connection-string', env: 'ACS_CONNECTION_STRING', value: acsConnectionString }
]
var presentSecrets = filter(secretDefs, s => !empty(s.value))
var appSecrets = map(presentSecrets, s => { name: s.name, value: s.value })
var secretEnv = map(presentSecrets, s => { name: s.env, secretRef: s.name })
var plainEnv = [
  { name: 'APP_ENV', value: 'production' }
  { name: 'DATABASE_SCHEMA', value: databaseSchema }
  { name: 'OPENAI_PROJECT_ID', value: openaiProjectId }
  { name: 'ACS_SENDER', value: acsSender }
  { name: 'TELNYX_PUBLIC_KEY', value: telnyxPublicKey }
]

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${appName}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${appName}-identity'
  location: location
}

// Let the app's identity pull images from the registry.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acrpull')
  scope: acr
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${appName}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [ acrPull ]
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // Declared here so a redeploy keeps them; the certificates already exist in the environment.
        customDomains: concat(
          empty(ownerHost) || empty(ownerCertificate) ? [] : [
            { name: ownerHost, bindingType: 'SniEnabled', certificateId: '${env.id}/managedCertificates/${ownerCertificate}' }
          ],
          empty(adminHost) || empty(adminCertificate) ? [] : [
            { name: adminHost, bindingType: 'SniEnabled', certificateId: '${env.id}/managedCertificates/${adminCertificate}' }
          ],
          empty(siteHost) || empty(siteCertificate) ? [] : [
            { name: siteHost, bindingType: 'SniEnabled', certificateId: '${env.id}/managedCertificates/${siteCertificate}' }
          ]
        )
      }
      registries: [
        { server: acr.properties.loginServer, identity: identity.id }
      ]
      secrets: appSecrets
    }
    template: {
      // Calls live inside the replica that accepted them: give a replica time to finish
      // its calls before it is stopped during a deploy or scale-in.
      terminationGracePeriodSeconds: 300
      containers: [
        {
          name: 'api'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(plainEnv, secretEnv, [
            // Order-status links in texts use the owner domain when there is one.
            { name: 'PUBLIC_BASE_URL', value: empty(ownerHost) ? 'https://${appName}.${env.properties.defaultDomain}' : 'https://${ownerHost}' }
            { name: 'OWNER_HOST', value: ownerHost }
            { name: 'ADMIN_HOST', value: adminHost }
            { name: 'SITE_HOST', value: siteHost }
            { name: 'DEMO_REQUEST_TO', value: demoRequestTo }
          ])
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        // Never scale to zero: an incoming call must be answered immediately.
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output appUrl string = deployApp ? 'https://${app!.properties.configuration.ingress.fqdn}' : ''
output webhookUrl string = deployApp ? 'https://${app!.properties.configuration.ingress.fqdn}/voice/openai/webhook' : ''
// Add these to your PostgreSQL server's firewall so the app can connect.
output outboundIps array = deployApp ? app!.properties.outboundIpAddresses : []
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
