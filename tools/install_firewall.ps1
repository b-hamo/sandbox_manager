<#
Secure CUA Sandbox Manager - Host firewall setup. Run ONCE in an administrator PowerShell.

  powershell -ExecutionPolicy Bypass -File tools\install_firewall.ps1 -PythonExe "C:\...\Python313\python.exe"

What it does
  1. Creates 4 inbound rules on the Sandbox switch adapter `vEthernet (Default Switch)`:
       allow TCP 17443 (control WSS) and 17444 (screenshot HTTPS PUT) for the Host Python only,
       block every other TCP port and every UDP port except DNS(53)/DHCP(67,68) from the Sandbox.
  2. Registers a scheduled task that runs as SYSTEM from boot and keeps those rules bound to the
     CURRENT adapter.

Why the task (poc/sandbox-launch README, 2026-09-29): Windows stores a rule's interface as the adapter
GUID. Every reboot recreates `vEthernet (Default Switch)` with a new GUID - and only when the first
Sandbox starts - so all four rules silently stop applying. The task notices within seconds and rebinds.

Security: the task's command is embedded in the task itself (-EncodedCommand). There is no script
file a normal user could edit to get SYSTEM code execution. Rules are created DISABLED and without an
interface; the task binds them to the switch and enables them in the same call, so a block rule is never
active on the LAN or Wi-Fi adapter.

Remove everything with tools\uninstall_firewall.ps1.
#>
param(
    [Parameter(Mandatory = $true)][string]$PythonExe
)
$ErrorActionPreference = 'Stop'

$Prefix   = 'SCRP PoC'                       # must match sandbox_manager/firewall.py RULE_PREFIX
$Alias    = 'vEthernet (Default Switch)'
$TaskName = 'SecureCUA Sandbox Firewall Rebind'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this in an administrator PowerShell.'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or ([IO.Path]::GetFileName($PythonExe) -ne 'python.exe')) {
    throw "PythonExe must be the Host python.exe (the base interpreter, not a WindowsApps alias): $PythonExe"
}
if ($PythonExe -like '*\WindowsApps\*') { throw 'The Microsoft Store python alias cannot be used in a firewall rule.' }
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path

# 1. Rules: replace any previous set so the script can be re-run safely.
Get-NetFirewallRule -DisplayName "$Prefix*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$common = @{ Direction = 'Inbound'; Profile = 'Any'; Enabled = 'False' }
New-NetFirewallRule @common -DisplayName "$Prefix 17443 from Sandbox" -Action Allow -Protocol TCP -LocalPort 17443 -Program $PythonExe | Out-Null
New-NetFirewallRule @common -DisplayName "$Prefix 17444 from Sandbox" -Action Allow -Protocol TCP -LocalPort 17444 -Program $PythonExe | Out-Null
New-NetFirewallRule @common -DisplayName "$Prefix block Sandbox to Host TCP" -Action Block -Protocol TCP -LocalPort '1-17442','17445-65535' | Out-Null
New-NetFirewallRule @common -DisplayName "$Prefix block Sandbox to Host UDP" -Action Block -Protocol UDP -LocalPort '1-52','54-66','69-65535' | Out-Null

# 2. Watcher: every 5 s, if the switch adapter exists and any rule is not bound to it (or disabled),
#    bind it to the current adapter and enable it. Cheap: two cmdlets while nothing changes.
$watch = @"
`$ErrorActionPreference = 'SilentlyContinue'
while (`$true) {
  if (Get-NetAdapter -Name '$Alias') {
    foreach (`$r in @(Get-NetFirewallRule -DisplayName '$Prefix*')) {
      `$bound = @((`$r | Get-NetFirewallInterfaceFilter).InterfaceAlias) -contains '$Alias'
      if (-not `$bound -or `$r.Enabled -ne 'True') { Set-NetFirewallRule -InputObject `$r -InterfaceAlias '$Alias' -Enabled True }
    }
  }
  Start-Sleep -Seconds 5
}
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($watch))

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$task     = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $task `
    -Description 'Keeps the Secure CUA Sandbox firewall rules bound to the current vEthernet (Default Switch) adapter.' | Out-Null
Start-ScheduledTask -TaskName $TaskName

if (Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue) {
    # The task's first PowerShell start takes a few seconds; wait until it has bound the rules.
    for ($i = 0; $i -lt 30; $i++) {
        $pending = @(Get-NetFirewallRule -DisplayName "$Prefix*" | Where-Object { $_.Enabled -ne 'True' })
        if ($pending.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
}
Write-Host "Rules:"
Get-NetFirewallRule -DisplayName "$Prefix*" | ForEach-Object {
    $i = @(($_ | Get-NetFirewallInterfaceFilter).InterfaceAlias) -join ','
    Write-Host ("  {0,-38} enabled={1,-5} {2,-5} iface={3}" -f $_.DisplayName, $_.Enabled, $_.Action, $i)
}
Write-Host "Task: $((Get-ScheduledTask -TaskName $TaskName).State)"
if (-not (Get-NetAdapter -Name $Alias -ErrorAction SilentlyContinue)) {
    Write-Host "Note: '$Alias' does not exist yet (normal right after a reboot). The rules turn on by themselves a few seconds after the first Sandbox starts."
}
