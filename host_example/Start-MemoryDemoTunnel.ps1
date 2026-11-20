param(
    [Parameter(Mandatory)][string]$TunnelClient,
    [Parameter(Mandatory)][string]$Python,
    [string]$DemoManifest
)

$ErrorActionPreference = 'Stop'
$tunnelBin = (Resolve-Path -LiteralPath $TunnelClient).Path
$pythonBin = (Resolve-Path -LiteralPath $Python).Path.Replace('\', '/')
$server = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../memory_mcp.py')).Path.Replace('\', '/')
$serverCommand = '"{0}" -B "{1}"' -f $pythonBin, $server
if ($DemoManifest) {
    $manifest = (Resolve-Path -LiteralPath $DemoManifest).Path.Replace('\', '/')
    $serverCommand += ' --demo-manifest "{0}"' -f $manifest
}

$tunnelId = (Read-Host '请粘贴隧道 ID').Trim()
if ($tunnelId -cnotmatch '^tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}$') {
    throw '隧道 ID 的格式不正确。请完整复制平台显示的 ID。'
}
$secret = Read-Host '请粘贴运行密钥（输入隐藏）' -AsSecureString
if ($secret.Length -eq 0) {
    throw '运行密钥不能为空。'
}
$previousKey = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'Process')
try {
    $env:CONTROL_PLANE_API_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
    $options = @(
        '--control-plane.base-url=https://api.openai.com'
        "--control-plane.tunnel-id=$tunnelId"
        '--control-plane.api-key=env:CONTROL_PLANE_API_KEY'
        "--mcp.command=$serverCommand"
        '--health.listen-addr=127.0.0.1:0'
    )
    & $tunnelBin doctor @options --explain
    if ($LASTEXITCODE -ne 0) { throw '官方隧道检查失败。请查看上方说明。' }
    & $tunnelBin run @options
    if ($LASTEXITCODE -ne 0) { throw '官方隧道已异常退出。请查看上方说明。' }
} finally {
    [Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', $previousKey, 'Process')
    $secret.Dispose()
}
