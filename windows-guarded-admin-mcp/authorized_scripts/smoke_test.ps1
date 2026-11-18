param(
    [string]$Message = 'Windows Server Admin MCP smoke test OK'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

@{
    success = $true
    data    = @{
        message      = $Message
        computerName = $env:COMPUTERNAME
        timestampUtc = (Get-Date).ToUniversalTime().ToString('o')
        psVersion    = $PSVersionTable.PSVersion.ToString()
    }
} | ConvertTo-Json -Depth 6 -Compress
