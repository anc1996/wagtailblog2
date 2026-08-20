param(
    [string]$OutputDirectory = "output/markdown-import-exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$output = Join-Path $projectRoot $OutputDirectory
$build = Join-Path $output "build"
$dist = Join-Path $output "dist"

New-Item -ItemType Directory -Force -Path $output | Out-Null
& py -3 -m PyInstaller --clean --noconfirm --distpath $dist --workpath $build "$PSScriptRoot\markdown_import_gui.spec"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed. Install PyInstaller in the Windows Python environment."
}
Write-Host ("EXE generated: " + (Join-Path $dist "markdown-importer.exe"))
