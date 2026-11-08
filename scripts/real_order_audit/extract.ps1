# READ-ONLY evidence extractor for one bot dir. Placeholder __BOT__ / __ROOT__ replaced by caller.
$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$d = Join-Path '__ROOT__' '__BOT__'
$ev = [regex]'REAL ORDER|REAL FILL|ENTRY_FILL|TRADE_CLOSE|Order skipped|Deploying live bot|Trading mode restored|FORCE CLOSE RETRY|fill confirmed'
$ts = [regex]'^(?:\[LIVE\] )?\[(\d{4}-\d\d-\d\d \d\d)'
$hours = New-Object 'System.Collections.Generic.HashSet[string]'
$seen = New-Object 'System.Collections.Generic.HashSet[string]'
$files = Get-ChildItem $d -File | Where-Object { $_.Name -like 'debug_*.log' -or $_.Name -like 'cli_stdout.log*' } | Sort-Object Name
foreach ($f in $files) {
  $fs = [IO.File]::Open($f.FullName, 'Open', 'Read', 'ReadWrite,Delete')
  $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
  while (($line = $sr.ReadLine()) -ne $null) {
    $m = $ts.Match($line); if ($m.Success) { [void]$hours.Add($m.Groups[1].Value) }
    if ($ev.IsMatch($line)) { if ($seen.Add($line)) { 'E|' + $f.Name + '|' + $line } }
  }
  $sr.Close(); $fs.Close()
}
foreach ($h in $hours) { 'H|' + $h }
$sp = Join-Path $d 'session.json'
$fs = [IO.File]::Open($sp, 'Open', 'Read', 'ReadWrite,Delete'); $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
$raw = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
'S|' + ($raw -replace "`r?`n", ' ')
$dp = Join-Path $d 'decisions.csv'
if (Test-Path $dp) { Select-String -Path $dp -Pattern 'REAL_|ORDER' -Encoding UTF8 | ForEach-Object { 'D|' + $_.Line } }
