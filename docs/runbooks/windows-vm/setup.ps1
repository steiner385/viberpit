# First-logon setup for the ViberPit Windows VM on rocky. Idempotent; log at C:\viberpit-setup.log.
Start-Transcript -Path C:\viberpit-setup.log -Append
$ErrorActionPreference = 'Continue'

# virtio guest tools (drivers incl. display + qemu guest agent)
foreach ($d in 'D','E','F','G') {
  $exe = "${d}:\virtio-win-guest-tools.exe"
  if (Test-Path $exe) { Start-Process $exe -ArgumentList '/install','/passive','/norestart' -Wait; break }
}

# OpenSSH server, PowerShell as the default shell, key-only admin login from rocky
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force
# The capability's built-in rule allows the Private profile only, and a fresh NAT network is
# classed Public — so mark the network Private AND widen the rule (either alone was not enough
# to survive a later reclassification).
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
if (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue) {
  Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Profile Any
} else {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any
}
$keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
Set-Content -Path $keys -Value '__PUBKEY__' -Encoding ascii
icacls.exe $keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F'
$cfg = 'C:\ProgramData\ssh\sshd_config'
(Get-Content $cfg) -replace '^#?PasswordAuthentication .*', 'PasswordAuthentication no' | Set-Content $cfg
Restart-Service sshd

# A desktop that never sleeps, locks, or reboots under a logged-on user (UI automation needs it live)
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization' /v NoLockScreen /t REG_DWORD /d 1 /f
reg add 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' /v InactivityTimeoutSecs /t REG_DWORD /d 0 /f
reg add 'HKCU\Control Panel\Desktop' /v ScreenSaveActive /t REG_SZ /d 0 /f
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f

Set-Content -Path C:\viberpit-setup.done -Value (Get-Date -Format o)
Stop-Transcript
