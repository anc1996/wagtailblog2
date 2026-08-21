[CmdletBinding()]
param(
    [string]$Version = '0.3.15-test.1'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $repositoryRoot 'wagtailblog3\static\vendor\Script\downlaod_markdown.js'
$outputDirectory = Join-Path $repositoryRoot 'output\userscript-blog-import'
$outputPath = Join-Path $outputDirectory 'downlaod_markdown.blog-import-test.user.js'
$infoPath = Join-Path $outputDirectory 'build-info.json'

if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Userscript source is missing: $sourcePath"
}

$source = Get-Content -LiteralPath $sourcePath -Raw -Encoding utf8
$metadataMarker = '// ==/UserScript=='
$metadataEnd = $source.IndexOf($metadataMarker)
if ($metadataEnd -lt 0) {
    throw 'Userscript metadata terminator is missing.'
}

$metadata = $source.Substring(0, $metadataEnd)
$body = $source.Substring($metadataEnd + $metadataMarker.Length)
$metadata = $metadata -replace '(?m)^// @name\s+.*$', '// @name         [TEST] Markdown Blog Import Preview'
$metadata = $metadata -replace '(?m)^// @namespace\s+.*$', '// @namespace    https://wagtailblog.local/userscript-test'
$metadata = $metadata -replace '(?m)^// @version\s+.*$', "// @version      $Version"
$metadata = $metadata -replace '(?m)^// @description\s+.*$', '// @description  Test copy: preview and unpublished blog draft creation only.'
$body = $body -replace '(?m)^// @(downloadURL|updateURL|require)\s+.*\r?\n?', ''
$body = $body -replace '(?m)^// ==/UserScript==\r?\n?', ''
# TEST 副本必须隔离配置，避免固定测试地址覆盖正式脚本保存的站点信息

if (-not $body.Contains("const blogConfigKey = 'zuihuitao.blogImport.v1';")) {
    throw 'Userscript config key is missing.'
}
if (-not $body.Contains("siteUrl: '',")) {
    throw 'Userscript default site URL is missing.'
}
$body = $body.Replace(
    "const blogConfigKey = 'zuihuitao.blogImport.v1';",
    "const blogConfigKey = 'zuihuitao.blogImport.test.v1';"
)
$body = $body.Replace("siteUrl: '',", "siteUrl: 'http://127.0.0.1:8080',")
$sentinelLines = @(
    ';(() => {',
    '    const target = document.body || document.documentElement;',
    "    if (!target || document.getElementById('zuihuitao-blog-import-test-sentinel')) return;",
    "    const notice = document.createElement('div');",
    "    notice.id = 'zuihuitao-blog-import-test-sentinel';",
    "    notice.setAttribute('role', 'status');",
    "    notice.textContent = 'TEST userscript executed';",
    "    notice.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;padding:8px 12px;border:1px solid #166534;border-radius:6px;background:#f0fdf4;color:#166534;font:14px/1.5 system-ui,sans-serif';",
    '    target.append(notice);',
    '})();',
    ''
)
$sentinel = [string]::Join("`n", $sentinelLines)
$testSource = $metadata.TrimEnd() + "`r`n$metadataMarker`r`n" + $sentinel + $body

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
Set-Content -LiteralPath $outputPath -Value $testSource -Encoding utf8

$hash = (Get-FileHash -LiteralPath $outputPath -Algorithm SHA256).Hash.ToLowerInvariant()
$head = (& git -C $repositoryRoot rev-parse HEAD).Trim()
$info = [ordered]@{
    source = (Resolve-Path -LiteralPath $sourcePath).Path
    output = (Resolve-Path -LiteralPath $outputPath).Path
    version = $Version
    git_head = $head
    sha256 = $hash
    generated_at = (Get-Date).ToString('o')
}
$info | ConvertTo-Json | Set-Content -LiteralPath $infoPath -Encoding utf8

Write-Output "Test userscript generated: $outputPath"
Write-Output "SHA256: $hash"
