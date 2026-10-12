// Deploys the FinScan API on Azure Container Apps with persistent Azure Files
// storage for company profiles, secrets in Key Vault, and Log Analytics /
// Application Insights wired in. See docs/azure_foundry_teams_deployment.md
// Phase 1 for how this fits into the wider Foundry + Teams architecture.

@description('Environment suffix, e.g. dev, prod')
param envName string = 'prod'

@description('Azure region')
param location string = resourceGroup().location

@description('Full container image reference, e.g. myacr.azurecr.io/finscan:latest')
param containerImage string

@secure()
@description('Static API key FinScan checks on the x-api-key header')
param finscanApiKey string

@secure()
@description('Azure OpenAI API key for the Foundry project deployment FinScan calls')
param azureOpenAiApiKey string

@description('Azure OpenAI endpoint, e.g. https://<resource>.openai.azure.com/')
param azureOpenAiEndpoint string

@description('Azure OpenAI deployment name, e.g. gpt-4o')
param azureOpenAiDeployment string = 'gpt-4o'

var prefix = 'finscan-${envName}'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: replace('${prefix}acr', '-', '')
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: replace('${prefix}sa', '-', '')
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2' }
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource dataShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: 'finscan-data'
  properties: { shareQuota: 100 }
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}-kv'
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-identity'
  location: location
}

resource kvSecretApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'finscan-api-key'
  properties: { value: finscanApiKey }
}

resource kvSecretOpenAi 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'azure-openai-api-key'
  properties: { value: azureOpenAiApiKey }
}

resource kvRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, identity.id, 'kv-secrets-user')
  scope: kv
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
  }
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, 'acr-pull')
  scope: acr
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource caEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource storageMount 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  parent: caEnv
  name: 'finscan-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: dataShare.name
      accessMode: 'ReadWrite'
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: '${prefix}-api'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: caEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
      }
      registries: [
        {
          server: '${acr.name}.azurecr.io'
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'finscan-api-key'
          keyVaultUrl: '${kv.properties.vaultUri}secrets/finscan-api-key'
          identity: identity.id
        }
        {
          name: 'azure-openai-api-key'
          keyVaultUrl: '${kv.properties.vaultUri}secrets/azure-openai-api-key'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'finscan-api'
          image: containerImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            { name: 'FINSCAN_LLM_PROVIDER', value: 'azure' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }
            { name: 'AZURE_OPENAI_API_KEY', secretRef: 'azure-openai-api-key' }
            { name: 'FINSCAN_API_KEY', secretRef: 'finscan-api-key' }
            { name: 'FINSCAN_WORK_DIR', value: '/data/work' }
            { name: 'FINSCAN_PROFILE_STORE', value: '/data/profiles' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          volumeMounts: [
            { volumeName: 'finscan-data', mountPath: '/data' }
          ]
        }
      ]
      volumes: [
        {
          name: 'finscan-data'
          storageType: 'AzureFile'
          storageName: 'finscan-data'
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
  dependsOn: [
    storageMount
    kvRoleAssignment
    acrPullRoleAssignment
  ]
}

output apiUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
