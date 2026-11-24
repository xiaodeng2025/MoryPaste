param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,
    [string]$CompilerPath = 'E:\Inno Setup 7\ISCC.exe'
)

$ErrorActionPreference = 'Stop'
$windows = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $windows
$python = Join-Path $windows '.venv\Scripts\python.exe'
$template = Join-Path $PSScriptRoot 'MoryPaste-0.6.3.iss'
$bootstrapper = Join-Path $PSScriptRoot 'vendor\MicrosoftEdgeWebview2Setup.exe'
$output = Join-Path $project "artifacts\installer-$Version"
$payload = Join-Path $output 'payload'
$build = Join-Path $output 'build'
$receiverDist = Join-Path $build 'receiver-dist'

$versions = @('desktop_settings.py', 'receiver.py') | ForEach-Object {
    [regex]::Match([IO.File]::ReadAllText((Join-Path $windows $_)), '(?m)^APP_VERSION = "([^"]+)"').Groups[1].Value
}
if ($versions | Where-Object { $_ -ne $Version }) { throw 'Source versions do not match the requested installer version.' }
if (Test-Path -LiteralPath $output) { throw "Refusing to overwrite existing output: $output" }
foreach ($path in @($python, $CompilerPath, $template, $bootstrapper)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing build input: $path" }
}
$signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Microsoft Corporation') {
    throw 'The bundled WebView2 bootstrapper must have a valid Microsoft signature.'
}

New-Item -ItemType Directory -Path $output | Out-Null
$env:SENDTOCOPYQ_BUILD_TEST = ''
& $python -m PyInstaller --noconfirm --clean --distpath $payload --workpath $build (Join-Path $PSScriptRoot 'SendToCopyQ.spec')
if ($LASTEXITCODE -ne 0) { throw "Desktop build exited with code $LASTEXITCODE." }
& $python -m PyInstaller --noconfirm --clean --distpath $receiverDist --workpath $build (Join-Path $PSScriptRoot 'SendToCopyQ-Receiver.spec')
if ($LASTEXITCODE -ne 0) { throw "Receiver build exited with code $LASTEXITCODE." }

$desktop = Join-Path $payload 'SendToCopyQ'
$receiver = Join-Path $receiverDist 'SendToCopyQ-Receiver'
Copy-Item -LiteralPath $receiver -Destination (Join-Path $desktop 'receiver') -Recurse
foreach ($file in @('SendToCopyQ.exe', 'receiver\SendToCopyQ-Receiver.exe', '_internal\desktop.js', 'receiver\_internal\desktop.js')) {
    if (-not (Test-Path -LiteralPath (Join-Path $desktop $file) -PathType Leaf)) { throw "Incomplete payload: $file" }
}

& $CompilerPath '--quiet-progress' "--define=ReleaseVersion=$Version" "--define=ArtifactDir=$output\" "--define=PayloadDir=$payload\" $template
if ($LASTEXITCODE -ne 0) { throw "Inno Setup exited with code $LASTEXITCODE." }

$installer = Get-Item -LiteralPath (Join-Path $output "MoryPaste-$Version-Setup.exe")
$hash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $($installer.Name)" | Set-Content -LiteralPath (Join-Path $output 'SHA256SUMS.txt') -Encoding ascii
$installer | Select-Object FullName, Length
Write-Output $hash
