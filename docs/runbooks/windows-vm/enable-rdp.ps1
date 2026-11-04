# Remote Desktop for the rocky Windows VM, reachable over Tailscale only.
$ErrorActionPreference = 'Stop'

# RDP on, with Network Level Authentication.
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -Value 1

# Firewall: the built-in Remote Desktop rules, enabled but scoped to the Tailscale CGNAT range.
Get-NetFirewallRule -Group '@FirewallAPI.dll,-28752' | ForEach-Object {
  Set-NetFirewallRule -InputObject $_ -Enabled True -Profile Any
  Set-NetFirewallAddressFilter -InputObject (Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $_) -RemoteAddress '100.64.0.0/10'
}

# An RDP logon takes the console session away from the autologon desktop; on disconnect it would stay
# disconnected (no display), breaking winrun's UI automation. Hand it back to the console on every
# RDP disconnect (LocalSessionManager event 24), as SYSTEM (tscon needs it).
$cmd = 'Start-Sleep 2; $s = (Get-Process explorer -ErrorAction SilentlyContinue | Where-Object SI -ne 0 | Select-Object -First 1).SI; if ($s) { tscon.exe $s /dest:console }'
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -EncodedCommand $enc"
$cim = Get-CimClass -Namespace ROOT\Microsoft\Windows\TaskScheduler -ClassName MSFT_TaskEventTrigger
$trigger = New-CimInstance -CimClass $cim -ClientOnly
$trigger.Enabled = $true
$trigger.Subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"><Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[(EventID=24)]]</Select></Query></QueryList>'
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskPath '\viberpit-host\' -TaskName 'rdp-return-to-console' -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null

"rdp=" + (Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections
"nla=" + (Get-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp').UserAuthentication
Get-NetFirewallRule -Group '@FirewallAPI.dll,-28752' | ForEach-Object { "$($_.DisplayName) enabled=$($_.Enabled) remote=$((Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $_).RemoteAddress)" }
"task=" + (Get-ScheduledTask -TaskPath '\viberpit-host\' -TaskName 'rdp-return-to-console').State
"listening=" + [bool](Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue)
