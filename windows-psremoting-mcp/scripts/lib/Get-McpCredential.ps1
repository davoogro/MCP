param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadJson
)

$ErrorActionPreference = 'Stop'

function Write-JsonResult {
    param([hashtable]$Result)
    $Result | ConvertTo-Json -Depth 8 -Compress
}

function Get-PlainCredential {
    param(
        [string]$Username,
        [string]$Password
    )
    $secure = ConvertTo-SecureString $Password -AsPlainText -Force
    return New-Object System.Management.Automation.PSCredential($Username, $secure)
}

try {
    $request = $PayloadJson | ConvertFrom-Json
    if (-not $request.credentialTarget -and -not $request.secretVault -and -not ($request.usernameEnv -and $request.passwordEnv)) {
        throw 'No credential references supplied'
    }

    if ($request.secretVault -and $request.usernameSecret -and $request.passwordSecret) {
        if (-not (Get-Module -ListAvailable -Name Microsoft.PowerShell.SecretManagement)) {
            throw 'SecretManagement module not installed'
        }
        Import-Module Microsoft.PowerShell.SecretManagement -ErrorAction Stop
        $username = Get-Secret -Name $request.usernameSecret -Vault $request.secretVault -AsPlainText
        $password = Get-Secret -Name $request.passwordSecret -Vault $request.secretVault -AsPlainText
        Write-JsonResult @{
            username   = $username
            password   = $password
            source     = 'secretmanagement'
            targetRef  = "$($request.secretVault)/$($request.usernameSecret)"
        }
        exit 0
    }

    if ($request.credentialTarget) {
        if (Get-Module -ListAvailable -Name CredentialManager) {
            Import-Module CredentialManager -ErrorAction Stop
            $stored = Get-StoredCredential -Target $request.credentialTarget
            if ($null -eq $stored) { throw "Credential target not found: $($request.credentialTarget)" }
            Write-JsonResult @{
                username   = $stored.UserName
                password   = $stored.GetNetworkCredential().Password
                source     = 'credentialmanager'
                targetRef  = $request.credentialTarget
            }
            exit 0
        }
    }

    if ($request.usernameEnv -and $request.passwordEnv) {
        $username = [Environment]::GetEnvironmentVariable($request.usernameEnv)
        $password = [Environment]::GetEnvironmentVariable($request.passwordEnv)
        if ([string]::IsNullOrWhiteSpace($username) -or [string]::IsNullOrWhiteSpace($password)) {
            throw "Missing environment variables: $($request.usernameEnv) / $($request.passwordEnv)"
        }
        Write-JsonResult @{
            username   = $username
            password   = $password
            source     = 'environment'
            targetRef  = "$($request.usernameEnv)/$($request.passwordEnv)"
        }
        exit 0
    }

    throw 'Unable to resolve credentials from configured references'
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
