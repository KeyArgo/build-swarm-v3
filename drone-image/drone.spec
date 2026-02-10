{
  "spec_version": 1,
  "description": "Build Swarm v3 — Minimal Drone Image Specification",
  "updated": "2026-02-09",

  "profile": "default/linux/amd64/23.0",
  "arch": "amd64",
  "accept_keywords": "amd64",

  "world_packages": [
    "sys-apps/portage",
    "sys-devel/gcc",
    "sys-devel/binutils",
    "sys-libs/glibc",
    "dev-lang/python",
    "net-misc/rsync",
    "net-misc/openssh",
    "app-misc/screen",
    "app-portage/gentoolkit",
    "sys-apps/openrc"
  ],

  "forbidden_patterns": [
    "kde-plasma/*",
    "kde-apps/*",
    "x11-base/xorg-server",
    "x11-wm/*",
    "gnome-base/*",
    "www-client/firefox",
    "www-client/chromium",
    "www-client/google-chrome",
    "app-containers/docker",
    "app-containers/containerd",
    "app-emulation/qemu",
    "app-emulation/libvirt",
    "games-*/*"
  ],

  "max_packages": 400,
  "warn_packages": 350,

  "required_commands": [
    "emerge",
    "portageq",
    "rsync",
    "ssh",
    "python3",
    "ps",
    "pgrep",
    "pkill",
    "screen",
    "equery"
  ],

  "required_services": [
    "swarm-drone",
    "sshd"
  ],

  "required_dirs": [
    "/opt/build-swarm",
    "/etc/build-swarm",
    "/var/log/build-swarm",
    "/var/cache/binpkgs",
    "/var/cache/distfiles"
  ],

  "required_files": [
    "/etc/build-swarm/drone.conf",
    "/etc/portage/make.conf",
    "/etc/portage/package.use/swarm-drone"
  ],

  "make_conf_required_features": [
    "buildpkg",
    "fail-clean"
  ],

  "network": {
    "v3_control_plane_port": 8100,
    "v2_gateway_port": 8090,
    "ssh_port": 22
  }
}
