#!/bin/sh
set -eu
umask 022

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
VERSION=$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$ROOT/appector/__init__.py")

if [ -z "$VERSION" ]; then
    echo "Could not read __version__ from appector/__init__.py" >&2
    exit 1
fi
case "$VERSION" in
    [0-9]*)
        case "$VERSION" in
            *[!0-9A-Za-z.+:~-]*)
                echo "Application version contains characters unsafe for a Debian version." >&2
                exit 1
                ;;
        esac
        ;;
    *)
        echo "Application version must begin with a digit." >&2
        exit 1
        ;;
esac
dpkg --validate-version "$VERSION"

SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct HEAD 2>/dev/null || true)}
case "$SOURCE_DATE_EPOCH" in
    ''|*[!0-9]*)
        echo "Set SOURCE_DATE_EPOCH or build from a Git checkout to create a reproducible package." >&2
        exit 1
        ;;
esac
export SOURCE_DATE_EPOCH
CHANGELOG_DATE=$(date -Ru -d "@$SOURCE_DATE_EPOCH")

PACKAGE="appector_${VERSION}_all"
STAGING="$ROOT/build/$PACKAGE"

rm -rf "$STAGING"
mkdir -p \
    "$STAGING/DEBIAN" \
    "$STAGING/usr/lib" \
    "$STAGING/usr/bin" \
    "$STAGING/usr/share/doc/appector" \
    "$STAGING/usr/share/applications" \
    "$STAGING/usr/share/icons" \
    "$STAGING/usr/share/metainfo" \
    "$STAGING/usr/share/polkit-1/actions" \
    "$STAGING/usr/share/man/man1"

cp -R "$ROOT/appector" "$STAGING/usr/lib/"
find "$STAGING/usr/lib/appector" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGING/usr/lib/appector" -type f -name '*.pyc' -delete
rm -f "$STAGING/usr/lib/appector/window.txt"

cat > "$STAGING/DEBIAN/control" <<EOF
Package: appector
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Elshad Guliyev <ell-shad@users.noreply.github.com>
Homepage: https://github.com/ell-shad/appector
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-gdkpixbuf-2.0, libadwaita-1-0 (>= 1.5), pkexec | policykit-1
Recommends: python3-apt, shared-mime-info
Suggests: appstream-util
Description: GTK application manager for Linux
 Manage installed applications and software sources with a GTK desktop app.
 .
 Appector lists software across APT, Snap, Flatpak, AppImage and manual
 installs. Destructive actions run through a preview and a dedicated
 polkit-authorized helper (appector-helper).
EOF

cat > "$STAGING/usr/bin/appector" <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/lib${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m appector "$@"
EOF
chmod 0755 "$STAGING/usr/bin/appector"

cp -R "$ROOT/assets/icons/hicolor" "$STAGING/usr/share/icons/"

# Privileged helper (ADR-0003): root-owned, executable, and the polkit
# action definitions that authorize each operation by name.
install -m 0755 "$ROOT/helper/appector-helper" \
    "$STAGING/usr/lib/appector/appector-helper"
install -m 0644 "$ROOT/helper/com.appector.appector.policy" \
    "$STAGING/usr/share/polkit-1/actions/com.appector.appector.policy"

install -m 0644 "$ROOT/assets/metainfo/com.appector.appector.metainfo.xml" \
    "$STAGING/usr/share/metainfo/com.appector.appector.metainfo.xml"

cp "$ROOT/debian/copyright" "$STAGING/usr/share/doc/appector/copyright"
cat > "$STAGING/usr/share/doc/appector/changelog" <<EOF
appector ($VERSION) unstable; urgency=medium

  * Initial packaged release candidate.

 -- Elshad Guliyev <ell-shad@users.noreply.github.com>  $CHANGELOG_DATE
EOF
gzip -9n "$STAGING/usr/share/doc/appector/changelog"

cat > "$STAGING/usr/share/man/man1/appector.1" <<'EOF'
.TH APPECTOR 1
.SH NAME
appector \- manage installed Linux applications
.SH SYNOPSIS
.B appector
.SH DESCRIPTION
Appector is a GTK desktop application for inspecting installed software and
performing package-management operations. Some actions can remove packages or
files; review each preview and confirmation before proceeding.
.SH FILES
.TP
.I ~/.local/state/appector/actions.log
Per-user activity log (rotated; older copies actions.log.1-3).
.TP
.I ~/.local/state/appector/purge-backups/
Private backups of APT conffiles made before residual-configuration purges.
.TP
.I /usr/lib/appector/appector-helper
Privileged helper executed through pkexec; one polkit action per operation
(see com.appector.appector.policy).
.SH NOTES
Package operations use the system package managers and may request
authentication. Appector does not automatically install its own updates.
.SH SEE ALSO
.BR apt (8),
.BR flatpak (1),
.BR snap (8)
EOF

cat > "$STAGING/usr/share/applications/com.appector.appector.desktop" <<'EOF'
[Desktop Entry]
Name=Appector
Comment=Manage installed applications
Comment[de]=Installierte Anwendungen verwalten
Exec=appector %F
Icon=com.appector.appector
Terminal=false
Type=Application
Categories=System;PackageManager;
Keywords=package;manager;install;uninstall;remove;apt;snap;flatpak;appimage;cleanup;
MimeType=application/vnd.debian.binary-package;application/vnd.flatpak.ref;application/x-appimage;application/x-executable;
StartupNotify=true
X-GNOME-UsesNotifications=false
EOF

gzip -9n "$STAGING/usr/share/man/man1/appector.1"

cat > "$STAGING/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ -d /usr/share/applications ]; then
    update-desktop-database -q /usr/share/applications || true
fi
if [ -d /usr/share/mime ]; then
    update-mime-database /usr/share/mime || true
fi
exit 0
POSTINST
chmod 0755 "$STAGING/DEBIAN/postinst"

cat > "$STAGING/DEBIAN/triggers" <<'TRIGGERS'
interest-noawait /usr/share/applications
interest-noawait /usr/share/mime
interest-noawait /usr/share/icons
TRIGGERS

find "$STAGING" -type d -exec chmod 0755 {} +
find "$STAGING" -type f -exec chmod 0644 {} +
chmod 0755 "$STAGING/usr/bin/appector"
chmod 0755 "$STAGING/usr/lib/appector/appector-helper"
chmod 0755 "$STAGING/DEBIAN/postinst"
find "$STAGING" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

mkdir -p "$ROOT/dist"
dpkg-deb --build --root-owner-group "$STAGING" "$ROOT/dist/$PACKAGE.deb"
printf 'Built %s\n' "$ROOT/dist/$PACKAGE.deb"
