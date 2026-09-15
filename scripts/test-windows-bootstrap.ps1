# Exercise the real Windows installer with Python and uv removed from PATH.
$ErrorActionPreference = 'Stop'
$testRoot = Join-Path $env:RUNNER_TEMP 'PRA Windows bootstrap test'
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$windowsPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$installer = Join-Path $PSScriptRoot 'install-claude.ps1'

# Install the actual standalone client. No account or model call is needed.
$claudeInstaller = Join-Path $testRoot 'install-native-claude.ps1'
Invoke-WebRequest -UseBasicParsing -Uri 'https://claude.ai/install.ps1' -OutFile $claudeInstaller
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $claudeInstaller
if ($LASTEXITCODE -ne 0) { throw 'Standalone Claude installation failed.' }
$claude = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.local/bin/claude.exe'
if (-not (Test-Path -LiteralPath $claude)) { throw 'Standalone Claude binary is missing.' }

$env:PEERREVIEW_BOOTSTRAP_DIR = Join-Path $testRoot 'bootstrap'
$env:UV_CACHE_DIR = Join-Path $testRoot 'uv cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $testRoot 'managed python'
$env:CLAUDE_CONFIG_DIR = Join-Path $testRoot 'claude settings'
$env:PATH = @(
    (Join-Path $env:SystemRoot 'System32'),
    $env:SystemRoot,
    (Split-Path -Parent $windowsPowerShell)
) -join [IO.Path]::PathSeparator
foreach ($name in @('python', 'python3', 'uv', 'bash')) {
    if (Get-Command $name -CommandType Application -ErrorAction SilentlyContinue) {
        throw "$name should not be available on the test PATH."
    }
}

& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $installer -Help
if ($LASTEXITCODE -ne 0) { throw 'Installer help failed.' }
if (Test-Path -LiteralPath $env:PEERREVIEW_BOOTSTRAP_DIR) { throw 'Help installed a runtime.' }

$dataDirectory = Join-Path $testRoot 'plugin data'
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $installer -Claude $claude -DataDir $dataDirectory
if ($LASTEXITCODE -ne 0) { throw 'Clean bootstrap failed.' }

$runtime = Join-Path $dataDirectory 'runtime/Scripts/peerreview-mcp.exe'
$python = Join-Path $dataDirectory 'runtime/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $runtime)) { throw 'The MCP executable was not installed.' }
& $python -c "import sys, peerreviewagents, peerreviewagents.mcp.server; assert sys.version_info[:2] == (3, 12); print('Managed Python and PRA imports passed')"
if ($LASTEXITCODE -ne 0) { throw 'Managed runtime import failed.' }

$configuration = Get-Content -Raw -LiteralPath (Join-Path $dataDirectory 'marketplace/plugins/peer-review-agents/.mcp.json') | ConvertFrom-Json
if ($configuration.mcpServers.'peer-review-agents'.command -ne $runtime) {
    # Python normalizes separators in the serialized path.
    if ([IO.Path]::GetFullPath($configuration.mcpServers.'peer-review-agents'.command) -ne [IO.Path]::GetFullPath($runtime)) {
        throw 'The configured MCP command does not point to the installed runtime.'
    }
}

# A standalone download must fetch the source and update the same plugin.
$standalone = Join-Path $testRoot 'standalone.ps1'
Copy-Item -LiteralPath $installer -Destination $standalone
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $standalone -Claude $claude -DataDir $dataDirectory -Runtime $runtime
if ($LASTEXITCODE -ne 0) { throw 'Standalone installer or repeat installation failed.' }

$health = & $claude mcp list 2>&1 | Out-String
Write-Host $health
if ($LASTEXITCODE -ne 0 -or $health -notmatch 'Connected') {
    throw 'Claude could not connect to the installed PRA MCP server.'
}

# A failed child installer must not be reported as a successful installation.
$badData = Join-Path $testRoot 'failed installation'
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $installer -Claude (Join-Path $testRoot 'missing.exe') -DataDir $badData
if ($LASTEXITCODE -eq 0) { throw 'The installer hid a child-process failure.' }
if (Test-Path -LiteralPath $badData) { throw 'Invalid Claude path created a runtime.' }
Write-Host 'Native Windows bootstrap, upgrade, failure handling, and real MCP connection passed.'
exit 0
