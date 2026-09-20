[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgsList
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallPy = Join-Path $ScriptDir "install.py"

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $InstallPy @ArgsList
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py $InstallPy @ArgsList
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 $InstallPy @ArgsList
} else {
    Write-Error "Python 3 is required to run install.py."
    exit 1
}
