# Prepare the shared inbox for prepare(inbox=...): a folder the Agent may write into, where nothing may run
# on the Host. Normal (non-admin) PowerShell is enough: it only changes the ACL of this new folder.
#
#   powershell -ExecutionPolicy Bypass -File tools\setup_inbox.ps1 [-Work <folder>]
#
# <Work> (default %USERPROFILE%\SecureCUA\codex-work) is the Agent's whole writable folder; <Work>\inbox is
# the part mapped into the Sandbox. On every file created anywhere under <Work>:
#   Everyone: DENY execute            -> the Host refuses to run it ("Access is denied"); the Guest only
#                                        reads it, copies it and runs its own copy
#   OWNER RIGHTS: read, write, delete -> whoever creates a file does not get to change its permissions
#                                        (so the deny cannot be removed by the file's creator)
param([string]$Work = (Join-Path $env:USERPROFILE 'SecureCUA\codex-work'))
$ErrorActionPreference = 'Stop'
$inbox = Join-Path $Work 'inbox'
New-Item -ItemType Directory -Force $inbox | Out-Null
foreach ($name in 'Documents','Desktop','Downloads','OneDrive','AppData') {
  $user = Join-Path $env:USERPROFILE $name
  if ($Work.TrimEnd('\').StartsWith($user, [StringComparison]::OrdinalIgnoreCase)) { throw "refusing a folder inside $user" }
}
& icacls $Work /deny '*S-1-1-0:(OI)(IO)(X)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls deny failed ($LASTEXITCODE)" }
& icacls $Work /grant '*S-1-3-4:(OI)(CI)(R,W,D)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls grant failed ($LASTEXITCODE)" }

# Prove it: a copy of a harmless system program must not run from there.
$probe = Join-Path $inbox 'setup_probe_whoami.exe'
Copy-Item "$env:SystemRoot\System32\whoami.exe" $probe -Force
$blocked = $false
try { & $probe | Out-Null } catch { $blocked = $true }
Remove-Item $probe -Force
if (-not $blocked) { throw "a program in $inbox still runs on the Host; the deny-execute ACE did not apply" }
"OK: $Work is writable, nothing under it runs on the Host; $inbox is the folder to map"
