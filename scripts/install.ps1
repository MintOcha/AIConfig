# scripts/install.ps1
[CmdletBinding()]
param(
    [switch]$Help,
    [switch]$DryRun,
    [string]$CodexHome,
    [switch]$Omp,
    [switch]$Freebuff,
    [string]$FreebuffHome
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host @"
Usage: .\scripts\install.ps1 [options]

Install MCP servers, system prompts, skills, and OMP configurations on Windows PowerShell.

Options:
  -Help                Show this help message
  -DryRun              Report changes without writing files or running external commands
  -CodexHome <path>    Specify custom Codex home directory (default: `$env:CODEX_HOME or ~\.codex)
  -Omp                 Target Oh My Pi agent (~/.omp/agent)
  -Freebuff            Target freebuff agent (~/.agents)
  -FreebuffHome <path> Specify custom freebuff home directory
"@
    exit 0
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

$TargetAgent = "codex"
$UserHome = [Environment]::GetFolderPath("UserProfile")
if (-not $UserHome) {
    $UserHome = $env:HOME
}
$TargetHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $UserHome ".codex" }

if ($Omp) {
    $TargetAgent = "omp"
    $TargetHome = Join-Path (Join-Path $UserHome ".omp") "agent"
} elseif ($Freebuff -or $FreebuffHome) {
    $TargetAgent = "freebuff"
    if ($FreebuffHome) {
        $TargetHome = $FreebuffHome
    } elseif ($env:FREEBUFF_HOME) {
        $TargetHome = $env:FREEBUFF_HOME
    } else {
        $TargetHome = Join-Path $UserHome ".agents"
    }
}

if ($CodexHome) {
    $TargetHome = $CodexHome
}

$McpFile = Join-Path $RepoRoot "mcp.toml"
$SkillsFile = Join-Path $RepoRoot "skills.toml"
$PromptsFile = Join-Path $RepoRoot "prompts.toml"
$PromptsDir = Join-Path $RepoRoot "prompts"
$OmpConfigDir = if ($env:AI_CONFIG_OMP_DIR) { $env:AI_CONFIG_OMP_DIR } else { Join-Path (Join-Path $RepoRoot "config") "omp" }

# Helper: locate Python executable (python, py, or python3)
function Get-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py"
    }
    if (Get-Command python3 -ErrorAction SilentlyContinue) {
        return "python3"
    }
    return $null
}

function Require-Python {
    $cmd = Get-PythonCommand
    if (-not $cmd) {
        Write-Error "Python 3 is required. Please install Python 3 or ensure 'python' / 'py' is in your PATH."
        exit 1
    }
    return $cmd
}

function Require-Command {
    param([string]$CommandName)
    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        Write-Error "Missing required command: $CommandName. Please install it or ensure it is in your PATH."
        exit 1
    }
}

function Require-Config {
    if (-not (Test-Path -LiteralPath $McpFile -PathType Leaf)) {
        Write-Error "MCP catalog not found: $McpFile"
        exit 1
    }
    [void](Require-Python)
}

function Invoke-PythonScript {
    param(
        [string]$ScriptText,
        [string[]]$ScriptArgs = @(),
        [string]$StdInText = $null
    )
    $py = Require-Python
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $py
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $psi.ArgumentList.Add("-")
    foreach ($arg in $ScriptArgs) {
        $psi.ArgumentList.Add($arg)
    }

    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Write($ScriptText)
    if ($null -ne $StdInText) {
        # If input text was given, keep in mind we already wrote the script to stdin
    }
    $proc.StandardInput.Close()

    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($proc.ExitCode -ne 0) {
        throw "Python script error (code $($proc.ExitCode)): $stderr"
    }
    return $stdout
}

function Invoke-PythonScriptWithInput {
    param(
        [string]$ScriptText,
        [string[]]$ScriptArgs = @(),
        [string]$StdInText = ""
    )
    $py = Require-Python
    $tempPy = [System.IO.Path]::GetTempFileName() + ".py"
    try {
        [System.IO.File]::WriteAllText($tempPy, $ScriptText, [System.Text.Encoding]::UTF8)
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $py
        $psi.UseShellExecute = $false
        $psi.RedirectStandardInput = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

        $psi.ArgumentList.Add($tempPy)
        foreach ($arg in $ScriptArgs) {
            $psi.ArgumentList.Add($arg)
        }

        $proc = [System.Diagnostics.Process]::Start($psi)
        if ($StdInText) {
            $proc.StandardInput.Write($StdInText)
        }
        $proc.StandardInput.Close()

        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()

        if ($proc.ExitCode -ne 0) {
            throw "Python script error: $stderr"
        }
        return $stdout
    } finally {
        if (Test-Path -LiteralPath $tempPy) {
            Remove-Item -LiteralPath $tempPy -Force -ErrorAction SilentlyContinue
        }
    }
}

function Draw-Header {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "       AIConfig Setup (PowerShell)      " -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "Target Agent : $TargetAgent"
    Write-Host "Target Home  : $TargetHome"
    if ($DryRun) {
        Write-Host "Mode         : DRY RUN (no changes will be made)" -ForegroundColor Yellow
    }
    Write-Host ""
}

function Success-Msg([string]$Text) {
    Write-Host "✓ $Text" -ForegroundColor Green
}

function Warning-Msg([string]$Text) {
    Write-Host "! $Text" -ForegroundColor Yellow
}

function Failure-Msg([string]$Text) {
    Write-Host "x $Text" -ForegroundColor Red
}

# --- MCP functions ---

function Get-McpEntries {
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

for server_id, server in catalog.get("mcp", {}).items():
    label = server.get("label", server_id)
    category = server.get("category", "other")
    description = server.get("description", "")
    print(f"{server_id}\t{label}\t{category}\t{description}")
'@
    $output = Invoke-PythonScript -ScriptText $script -ScriptArgs @($McpFile)
    $lines = $output -split "`r?`n" | Where-Object { $_ -match '\S' }
    return $lines
}

function Get-McpValue {
    param([string]$ServerId, [string]$Field)
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

server = catalog.get("mcp", {}).get(sys.argv[2])
if server is None:
    raise SystemExit(f"unknown MCP: {sys.argv[2]}")

value = server.get(sys.argv[3], "")
if isinstance(value, list):
    for item in value:
        print(item)
else:
    print(value)
'@
    $output = Invoke-PythonScript -ScriptText $script -ScriptArgs @($McpFile, $ServerId, $Field)
    $lines = $output -split "`r?`n" | Where-Object { $_ -ne "" }
    return $lines
}

function Get-McpProviderEntries {
    param([string]$ServerId)
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

server = catalog.get("mcp", {}).get(sys.argv[2])
if server is None:
    raise SystemExit(f"unknown MCP: {sys.argv[2]}")

for provider in server.get("providers", []):
    print("\t".join((
        provider["label"],
        provider["kind"],
        provider["config_table"],
        provider["config_key"],
    )))
'@
    $output = Invoke-PythonScript -ScriptText $script -ScriptArgs @($McpFile, $ServerId)
    $lines = $output -split "`r?`n" | Where-Object { $_ -match '\S' }
    return $lines
}

function Render-McpTemplate {
    param([string]$Value)
    # Use forward slash or native slash appropriately
    $rendered = $Value.Replace("{REPO_ROOT}", $RepoRoot.Replace("\", "/"))
    $rendered = $rendered.Replace("{CODEX_HOME}", $TargetHome.Replace("\", "/"))
    return $rendered
}

function Prepare-McpConfig {
    param([string]$ServerId)
    $templatePathArr = Get-McpValue -ServerId $ServerId -Field "config_template"
    $configPathArr = Get-McpValue -ServerId $ServerId -Field "config_path"

    $templatePath = if ($templatePathArr.Count -gt 0) { $templatePathArr[0] } else { "" }
    $configPath = if ($configPathArr.Count -gt 0) { $configPathArr[0] } else { "" }

    if (-not $templatePath -and -not $configPath) {
        return $true
    }
    if (-not $templatePath -or -not $configPath) {
        Failure-Msg "error: $ServerId must define both config_template and config_path"
        return $false
    }

    $sourcePath = Join-Path $RepoRoot $templatePath
    $configPath = Render-McpTemplate $configPath
    # Convert forward slashes to backslashes if Windows path
    $configPath = [System.IO.Path]::GetFullPath($configPath)

    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        Failure-Msg "error: MCP configuration template not found: $sourcePath"
        return $false
    }

    if (Test-Path -LiteralPath $configPath) {
        Write-Host "Using existing MCP configuration: $configPath"
    } elseif ($DryRun) {
        Write-Host "Dry run: would create editable MCP configuration: $configPath"
    } else {
        $parent = Split-Path -Parent $configPath
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Copy-Item -LiteralPath $sourcePath -Destination $configPath -Force
        Success-Msg "Created editable MCP configuration: $configPath"
    }
    return $true
}

function Get-ConfigProviderState {
    param(
        [string]$ConfigPath,
        [string]$ConfigTable,
        [string]$ConfigKey,
        [string]$Kind
    )
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    config = tomllib.load(stream)

value = config
for part in sys.argv[2].split("."):
    value = value.get(part, {}) if isinstance(value, dict) else {}

value = value.get(sys.argv[3]) if isinstance(value, dict) else None
if sys.argv[4] == "toggle":
    print("enabled" if value is True else "disabled")
else:
    print("configured" if isinstance(value, list) and bool(value) else "not configured")
'@
    $res = Invoke-PythonScript -ScriptText $script -ScriptArgs @($ConfigPath, $ConfigTable, $ConfigKey, $Kind)
    return $res.Trim()
}

function Write-ConfigApiKey {
    param(
        [string]$ConfigPath,
        [string]$ConfigTable,
        [string]$ConfigKey,
        [string]$SecretValue
    )
    $script = @'
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

path = Path(sys.argv[1])
table = sys.argv[2]
key = sys.argv[3]
secret = sys.stdin.read().rstrip("\r\n")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
header = f"[{table}]"
section_start = next(
    index for index, line in enumerate(lines) if line.strip() == header
)
section_end = next(
    (
        index
        for index in range(section_start + 1, len(lines))
        if lines[index].lstrip().startswith("[")
    ),
    len(lines),
)
key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
replacement = f"{key} = [{json.dumps(secret, ensure_ascii=False)}]\n"
for index in range(section_start + 1, section_end):
    if key_pattern.match(lines[index]):
        lines[index] = replacement
        break
else:
    lines.insert(section_start + 1, replacement)

updated = "".join(lines)
tomllib.loads(updated)
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    stream.write(updated)
    temporary_path = stream.name
os.replace(temporary_path, path)
'@
    [void](Invoke-PythonScriptWithInput -ScriptText $script -ScriptArgs @($ConfigPath, $ConfigTable, $ConfigKey) -StdInText $SecretValue)
}

function Toggle-ConfigBoolean {
    param(
        [string]$ConfigPath,
        [string]$ConfigTable,
        [string]$ConfigKey
    )
    $script = @'
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

path = Path(sys.argv[1])
table = sys.argv[2]
key = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
parsed = tomllib.loads("".join(lines))
value = parsed
for part in table.split("."):
    value = value.get(part, {}) if isinstance(value, dict) else {}
current = value.get(key) is True if isinstance(value, dict) else False
header = f"[{table}]"
section_start = next(
    index for index, line in enumerate(lines) if line.strip() == header
)
section_end = next(
    (
        index
        for index in range(section_start + 1, len(lines))
        if lines[index].lstrip().startswith("[")
    ),
    len(lines),
)
key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
replacement = f"{key} = {'false' if current else 'true'}\n"
for index in range(section_start + 1, section_end):
    if key_pattern.match(lines[index]):
        lines[index] = replacement
        break
else:
    lines.insert(section_start + 1, replacement)

updated = "".join(lines)
tomllib.loads(updated)
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    stream.write(updated)
    temporary_path = stream.name
os.replace(temporary_path, path)
'@
    [void](Invoke-PythonScript -ScriptText $script -ScriptArgs @($ConfigPath, $ConfigTable, $ConfigKey))
}

function Prompt-Secret([string]$Label) {
    Write-Host "Enter the $Label (input hidden): " -NoNewline
    $sec = Read-Host -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    return $plain
}

function Configure-Mcp {
    param([string]$ServerId)
    $configPathArr = Get-McpValue -ServerId $ServerId -Field "config_path"
    if ($configPathArr.Count -eq 0 -or -not $configPathArr[0]) {
        return $true
    }
    $configPath = [System.IO.Path]::GetFullPath((Render-McpTemplate $configPathArr[0]))
    if (-not (Test-Path -LiteralPath $configPath)) {
        return $true
    }

    $providers = Get-McpProviderEntries -ServerId $ServerId
    if ($providers.Count -eq 0) {
        return $true
    }

    while ($true) {
        Write-Host ""
        Write-Host "Configure providers for $ServerId" -ForegroundColor Cyan
        $idx = 1
        $providerData = @()
        foreach ($p in $providers) {
            $parts = $p -split "`t"
            $label = $parts[0]
            $kind = $parts[1]
            $table = $parts[2]
            $key = $parts[3]
            $state = Get-ConfigProviderState -ConfigPath $configPath -ConfigTable $table -ConfigKey $key -Kind $kind
            $action = if ($kind -eq "toggle") { "Toggle" } else { "Set" }
            Write-Host (" {0,2}) {1} {2} (current: {3})" -f $idx, $action, $label, $state)
            $providerData += ,@($label, $kind, $table, $key, $state)
            $idx++
        }
        Write-Host (" {0,2}) Done configuring providers" -f $idx)
        $choice = Read-Host ("Select an option [1-{0}]" -f $idx)

        if ($choice -eq [string]$idx) {
            return $true
        }

        $choiceNum = 0
        if ([int]::TryParse($choice, [ref]$choiceNum) -and $choiceNum -ge 1 -and $choiceNum -lt $idx) {
            $sel = $providerData[$choiceNum - 1]
            $label = $sel[0]
            $kind = $sel[1]
            $table = $sel[2]
            $key = $sel[3]

            if ($kind -eq "toggle") {
                if ($DryRun) {
                    Write-Host "Dry run: would toggle $label in $configPath"
                } else {
                    Toggle-ConfigBoolean -ConfigPath $configPath -ConfigTable $table -ConfigKey $key
                    Success-Msg "Toggled $label"
                }
            } else {
                $secret = Prompt-Secret -Label "$label API key"
                if (-not $secret) {
                    Warning-Msg "No key entered; provider was not updated."
                } elseif ($DryRun) {
                    Write-Host "Dry run: would update $label in $configPath"
                } else {
                    Write-ConfigApiKey -ConfigPath $configPath -ConfigTable $table -ConfigKey $key -SecretValue $secret
                    Success-Msg "Updated $label"
                }
            }
        } else {
            Warning-Msg "Please choose a number from 1 to $idx."
        }
    }
}

function Write-OmpMcp {
    param(
        [string]$ServerId,
        [string]$Transport,
        [string[]]$CommandArgs
    )
    $configPath = Join-Path $TargetHome "mcp.json"
    if (-not (Test-Path -LiteralPath $TargetHome)) {
        New-Item -ItemType Directory -Path $TargetHome -Force | Out-Null
    }

    $script = @'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
server_id = sys.argv[2]
transport = sys.argv[3]
values = sys.argv[4:]
if path.exists():
    config = json.loads(path.read_text(encoding="utf-8"))
else:
    config = {"mcpServers": {}}
servers = config.setdefault("mcpServers", {})
if transport == "url":
    servers[server_id] = {"url": values[0]}
else:
    servers[server_id] = {"command": values[0], "args": values[1:]}

with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    json.dump(config, stream, indent=2)
    stream.write("\n")
    temporary_path = stream.name
os.replace(temporary_path, path)
'@
    $pyArgs = @($configPath, $ServerId, $Transport) + $CommandArgs
    [void](Invoke-PythonScript -ScriptText $script -ScriptArgs $pyArgs)
}

function Install-Mcp {
    param([string]$ServerId)
    $transportArr = Get-McpValue -ServerId $ServerId -Field "transport"
    $transport = if ($transportArr.Count -gt 0) { $transportArr[0] } else { "stdio" }

    if ($transport -eq "url") {
        $urlArr = Get-McpValue -ServerId $ServerId -Field "url"
        $urlTemplate = if ($urlArr.Count -gt 0) { $urlArr[0] } else { "" }
        $url = $urlTemplate
        if ($urlTemplate -match '\{([^{}]+)\}') {
            $placeholder = $Matches[0]
            $labelArr = Get-McpValue -ServerId $ServerId -Field "secret_label"
            $secretLabel = if ($labelArr.Count -gt 0 -and $labelArr[0]) { $labelArr[0] } else { "$ServerId API key" }

            Write-Host "`n$ServerId requires a value for $placeholder."
            Write-Host "1) Paste the API key into this interactive setup"
            Write-Host "2) Skip setup"
            $ans = Read-Host "Select an option [1-2]"
            if ($ans -eq "1") {
                $secretVal = Prompt-Secret -Label $secretLabel
                $url = $url.Replace($placeholder, $secretVal)
            } else {
                Write-Host "Skipped MCP: $ServerId"
                return
            }
        }
        if ($DryRun) {
            Write-Host "Dry run: would install MCP: $ServerId"
            return
        }
        if ($TargetAgent -eq "omp" -or $TargetAgent -eq "freebuff") {
            Write-OmpMcp -ServerId $ServerId -Transport "url" -CommandArgs @($url)
        } else {
            & codex mcp remove $ServerId 2>$null | Out-Null
            & codex mcp add $ServerId --url $url
        }
    } else {
        if (-not (Prepare-McpConfig -ServerId $ServerId)) { return }
        if (-not (Configure-Mcp -ServerId $ServerId)) { return }

        $cmdArr = Get-McpValue -ServerId $ServerId -Field "command"
        $cmdName = if ($cmdArr.Count -gt 0) { Render-McpTemplate $cmdArr[0] } else { "" }
        $argsRaw = Get-McpValue -ServerId $ServerId -Field "args"
        if ($argsRaw.Count -eq 0) {
            Failure-Msg "error: $ServerId has no command arguments in mcp.toml"
            return
        }
        $cmdArgs = @()
        foreach ($arg in $argsRaw) {
            $cmdArgs += (Render-McpTemplate $arg)
        }

        if ($DryRun) {
            Write-Host "Dry run: would install MCP: $ServerId"
            return
        }

        if ($TargetAgent -eq "omp" -or $TargetAgent -eq "freebuff") {
            Write-OmpMcp -ServerId $ServerId -Transport "stdio" -CommandArgs (@($cmdName) + $cmdArgs)
        } else {
            & codex mcp remove $ServerId 2>$null | Out-Null
            & codex mcp add $ServerId -- $cmdName @cmdArgs
        }
    }
    Success-Msg "Installed MCP: $ServerId"
}

function Select-McpIds {
    Require-Config
    $entries = Get-McpEntries
    if ($entries.Count -eq 0) {
        Write-Host "No MCPs are defined in mcp.toml."
        return $null
    }

    Write-Host ""
    Write-Host "Available MCPs from mcp.toml" -ForegroundColor Cyan
    $idx = 1
    $serverIds = @()
    foreach ($entry in $entries) {
        $parts = $entry -split "`t"
        $sid = $parts[0]
        $label = $parts[1]
        $cat = $parts[2]
        $desc = if ($parts.Count -gt 3) { $parts[3] } else { "" }
        Write-Host ("{0,2}) {1} [{2}] - {3}" -f $idx, $label, $cat, $desc)
        $serverIds += $sid
        $idx++
    }
    Write-Host " a) Install all"
    $sel = Read-Host "Select MCP numbers separated by commas, or a"

    if ($sel -match '^[aA]$') {
        return $serverIds
    }

    $chosen = @()
    $tokens = $sel -split ","
    foreach ($tok in $tokens) {
        $t = $tok.Trim()
        $num = 0
        if ([int]::TryParse($t, [ref]$num) -and $num -ge 1 -and $num -le $serverIds.Count) {
            $chosen += $serverIds[$num - 1]
        } else {
            Warning-Msg "Invalid or out of range MCP selection: $t"
            return $null
        }
    }
    return $chosen
}

function Install-Mcps {
    if ($TargetAgent -ne "omp" -and $TargetAgent -ne "droid" -and $TargetAgent -ne "freebuff") {
        Require-Command "codex"
    }
    $ids = Select-McpIds
    if (-not $ids) { return }
    foreach ($sid in $ids) {
        Install-Mcp -ServerId $sid
    }
}

# --- Prompt functions ---

function Get-PromptValue {
    param([string]$PromptId, [string]$Field)
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)
print(catalog.get("prompts", {}).get(sys.argv[2], {}).get(sys.argv[3], ""))
'@
    $res = Invoke-PythonScript -ScriptText $script -ScriptArgs @($PromptsFile, $PromptId, $Field)
    return $res.Trim()
}

function Get-PromptEntries {
    $files = Get-ChildItem -Path $PromptsDir -Filter "*.md" -File -ErrorAction SilentlyContinue
    $entries = @()
    foreach ($f in $files) {
        if ($f.Name -eq "README.md") { continue }
        $promptName = [System.IO.Path]::GetFileNameWithoutExtension($f.Name)
        $label = Get-PromptValue -PromptId $promptName -Field "label"
        if (-not $label) { $label = "$promptName prompt" }
        $entries += ,@($promptName, $label, $f.FullName)
    }
    return $entries
}

function Select-PromptSource {
    [void](Require-Python)
    $entries = Get-PromptEntries
    Write-Host ""
    Write-Host "Choose a prompt" -ForegroundColor Cyan
    Write-Host "Prompts are discovered from prompts/ and described by prompts.toml."

    $idx = 1
    foreach ($e in $entries) {
        $pId = $e[0]
        $label = $e[1]
        $desc = Get-PromptValue -PromptId $pId -Field "description"
        Write-Host ("{0,2}) {1,-24} {2}" -f $idx, $label, $desc)
        $idx++
    }
    Write-Host " b) Back"
    $sel = Read-Host "Select a prompt"

    if ($sel -match '^[bB]$') {
        return $null
    }
    $num = 0
    if ([int]::TryParse($sel, [ref]$num) -and $num -ge 1 -and $num -le $entries.Count) {
        return $entries[$num - 1][2]
    }
    Warning-Msg "Please choose a valid prompt number."
    return $null
}

function Install-Prompt {
    $selectedPath = Select-PromptSource
    if (-not $selectedPath) { return }

    if ($TargetAgent -eq "freebuff") {
        $target = Join-Path $UserHome ".AGENTS.md"
        $promptName = [System.IO.Path]::GetFileNameWithoutExtension($selectedPath)
        $marker = "AIConfig prompt: $promptName"

        if ($DryRun) {
            Write-Host "Preview: would inline $selectedPath into $target"
            return
        }

        if (Test-Path -LiteralPath $target) {
            $existing = Get-Content -LiteralPath $target -Raw
            if ($existing -like "*$marker*") {
                Write-Host "Already installed in $target"
                return
            }
        }

        $content = Get-Content -LiteralPath $selectedPath -Raw
        $block = "`n# $marker`n<!-- installed from $selectedPath by scripts/install.ps1 -->`n`n$content`n"
        Add-Content -LiteralPath $target -Value $block -Encoding UTF8
        Success-Msg "Prompt inlined through $target"
        return
    }

    $target = Join-Path $TargetHome "AGENTS.md"
    # Format line using forward slashes for cross-agent compatibility
    $normalizedPath = $selectedPath.Replace("\", "/")
    $importLine = "@$normalizedPath"

    if ($DryRun) {
        Write-Host "Preview: would link $importLine through $target"
        return
    }

    if (-not (Test-Path -LiteralPath $TargetHome)) {
        New-Item -ItemType Directory -Path $TargetHome -Force | Out-Null
    }

    if (-not (Test-Path -LiteralPath $target)) {
        $initText = "# Global Agent Instructions`n`n$importLine`n"
        Set-Content -LiteralPath $target -Value $initText -Encoding UTF8
    } else {
        $lines = Get-Content -LiteralPath $target
        if ($lines -notcontains $importLine) {
            Add-Content -LiteralPath $target -Value "`n$importLine" -Encoding UTF8
        }
    }
    Success-Msg "Prompt linked through $target"
}

# --- Skill functions ---

function Get-SkillEntries {
    $skillRoot = Join-Path $RepoRoot "skill"
    $dirs = Get-ChildItem -Path $skillRoot -Directory -ErrorAction SilentlyContinue
    $skills = @()
    foreach ($d in ($dirs | Sort-Object Name)) {
        $skillMd = Join-Path $d.FullName "SKILL.md"
        if (Test-Path -LiteralPath $skillMd -PathType Leaf) {
            $skills += $d.Name
        }
    }
    return $skills
}

function Get-SkillGroups {
    $script = @'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

for group_id, group in catalog.get("groups", {}).items():
    label = group.get("label", group_id)
    description = group.get("description", "")
    print(f"{group_id}\t{label}\t{description}")
'@
    $res = Invoke-PythonScript -ScriptText $script -ScriptArgs @($SkillsFile)
    return ($res -split "`r?`n" | Where-Object { $_ -match '\S' })
}

function Get-GroupSkills([string]$GroupId) {
    $script = @'
import sys
import tomllib
from pathlib import Path

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

available = sorted(
    path.name for path in Path(sys.argv[2]).iterdir()
    if path.is_dir() and (path / "SKILL.md").is_file()
)
members = catalog.get("groups", {}).get(sys.argv[3], {}).get("skills", [])
selected = available if "*" in members else [name for name in members if name in available]
for skill in selected:
    print(skill)
'@
    $res = Invoke-PythonScript -ScriptText $script -ScriptArgs @($SkillsFile, (Join-Path $RepoRoot "skill"), $GroupId)
    return ($res -split "`r?`n" | Where-Object { $_ -match '\S' })
}

function Select-IndividualSkills {
    $skills = Get-SkillEntries
    if ($skills.Count -eq 0) {
        Write-Host "No skills with SKILL.md files were found."
        return $null
    }

    Write-Host ""
    Write-Host "Choose individual skills" -ForegroundColor Cyan
    $idx = 1
    foreach ($s in $skills) {
        Write-Host ("{0,2}) {1}" -f $idx, $s)
        $idx++
    }
    Write-Host " a) Install all discovered skills"
    $sel = Read-Host "Select skill numbers separated by commas, or a"

    if ($sel -match '^[aA]$') {
        return $skills
    }

    $chosen = @()
    $tokens = $sel -split ","
    foreach ($tok in $tokens) {
        $t = $tok.Trim()
        $num = 0
        if ([int]::TryParse($t, [ref]$num) -and $num -ge 1 -and $num -le $skills.Count) {
            $chosen += $skills[$num - 1]
        } else {
            Warning-Msg "Invalid or out of range skill selection: $t"
            return $null
        }
    }
    return $chosen
}

function Select-SkillSet {
    [void](Require-Python)
    if (-not (Test-Path -LiteralPath $SkillsFile)) {
        Failure-Msg "error: skill groups not found: $SkillsFile"
        return $null
    }

    $groups = Get-SkillGroups
    if ($groups.Count -eq 0) {
        return Select-IndividualSkills
    }

    Write-Host ""
    Write-Host "Choose a skill set" -ForegroundColor Cyan
    Write-Host "Groups are defined in skills.toml and expand against discovered skill directories."
    $idx = 1
    $groupData = @()
    foreach ($g in $groups) {
        $parts = $g -split "`t"
        $gid = $parts[0]
        $label = $parts[1]
        $desc = if ($parts.Count -gt 2) { $parts[2] } else { "" }
        Write-Host ("{0,2}) {1} - {2}" -f $idx, $label, $desc)
        $groupData += $gid
        $idx++
    }
    Write-Host " i) Choose individual skills"
    Write-Host " b) Back"
    $sel = Read-Host "Select a skill group"

    if ($sel -match '^[iI]$') {
        return Select-IndividualSkills
    }
    if ($sel -match '^[bB]$') {
        return $null
    }

    $num = 0
    if ([int]::TryParse($sel, [ref]$num) -and $num -ge 1 -and $num -le $groupData.Count) {
        $chosenGid = $groupData[$num - 1]
        $skills = Get-GroupSkills -GroupId $chosenGid
        if ($skills.Count -eq 0) {
            Write-Host "The selected group has no available skills."
            return $null
        }
        Write-Host "Selected group: $chosenGid ($($skills.Count) skills)"
        return $skills
    }
    Warning-Msg "Invalid skill group selection."
    return $null
}

function Install-Skills {
    if ($TargetAgent -ne "freebuff") {
        Require-Command "codex"
    }
    $skillsDir = Join-Path $TargetHome "skills"
    $selected = Select-SkillSet
    if (-not $selected) { return }

    if (-not $DryRun -and -not (Test-Path -LiteralPath $skillsDir)) {
        New-Item -ItemType Directory -Path $skillsDir -Force | Out-Null
    }

    foreach ($skill in $selected) {
        $target = Join-Path $skillsDir $skill
        $source = Join-Path (Join-Path $RepoRoot "skill") $skill

        if (Test-Path -LiteralPath $target) {
            $item = Get-Item -LiteralPath $target
            # Check if it's a junction / symlink pointing to source
            if ($item.LinkType -and $item.Target -eq $source) {
                Write-Host "Already linked: $skill"
                continue
            }
            Warning-Msg "Skipped existing non-repository skill: $target"
            continue
        }

        if ($DryRun) {
            Write-Host "Preview: would link skill $skill"
        } else {
            # Use Directory Junction on Windows which does not require admin privileges
            New-Item -ItemType Junction -Path $target -Target $source | Out-Null
            Success-Msg "Linked skill: $skill"
        }
    }
}

# --- OMP configuration functions ---

function Confirm-Overwrite([string]$Prompt) {
    if ($DryRun) { return $true }
    $ans = Read-Host $Prompt
    return ($ans -match '^[yY]$')
}

function Install-OmpConfig {
    if (-not (Test-Path -LiteralPath $OmpConfigDir -PathType Container)) {
        Failure-Msg "OMP settings directory not found: $OmpConfigDir"
        return
    }

    $files = Get-ChildItem -Path $OmpConfigDir -File
    $hasExisting = $false
    foreach ($f in $files) {
        $target = Join-Path $TargetHome $f.Name
        if (Test-Path -LiteralPath $target -PathType Leaf) {
            $hasExisting = $true
            break
        }
    }

    if ($hasExisting -and -not $DryRun) {
        if (-not (Confirm-Overwrite "Overwrite existing OMP files in $TargetHome? [y/N]")) {
            Write-Host "Cancelled applying OMP settings."
            return
        }
    }

    if (-not $DryRun -and -not (Test-Path -LiteralPath $TargetHome)) {
        New-Item -ItemType Directory -Path $TargetHome -Force | Out-Null
    }

    foreach ($f in $files) {
        $target = Join-Path $TargetHome $f.Name
        if ($DryRun) {
            Write-Host "Preview: would install OMP setting $($f.Name)"
        } else {
            Copy-Item -LiteralPath $f.FullName -Destination $target -Force
            Success-Msg "Installed OMP setting: $($f.Name)"
        }
    }

    if (Get-Command omp -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "Preview: would refresh OMP models catalog"
        } else {
            Write-Host "Refreshing OMP models catalog..."
            try {
                & omp models refresh
            } catch {
                Warning-Msg "Failed to refresh OMP models catalog automatically."
            }
        }
    }
}

function Copy-OmpConfig {
    [void](Require-Python)
    $syncScript = Join-Path (Join-Path $RepoRoot "scripts") "sync-omp-config.py"
    if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
        Failure-Msg "sync-omp-config.py not found: $syncScript"
        return
    }

    $py = Require-Python
    $argsList = @($syncScript, "--source", $TargetHome, "--destination", $OmpConfigDir)
    if ($DryRun) {
        $argsList += "--dry-run"
    }

    & $py @argsList
    if ($LASTEXITCODE -eq 0) {
        Success-Msg "Saved non-secret OMP settings into config/omp"
    } else {
        Failure-Msg "Failed to copy OMP settings."
    }
}

# --- Main Menu ---

function Show-Menu {
    while ($true) {
        Draw-Header
        Write-Host "What would you like to set up?"
        Write-Host "  1) Install MCPs        Select from the catalog"
        if ($TargetAgent -eq "omp") {
            Write-Host "  2) Apply saved settings Restore config/omp into ~/.omp/agent"
            Write-Host "  3) Copy existing omp configs Save ~/.omp/agent into config/omp"
            Write-Host "  4) Exit"
            $choice = Read-Host "Select an option [1-4]"
        } else {
            Write-Host "  2) Install a prompt    Choose the global instruction set"
            Write-Host "  3) Install skills      Choose a group or individual skills"
            Write-Host "  4) Exit"
            $choice = Read-Host "Select an option [1-4]"
        }

        switch ($choice) {
            "1" { Install-Mcps }
            "2" {
                if ($TargetAgent -eq "omp") {
                    Install-OmpConfig
                } else {
                    Install-Prompt
                }
            }
            "3" {
                if ($TargetAgent -eq "omp") {
                    Copy-OmpConfig
                } else {
                    Install-Skills
                }
            }
            "4" { return }
            default { Warning-Msg "Please choose one of the displayed options." }
        }
    }
}

if ($TargetAgent -eq "omp") {
    $configFile = Join-Path $TargetHome "config.yml"
    if (-not (Test-Path -LiteralPath $configFile)) {
        Install-OmpConfig
    }
}

Show-Menu
