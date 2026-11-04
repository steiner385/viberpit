# Windows VM on a session runner

How to give a Linux session runner a Windows 11 desktop that sessions can drive, so issues labelled
`env: windows` are dispatched to it. This records what was **actually built on rocky on 2026-11-03**,
gotchas included. If a command here disagrees with the live host, the host wins; fix this doc.

Design: [`docs/superpowers/specs/2026-11-03-host-platforms-windows-vm-design.md`](../superpowers/specs/2026-11-03-host-platforms-windows-vm-design.md) §4–§5.
What sessions are told: [`windows-vm-session-guide.md`](windows-vm-session-guide.md).
Templates: [`windows-vm/`](windows-vm/) (`autounattend.xml`, `setup.ps1`, `vmtype.py`).

**End state:** libvirt domain `win11` (q35, UEFI Secure Boot, swtpm TPM 2.0, 4 vCPU, 8 GB, 100 GB
virtio qcow2, NAT network `default`, autostarted). Windows 11 Pro, activated via the host's own OEM
key. Local admin `viber`, auto-logged-on, desktop never sleeps or locks. OpenSSH with key-only login,
PowerShell as the default shell, reachable from the runner as `ssh win11`. Python 3.12 + Git for
Windows. An internal snapshot `clean` of the logged-on desktop. `winrun` / `winsync` on the runner's
`PATH`, and the daemon advertising the `windows` platform.

All commands run on the runner (rocky) as the daemon user (`tony`) unless they say `sudo`.

## 1. Prerequisites

- A CPU with VT-x/AMD-V enabled in firmware (`grep -cE 'vmx|svm' /proc/cpuinfo` > 0) and ~16 GB RAM
  to spare beyond the runner's own sessions. rocky: OptiPlex 7050, i7-7700 (4c/8t), 32 GB, Ubuntu 26.04.
- The runner's firmware carries a Windows OEM key: `ls /sys/firmware/acpi/tables/MSDM` exists.
  Without it you need your own licence (see §12).
- Packages:

  ```bash
  sudo apt install qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients virtinst \
    swtpm swtpm-tools ovmf dnsmasq-base genisoimage
  sudo virsh net-autostart default && sudo virsh net-start default   # NAT 192.168.122.0/24
  ```

  Ubuntu 26.04 runs the modular daemons (`virtqemud`); use `sudo virsh` throughout. docker, ufw and
  tailscale coexist with it on rocky without changes.

## 2. Installation media

- **Windows 11 ISO.** Microsoft's download page refuses scripted requests ("Sentinel marked this
  request as rejected"), so get the link in a real browser: open
  <https://www.microsoft.com/software-download/windows11>, choose the multi-edition ISO, English
  (United States), 64-bit, and copy the download link. It expires after 24 h. Fetch it on the runner:

  ```bash
  sudo curl -fL -o /var/lib/libvirt/images/Win11_x64.iso '<link from the browser>'
  ```

- **virtio drivers:**

  ```bash
  sudo curl -fL -o /var/lib/libvirt/images/virtio-win.iso \
    https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso
  ```

## 3. Secrets (they live only on the runner)

```bash
install -d -m 0700 ~/.config/viberpit
( umask 077; openssl rand -base64 18 > ~/.config/viberpit/win11-vm.secret )   # the viber password
ssh-keygen -t ed25519 -N '' -C win11 -f ~/.ssh/win11_ed25519                    # the VM login key
```

Never print or paste either file. Sessions are told not to read them, and `winrun` never prints them
(design call 6: an accepted risk, since sessions already run as this user).

## 4. Firmware licence table (MSDM)

```bash
sudo sh -c 'cat /sys/firmware/acpi/tables/MSDM > /var/lib/libvirt/images/rocky-msdm.bin'
sudo chown root:kvm /var/lib/libvirt/images/rocky-msdm.bin
sudo chmod 0640 /var/lib/libvirt/images/rocky-msdm.bin
```

It is passed into the VM as a raw ACPI table (§6), which is where Windows looks for the OEM key.

## 5. Build the unattend ISO

`autounattend.xml` installs Windows 11 Pro unattended (bypassing the CPU check, since the i7-7700 is
below Windows 11's list; TPM and Secure Boot are real), creates `viber` with auto-logon, and runs
`setup.ps1` at first logon. `setup.ps1` installs the virtio guest tools, OpenSSH with key-only login
and PowerShell as default shell, the firewall fix (gotcha 1), and the never-sleep/no-lock settings.
Both carry `__PASSWORD__` / `__PUBKEY__` placeholders; fill them into a private directory without
echoing the secret:

```bash
work=$(mktemp -d) && chmod 0700 "$work"
cp docs/runbooks/windows-vm/autounattend.xml docs/runbooks/windows-vm/setup.ps1 "$work"/
python3 - "$work" <<'EOF'
import pathlib, sys
d = pathlib.Path(sys.argv[1])
pw = (pathlib.Path.home() / ".config/viberpit/win11-vm.secret").read_text().strip()
pub = (pathlib.Path.home() / ".ssh/win11_ed25519.pub").read_text().strip()
for name in ("autounattend.xml", "setup.ps1"):
    p = d / name
    p.write_text(p.read_text().replace("__PASSWORD__", pw).replace("__PUBKEY__", pub))
EOF
sudo genisoimage -quiet -J -r -V UNATTEND -o /var/lib/libvirt/images/win11-unattend.iso "$work"
sudo chmod 0600 /var/lib/libvirt/images/win11-unattend.iso
rm -rf "$work"
```

(The generated password is base64, so it needs no XML escaping. A hand-picked one containing `&` or
`<` would.) The unattend ISO holds the password in plain text: keep it root-only, and see §7.

## 6. Create the VM

```bash
sudo virt-install --name win11 --osinfo win11 --memory 8192 --vcpus 4 --cpu host-passthrough --machine q35 \
  --boot firmware=efi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=yes,firmware.feature1.name=enrolled-keys,firmware.feature1.enabled=yes \
  --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
  --disk path=/var/lib/libvirt/images/win11.qcow2,size=100,bus=virtio,format=qcow2,discard=unmap \
  --disk /var/lib/libvirt/images/Win11_x64.iso,device=cdrom,bus=sata,boot.order=1 \
  --disk /var/lib/libvirt/images/virtio-win.iso,device=cdrom,bus=sata \
  --disk /var/lib/libvirt/images/win11-unattend.iso,device=cdrom,bus=sata \
  --network network=default,model=virtio --graphics vnc,listen=127.0.0.1 --video virtio \
  --xml ./os/acpi/table=/var/lib/libvirt/images/rocky-msdm.bin --xml ./os/acpi/table/@type=raw \
  --autostart --noautoconsole --wait 0

# Get past "Press any key to boot from CD or DVD": one Enter a second for ~12 s.
for _ in $(seq 12); do sudo virsh send-key win11 KEY_ENTER >/dev/null; sleep 1; done
```

Setup loads the virtio storage and network drivers from the virtio ISO (the `DriverPaths` cover
drive letters D–F). **The install takes about 30 minutes**, unattended, with several reboots. To
watch it: `sudo virsh vncdisplay win11` (`:0` = port 5900 on 127.0.0.1), then from your workstation
`ssh -L 5900:127.0.0.1:5900 rocky` and point a VNC viewer at `localhost:5900`. It is done when the
desktop shows `viber` logged on and `C:\viberpit-setup.done` exists (the log is `C:\viberpit-setup.log`).

## 7. Network: DHCP reservation and the ssh alias

Pin the VM's address so the ssh alias never moves:

```bash
mac=$(sudo virsh domiflist win11 | awk '/network/ {print $5}')
sudo virsh net-update default add ip-dhcp-host \
  "<host mac='$mac' name='win11' ip='192.168.122.76'/>" --live --config
```

The reservation applies from the VM's next lease; reboot it (`sudo virsh reboot win11`) if it already
holds another address. Then add to `~/.ssh/config` on the runner:

```
Host win11
  HostName 192.168.122.76
  User viber
  IdentityFile ~/.ssh/win11_ed25519
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 5
  LogLevel ERROR
```

Check: `ssh win11 '$PSVersionTable.PSVersion.ToString(); whoami'` prints a 5.1 version and `win11-rocky\viber`.

**Gotcha 1: port 22 times out.** The OpenSSH capability's built-in firewall rule allows only the
Private profile, and the NAT network comes up as Public. `setup.ps1` now marks the network Private
**and** widens the rule to `-Profile Any` (either alone did not survive a later reclassification). If
ssh still times out, use the console (§11) to run:
`Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private; Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -Profile Any`.

Once ssh works, detach the unattend ISO, which holds the password in plain text, and delete it.
This hardening step was not done in the 2026-11-03 build.

```bash
sudo virsh domblklist win11        # find the target (e.g. sdc) whose source is win11-unattend.iso
sudo virsh change-media win11 sdc --eject --config --live
sudo shred -u /var/lib/libvirt/images/win11-unattend.iso
```

## 8. Python and Git for Windows

Match the CI image (`windows-latest`, Python 3.12). In an ssh session (`ssh win11`, PowerShell):

```powershell
$py = "$env:TEMP\python-3.12.10-amd64.exe"
Invoke-WebRequest https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe -OutFile $py
Start-Process $py -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1' -Wait

# Git for Windows: take the current 64-bit installer URL from https://git-scm.com/downloads/win
# (2.56 on 2026-11-03).
$git = "$env:TEMP\git-setup.exe"
Invoke-WebRequest '<Git-2.xx.x-64-bit.exe URL>' -OutFile $git
Start-Process $git -ArgumentList '/VERYSILENT','/NORESTART' -Wait
```

Open a **new** ssh session (PATH is read at logon) and check `python --version` and `git --version`.

## 9. qcow2 nvram, then the `clean` snapshot

**Gotcha 2:** internal snapshots fail on this VM out of the box: "internal snapshots of a VM with
pflash based firmware require QCOW2 nvram format". Convert the UEFI variable store once, with the VM
shut off:

```bash
sudo virsh shutdown win11        # wait until `sudo virsh domstate win11` says "shut off"
cd /var/lib/libvirt/qemu/nvram
sudo qemu-img convert -f raw -O qcow2 win11_VARS.fd win11_VARS.qcow2
sudo virsh edit win11            # in <os>: <nvram format="qcow2">/var/lib/libvirt/qemu/nvram/win11_VARS.qcow2</nvram>
sudo virsh start win11
```

Wait until `viber` is auto-logged-on and `ssh win11 exit` succeeds, then snapshot the **running**
VM, so the snapshot includes memory and restores straight to the logged-on desktop:

```bash
sudo virsh snapshot-create-as win11 clean \
  --description "logged-on desktop, OpenSSH, Python 3.12.10, Git for Windows"
sudo virsh snapshot-list win11
```

## 10. Helpers, guide and daemon settings

From a checkout of this repo on the runner:

```bash
sudo install -m 0755 scripts/winvm/winrun.py  /usr/local/bin/winrun
sudo install -m 0755 scripts/winvm/winsync.py /usr/local/bin/winsync
sudo install -m 0644 scripts/winvm/_winvm.py  /usr/local/bin/_winvm.py   # shared module both import
sudo install -D -m 0644 docs/runbooks/windows-vm-session-guide.md /etc/viberpit/windows-guide.md
```

Re-run these after any change to `scripts/winvm/` or the guide; deploys do not install them. Sessions
find the helpers through the daemon's `PATH`
(it already lists `/usr/local/bin`: check with `systemctl show viberpit-daemon -p Environment` or the daemon process environment), so no `PATH` change is
needed. Check with `which winrun winsync` as the daemon user.

In that same daemon env file:

```bash
VIBERPIT_HOST_PLATFORMS=linux,windows
VIBERPIT_HOST_PLATFORM_GUIDE_WINDOWS=/etc/viberpit/windows-guide.md
# Only if the ssh alias is not "win11". The daemon's reachability check and winrun/winsync
# (which read it from the session's environment) must agree:
# VIBERPIT_WINDOWS_VM_SSH_HOST=win11
```

Then restart the daemon, **only when the Sessions page shows nothing mid-turn**: a restart touches
every live session (`deploy/README.md` § "Stopping / restarting the daemon"):

```bash
sudo systemctl restart viberpit-daemon.service   # blocks while in-flight turns drain; expected
```

The daemon advertises `windows` on `/info` only while `ssh win11 exit` succeeds (re-checked at most
every 60 s), so a stopped VM quietly takes the runner out of `env: windows` dispatch.

**Verify:**

```bash
winrun -- python -c "import ctypes; print(ctypes.windll.user32.GetForegroundWindow() != 0)"   # True
bash scripts/winvm/smoke.sh     # full live smoke: sync, desktop, exit code, timeout, --ps, cleanup
```

`GET /api/fleet` should list `windows` among the runner's `platforms`.

## 11. Operating it

- **Restore to clean** (a session left the desktop dirty, or Windows misbehaves). Hold the desktop lock
  so no `winrun` is mid-command, and revert to the running snapshot:

  ```bash
  flock -w 900 ~/.cache/viberpit/winvm.lock sudo virsh snapshot-revert win11 clean --running
  ```

  Synced trees under `C:\work\` are gone afterwards; sessions recreate them with `winsync`. The guest
  clock resumes from snapshot time; if it lags, `ssh win11 'w32tm /resync /force'`.
- **Refresh `clean`** after installing tooling: `sudo virsh snapshot-delete win11 clean`, then
  re-create it as in §9.
- **Who holds the desktop:** `flock -n ~/.cache/viberpit/winvm.lock true && echo free || cat
  ~/.cache/viberpit/winvm.lock`. The file keeps the last holder's pid after release, so `cat` alone
  does not tell you whether the lock is held.
- **Leftover tasks:** each `winrun` unregisters its task under `\viberpit\` and removes
  `C:\winrun\<id>`, even on timeout or Ctrl-C; the next `winrun` sweeps anything a crashed run left, killing
  the orphaned command by its recorded pid first. Each task also carries an execution time limit of
  `--timeout` plus 300 s as a VM-side backstop.
- **Console rescue (gotcha 3).** There is no guest-agent channel (osinfo added no virtio-serial
  device), so when ssh is down, type into the console with `docs/runbooks/windows-vm/vmtype.py`,
  which drives `virsh send-key` with a US layout:

  ```bash
  python3 docs/runbooks/windows-vm/vmtype.py win11 'powershell' --enter
  ```

  A UAC prompt's "Yes" is `sudo virsh send-key win11 KEY_LEFT` then `KEY_ENTER`. Watch the result over VNC (§6).

## 12. Licensing

Windows is activated with the **OEM licence of this same machine**: the runner's firmware MSDM
table, passed through as a raw ACPI table (§4, §6), so Windows 11 Pro activates
(`LicenseStatus=1`, checked with `Get-CimInstance SoftwareLicensingProduct | ? PartialProductKey`).
`autounattend.xml` holds only Microsoft's generic Pro install key, which selects the edition and does
not activate. This is the host's own licence used in a VM on that same hardware. Do not copy the
MSDM blob, the disk image or the snapshot to any other machine; a VM on other hardware needs its own
licence.
