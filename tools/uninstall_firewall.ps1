<#
Removes what tools\install_firewall.ps1 created. Run in an administrator PowerShell.
  powershell -ExecutionPolicy Bypass -File tools\uninstall_firewall.ps1
#>
$ErrorActionPreference = 'Stop'
$Prefix   = 'SCRP PoC'
$TaskName = 'SecureCUA Sandbox Firewall Rebind'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this in an administrator PowerShell.'
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task removed: $TaskName"
}
$rules = @(Get-NetFirewallRule -DisplayName "$Prefix*" -ErrorAction SilentlyContinue)
$rules | Remove-NetFirewallRule
Write-Host "Rules removed: $($rules.Count)"
