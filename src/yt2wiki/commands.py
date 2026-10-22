"""Command-literal recognition: precise, conservative, domain-aware."""

from __future__ import annotations

import re

# Programs that plausibly begin a reproducible instruction.  This list is
# deliberately broad across shells, build systems, package managers, editors,
# media tools, hardware tools and infrastructure CLIs, because a technical
# tutorial is overwhelmingly about running one of these.
EXECUTABLE_HINTS: frozenset[str] = frozenset({
    # shells / core utils
    "ls", "cd", "pwd", "cp", "mv", "rm", "mkdir", "rmdir", "ln", "touch", "cat",
    "less", "more", "head", "tail", "find", "grep", "egrep", "fgrep", "sed",
    "awk", "gawk", "xargs", "sort", "uniq", "wc", "cut", "tr", "tee", "paste",
    "comm", "diff", "patch", "file", "stat", "du", "df", "tree", "basename",
    "dirname", "realpath", "readlink", "mktemp", "install", "rsync", "split",
    "md5sum", "sha1sum", "sha256sum", "cksum", "base64", "xxd", "od", "hexdump",
    # process / environment
    "ps", "pgrep", "pkill", "kill", "killall", "top", "htop", "nice", "renice",
    "nohup", "setsid", "timeout", "time", "watch", "jobs", "bg", "fg", "env",
    "export", "unset", "alias", "unalias", "source", "eval", "set", "read",
    "printf", "echo", "test", "true", "false", "yes", "seq", "clear", "reset",
    "history", "type", "command", "exec", "ulimit", "umask", "locale", "date",
    "cal", "sleep", "who", "whoami", "id", "groups", "users", "w", "last",
    "hostname", "uname", "arch", "nproc", "free", "vmstat", "iostat", "mpstat",
    "uptime", "dmesg", "lsof", "fuser", "strace", "ltrace", "gdb",
    # archiving / text
    "tar", "gzip", "gunzip", "bzip2", "xz", "zstd", "zip", "unzip", "7z", "7za",
    "cpio", "dd", "openssl", "gpg", "gpg2", "age", "jq", "yq", "python", "perl",
    # network
    "ssh", "scp", "sftp", "rsync", "curl", "wget", "http", "https", "nc",
    "netcat", "nmap", "ping", "ping6", "traceroute", "tracepath", "mtr", "dig",
    "nslookup", "host", "whois", "ip", "ifconfig", "iwconfig", "iw", "ss",
    "netstat", "route", "arp", "ethtool", "tcpdump", "tshark", "iptables",
    "nftables", "ufw", "firewall-cmd", "socat", "telnet", "ftp", "smbclient",
    "mount", "umount", "showmount", "exportfs", "ssh-keygen", "ssh-copy-id",
    "ssh-add", "ssh-agent", "openvpn", "wg", "wg-quick", "resolvconf",
    # privileged / system
    "sudo", "doas", "su", "visudo", "passwd", "chsh", "chfn", "useradd",
    "usermod", "userdel", "groupadd", "groupmod", "groupdel", "gpasswd",
    "adduser", "addgroup", "chown", "chgrp", "chmod", "chattr", "lsattr",
    "getfacl", "setfacl", "swapon", "swapoff", "sysctl", "lsmod", "modprobe",
    "insmod", "rmmod", "depmod", "lspci", "lsusb", "lscpu", "lsblk", "blkid",
    "blockdev", "hdparm", "sdparm", "smartctl", "fdisk", "sfdisk", "gdisk",
    "parted", "partprobe", "kpartx", "mkfs", "mkfs.ext4", "mkswap", "fsck",
    "e2fsck", "resize2fs", "tune2fs", "wipefs", "cryptsetup", "lvcreate",
    "vgcreate", "vgchange", "pvs", "vgs", "lvs", "systemctl", "journalctl",
    "service", "chkconfig", "update-rc.d", "timedatectl", "localectl",
    "hostnamectl", "loginctl", "udevadm", "grub-mkconfig", "update-grub",
    "mkinitcpio", "dracut", "shutdown", "reboot", "poweroff", "halt", "init",
    "telinit", "runlevel", "apk", "apt", "apt-get", "apt-cache", "dpkg",
    "dpkg-reconfigure", "snap", "flatpak", "yum", "dnf", "rpm", "zypper",
    "pacman", "makepkg", "emerge", "xbps-install", "brew", "port", "pkg",
    "nix", "nix-env", "nixos-rebuild", "guix", "aptitude", "add-apt-repository",
    "ppa-purge", "needrestart", "update-alternatives",
    # hardware / maker
    "esptool", "esptool.py", "idf.py", "avrdude", "avr-gcc", "avr-objcopy",
    "openocd", "st-flash", "st-util", "pio", "platformio", "arduino-cli",
    "gpio", "raspi-config", "i2cdetect", "i2cget", "i2cset", "i2cdump",
    "gpiodetect", "wiringpi", "pinctrl", "vcgencmd", "v4l2-ctl", "ffmpeg",
    "ffprobe", "avconv", "sox", "play", "arecord", "aplay", "speaker-test",
    "pactl", "amixer", "alsamixer", "espeak", "espeak-ng", "festival", "flite",
    "gpiod", "libcamera-still", "libcamera-vid", "raspistill", "raspivid",
    "candump", "cansend", "ipmitool", "sensors", "lm-sensors",
    # editors / pagers
    "vim", "vi", "nvim", "nano", "pico", "emacs", "ed", "joe", "micro", "kak",
    "helix", "hx", "code", "code-insiders", "codium", "subl", "sublime_text",
    "gedit", "kate", "notepad", "bat", "batcat",
    # VCS / build
    "git", "hg", "svn", "bzr", "fossil", "jj", "make", "gmake", "cmake",
    "ctest", "cpack", "ninja", "meson", "scons", "bazel", "buck", "please",
    "autoconf", "automake", "autoreconf", "libtoolize", "configure", "bootstrap",
    "gcc", "g++", "cc", "c++", "clang", "clang++", "ld", "ar", "objdump",
    "readelf", "nm", "strip", "rustc", "cargo", "rustup", "go", "gomod",
    "javac", "java", "jar", "mvn", "gradle", "kotlinc", "scalac", "sbt",
    "swiftc", "dotnet", "mono", "msbuild",
    # languages / package managers
    "node", "nodejs", "deno", "bun", "npm", "npx", "yarn", "pnpm", "tsc",
    "ts-node", "esbuild", "vite", "webpack", "rollup", "pip", "pip3", "pipx",
    "poetry", "pipenv", "uv", "rye", "hatch", "conda", "mamba", "micromamba",
    "python2", "python3", "pypy", "ruby", "gem", "bundle", "bundler", "perl",
    "php", "composer", "lua", "luarocks", "R", "Rscript", "julia", "elixir",
    "mix", "erl", "rebar3", "ghc", "cabal", "stack", "ocaml", "dune", "opam",
    # infrastructure
    "docker", "docker-compose", "podman", "buildah", "skopeo", "nerdctl",
    "kubectl", "helm", "kustomize", "minikube", "kind", "k3d", "oc", "istioctl",
    "k9s", "stern", "terraform", "tofu", "terragrunt", "ansible",
    "ansible-playbook", "ansible-galaxy", "ansible-vault", "vagrant", "packer",
    "aws", "gcloud", "az", "doctl", "linode-cli", "hcloud", "scaleway",
    "vultr-cli", "gh", "glab", "act", "circleci", "flux", "argocd", "kustomize",
    "etcdctl", "consul", "vault", "nomad", "boundary", "waypoint",
    # misc daemons seen in tutorials
    "nginx", "caddy", "httpd", "apache2", "apachectl", "postgres", "psql",
    "pg_ctl", "pg_dump", "pg_restore", "mysql", "mariadb", "mysqld",
    "mongo", "mongosh", "redis-cli", "redis-server", "memcached", "rabbitmqctl",
    "celery", "supervisord", "pm2", "systemd-analyze", "crond", "crontab",
    "at", "batch", "anacron", "logrotate", "fail2ban-client", "certbot",
    "openssl",
})

_VERSION_SUFFIX = re.compile(r"[-.]\d+(\.\d+)*$")


_WRAPPERS = ("sudo", "doas", "env", "nohup", "timeout", "command", "stdbuf", "nice")


def program_name(command: str) -> str:
    """First token of a command line, unwrapping shell wrappers and prompts."""
    text = command.strip()
    while text[:1] in "$#>~":
        text = text[1:].lstrip()
    parts = text.split()
    while len(parts) > 1 and parts[0].lower() in _WRAPPERS:
        parts = parts[1:]
    return parts[0] if parts else ""


def is_executable(command: str) -> bool:
    """True when the command starts with a known program.

    Matching is exact, or on the version-stripped base name
    (``python3.11`` -> ``python3``), never substring containment: substring
    containment is what previously made "Run: ssh ..." and "Open a file for
    editing using nano" look like commands.
    """
    name = program_name(command).lower()
    if not name:
        return False
    if name in EXECUTABLE_HINTS:
        return True
    base = _VERSION_SUFFIX.sub("", name)
    return base != name and base in EXECUTABLE_HINTS
