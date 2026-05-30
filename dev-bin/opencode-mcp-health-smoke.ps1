[CmdletBinding()]
param(
    [string]$OpenCodePath = "opencode",
    [string]$McpUrl = "http://127.0.0.1:8767/mcp",
    [string]$ProjectDir = "",
    [switch]$KeepConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Failure {
    param([string]$Message)
    [Console]::Error.WriteLine("ERROR: $Message")
}

Write-Host "CodePilot OpenCode MCP health smoke"
Write-Host "Prerequisite: codepilot mcp serve --transport http --port 8767"
Write-Host "MCP URL: $McpUrl"

if (-not $ProjectDir) {
    $ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$openCodeCommand = Get-Command -Name $OpenCodePath -ErrorAction SilentlyContinue
if (-not $openCodeCommand) {
    Write-Failure "OpenCode binary was not found: $OpenCodePath"
    Write-Failure "Install OpenCode or pass -OpenCodePath with the binary path."
    exit 127
}

$openCodeExecutable = $openCodeCommand.Path
if (-not $openCodeExecutable) {
    $openCodeExecutable = $openCodeCommand.Source
}

$configPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "codepilot-opencode-mcp-health-smoke-{0}.json" -f [System.Guid]::NewGuid().ToString("N")
)
$config = [ordered]@{
    '$schema' = "https://opencode.ai/config.json"
    mcp = [ordered]@{
        codepilot = [ordered]@{
            type = "remote"
            url = $McpUrl
            enabled = $true
        }
    }
}
$config | ConvertTo-Json -Depth 8 | Set-Content -Path $configPath -Encoding UTF8

$prompt = @"
Use the MCP server named codepilot and call the MCP tool codepilot.health exactly once.
Return only a compact JSON object with the ok, version, project, and project_path fields from the tool result.
Do not answer from memory. If the tool call fails, describe the failure.
"@

$oldOpenCodeConfig = $env:OPENCODE_CONFIG
$env:OPENCODE_CONFIG = $configPath
$outputLines = @()
$exitCode = 1

try {
    Push-Location $ProjectDir
    try {
        # opencode run is the headless OpenCode invocation used for this smoke.
        $outputLines = & $openCodeExecutable run --format json --dangerously-skip-permissions $prompt 2>&1 |
            ForEach-Object { $_.ToString() }
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Failure "OpenCode smoke command failed before completion: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($null -eq $oldOpenCodeConfig) {
        Remove-Item Env:\OPENCODE_CONFIG -ErrorAction SilentlyContinue
    }
    else {
        $env:OPENCODE_CONFIG = $oldOpenCodeConfig
    }

    if (-not $KeepConfig) {
        Remove-Item -Path $configPath -ErrorAction SilentlyContinue
    }
}

$outputText = $outputLines -join [Environment]::NewLine
$normalizedOutput = $outputText -replace '\\"', '"'

if ($exitCode -ne 0) {
    Write-Failure "OpenCode exited with code $exitCode. Confirm the MCP server is running and OpenCode is authenticated."
    if ($outputText) {
        [Console]::Error.WriteLine($outputText)
    }
    exit $exitCode
}

if ($normalizedOutput -notmatch '"ok"\s*:\s*true') {
    Write-Failure "codepilot.health did not return ok: true through OpenCode MCP."
    Write-Failure "Start the server first with: codepilot mcp serve --transport http --port 8767"
    if ($outputText) {
        [Console]::Error.WriteLine($outputText)
    }
    exit 1
}

Write-Host "OpenCode MCP health smoke passed: codepilot.health returned ok: true"
exit 0
