---
name: repair-debian-opencl
description: Diagnose and repair Debian 12 APT failures caused by invalid repositories, duplicate sources, or missing dpkg-owned Perl modules, then install and verify Intel OpenCL GPU access. Use when apt reports broken installs or needrestart Perl errors such as missing NeedRestart.pm or Module/Find.pm, when an Ubuntu Docker feed is incorrectly paired with bookworm, or when Intel OpenCL and render-group access need configuration.
---

# Repair Debian OpenCL

Inspect before changing the system. Confirm Debian 12, capture the complete `apt-get update` or package-trigger failure, inspect relevant files in `/etc/apt/sources.list{,.d}`, and use `dpkg -V` on implicated packages. Do not treat a missing Perl module as an instruction to install from CPAN; restore the Debian package that owns it.

Use `scripts/repair_debian_opencl.sh USER` when the evidence matches this workflow. The script:

- Disables the exact known-invalid Ubuntu Docker/bookworm source and exact duplicate Palantir backports entry through recoverable `.disabled` renames.
- Finds installed packages with missing payloads under `/usr/share/perl5` and reinstalls the affected set with `needrestart` in one transaction.
- Completes interrupted dpkg configuration and fixes dependency state.
- Installs `intel-opencl-icd`, the OpenCL loader, and `clinfo`.
- Adds the target user to `render` and `video`, verifies repaired package payloads, and runs `clinfo -l` as that user.

Run the script only with explicit authorization to repair the host. It requires root, leaves nonmatching source configurations untouched, and refuses unsupported operating systems, absent Intel render devices, invalid users, and overwriting existing disabled-source backups. If local policy prohibits privileged execution, give the user the single command to run:

```bash
sudo /absolute/path/to/scripts/repair_debian_opencl.sh USER
```

After success, tell the user to log out and back in so the current login session receives its new group membership. Explain that OpenCL availability alone does not make PyTorch or TensorFlow use the GPU; the training framework must support Intel GPU/XPU acceleration.
