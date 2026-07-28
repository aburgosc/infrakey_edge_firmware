$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$files = Get-ChildItem -Path $root -Recurse -File -Include *.py

foreach ($file in $files) {
    $text = [System.IO.File]::ReadAllText($file.FullName)
    [System.IO.File]::WriteAllText($file.FullName, $text, $utf8NoBom)
    Write-Output ("normalized: " + $file.FullName)
}

Write-Output "done"
