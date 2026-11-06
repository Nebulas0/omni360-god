param(
  [string]$XdkPath = $env:XEDK,
  [string]$SigningKeyPath = $env:OMNI360_SIGNING_KEY_FILE
)
$ErrorActionPreference = 'Stop'
if (-not $XdkPath -or -not (Test-Path $XdkPath)) {
  throw 'Install the official Xbox 360 XDK and set XEDK. A GitHub token or Linux Docker image cannot replace it.'
}
if (-not $SigningKeyPath -or -not (Test-Path $SigningKeyPath)) {
  throw 'Set OMNI360_SIGNING_KEY_FILE to the private key matching src/UpdateKey.h. Never put the private key in the repository.'
}
$env:XEDK = $XdkPath
$MSBuild = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\MSBuild.exe'
if (-not (Test-Path $MSBuild)) { throw 'MSBuild 4.0 / Visual Studio 2010 is required.' }
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
  & $MSBuild Omni360.sln /m /t:Rebuild /p:Configuration=Release '/p:Platform=Xbox 360'
  if ($LASTEXITCODE -ne 0) { throw 'Xbox native build failed. No release was produced.' }
  if (-not (Test-Path 'Release\Omni360.xex')) { throw 'Build did not produce Omni360.xex.' }
  & python tools/sign_release.py Release/Omni360.xex --key $SigningKeyPath
  if ($LASTEXITCODE -ne 0) { throw 'Release signing/verification failed.' }
  Get-FileHash Release/Omni360.xex -Algorithm SHA256
} finally { Pop-Location }
