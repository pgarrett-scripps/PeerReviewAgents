#Requires -Version 5.1
# Install PRA for Claude Code on native Windows without preinstalled Python.
[CmdletBinding()]
param(
    [string]$Claude,
    [string]$Runtime,
    [string]$DataDir,
    [switch]$ConfigureOnly,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

if ($Help) {
    Write-Output @'
Usage: powershell -NoProfile -ExecutionPolicy Bypass -File install-claude.ps1 [options]

Installs uv, managed Python, and the PRA Claude Code plugin on Windows.
No preinstalled Python, Bash, WSL, or administrator access is required.
Install and sign in to the standalone Claude Code CLI first.

Options:
  -Claude PATH      Use a specific Claude Code executable
  -Runtime PATH     Use an existing peerreview-mcp executable
  -DataDir PATH     Choose the runtime and plugin installation directory
  -ConfigureOnly    Prepare the plugin without registering it in Claude
  -Help             Show this message without downloading anything

Works from a checkout or as a standalone downloaded script.
'@
    exit 0
}

$scratch = Join-Path ([IO.Path]::GetTempPath()) ('pra-install-' + [Guid]::NewGuid().ToString('N'))
$originalPath = $env:PATH
$originalPythonDirectory = $env:UV_PYTHON_INSTALL_DIR
$originalUvInstall = $env:UV_UNMANAGED_INSTALL
$originalProtocol = [Net.ServicePointManager]::SecurityProtocol

try {
    [Net.ServicePointManager]::SecurityProtocol = $originalProtocol -bor [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Path $scratch | Out-Null

    $repoRoot = $null
    if ($PSScriptRoot) {
        $candidate = Split-Path -Parent $PSScriptRoot
        if ((Test-Path -LiteralPath (Join-Path $candidate 'scripts/install-claude.py')) -and
            (Test-Path -LiteralPath (Join-Path $candidate 'pyproject.toml'))) {
            $repoRoot = $candidate
        }
    }
    if (-not $repoRoot) {
        Write-Host 'Downloading PeerReviewAgents...'
        $archive = Join-Path $scratch 'source.zip'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/pgarrett-scripps/PeerReviewAgents/archive/refs/heads/main.zip' -OutFile $archive
        Expand-Archive -LiteralPath $archive -DestinationPath $scratch
        $repoRoot = Join-Path $scratch 'PeerReviewAgents-main'
    }

    $bootstrapRoot = $env:PEERREVIEW_BOOTSTRAP_DIR
    if (-not $bootstrapRoot) {
        $bootstrapRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'peerreviewagents/bootstrap'
    }
    $bootstrapRoot = [IO.Path]::GetFullPath($bootstrapRoot)
    $existingUv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existingUv) {
        $uvCommand = $existingUv.Source
    } else {
        $uvCommand = Join-Path $bootstrapRoot 'bin/uv.exe'
        if (-not (Test-Path -LiteralPath $uvCommand -PathType Leaf)) {
            Write-Host 'Installing the PRA runtime manager...'
            $uvInstaller = Join-Path $scratch 'install-uv.ps1'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller
            $env:UV_UNMANAGED_INSTALL = Join-Path $bootstrapRoot 'bin'
            $windowsPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
            & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
            if ($LASTEXITCODE -ne 0) {
                throw "The uv installer failed with exit code $LASTEXITCODE."
            }
        }
    }
    if (-not (Test-Path -LiteralPath $uvCommand -PathType Leaf)) {
        throw "The runtime manager was not installed at $uvCommand."
    }

    $env:PATH = (Split-Path -Parent $uvCommand) + [IO.Path]::PathSeparator + $originalPath
    if (-not $env:UV_PYTHON_INSTALL_DIR) {
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $bootstrapRoot 'python'
    }
    $installerArguments = @(
        'run', '--no-project', '--no-config', '--managed-python', '--python', '3.12',
        (Join-Path $repoRoot 'scripts/install-claude.py')
    )
    if ($Claude) { $installerArguments += @('--claude', $Claude) }
    if ($Runtime) { $installerArguments += @('--runtime', $Runtime) }
    if ($DataDir) { $installerArguments += @('--data-dir', $DataDir) }
    if ($ConfigureOnly) { $installerArguments += '--configure-only' }

    Write-Host 'Preparing Python automatically and installing the Claude Code plugin...'
    & $uvCommand @installerArguments
    if ($LASTEXITCODE -ne 0) {
        throw "PRA installation failed with exit code $LASTEXITCODE."
    }
} catch {
    [Console]::Error.WriteLine("PRA installation failed: $($_.Exception.Message)")
    exit 1
} finally {
    $env:PATH = $originalPath
    $env:UV_PYTHON_INSTALL_DIR = $originalPythonDirectory
    $env:UV_UNMANAGED_INSTALL = $originalUvInstall
    [Net.ServicePointManager]::SecurityProtocol = $originalProtocol
    if (Test-Path -LiteralPath $scratch) {
        Remove-Item -LiteralPath $scratch -Recurse -Force
    }
}
