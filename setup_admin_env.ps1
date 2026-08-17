$ErrorActionPreference = "Stop"

# Admin-side publish setup for the UMaT timetable builder.
# Creates/updates .env at the project root with the student-app URL and a
# shared publish secret, and prints the secret so it can be pasted into the
# student app's Vercel project (env var TIMETABLE_PUBLISH_SECRET).

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $root ".env"

$urlKey = "STUDENT_APP_URL"
$secretKey = "STUDENT_APP_PUBLISH_SECRET"

$vars = @{}
if (Test-Path -LiteralPath $envPath) {
    Get-Content -LiteralPath $envPath | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $vars[$matches[1]] = $matches[2]
        }
    }
}

if (-not $vars.ContainsKey($secretKey) -or -not $vars[$secretKey]) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $vars[$secretKey] = -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

$vars[$urlKey] = "https://umat-student-app.vercel.app"

$lines = @()
foreach ($k in ($vars.Keys | Sort-Object)) {
    $lines += "$k=$($vars[$k])"
}
Set-Content -LiteralPath $envPath -Value $lines -Encoding ASCII

Write-Host ""
Write-Host "Admin .env written: $envPath"
Write-Host ""
Write-Host "SECRET (set this on Vercel too):"
Write-Host "  TIMETABLE_PUBLISH_SECRET=$($vars[$secretKey])"
Write-Host ""
Write-Host "Steps:"
Write-Host "  1. On Vercel (umat-student-app project): add env var"
Write-Host "     TIMETABLE_PUBLISH_SECRET = the secret above, then redeploy."
Write-Host "  2. Restart this admin server (close run_web.bat and start it again)."
Write-Host "  3. Open http://127.0.0.1:8000, log in, and click Publish for sem1 and sem2."
Write-Host ""
