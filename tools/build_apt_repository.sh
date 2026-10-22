#!/usr/bin/env bash
# Build and verify one flat, signed APT snapshot. Requires Ubuntu packaging tools.
set -euo pipefail
umask 077

if [[ $# != 2 ]]; then
  echo "Usage: $0 COLLECTED_DIRECTORY NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
collected=$(realpath "$1")
output=$(realpath -m "$2")
[[ -d "$collected" && ! -e "$output" ]]
shopt -s nullglob
packages=("$collected/"*.deb)
[[ ${#packages[@]} -gt 0 && -f "$collected/manifest.json" ]]
declare -A identities
for package in "${packages[@]}"; do
  name=$(dpkg-deb -f "$package" Package)
  [[ "$name" =~ ^[a-z0-9][a-z0-9+.-]+$ ]]
  [[ $(dpkg-deb -f "$package" Architecture) == all ]]
  [[ -z "${identities[$name]+present}" ]]
  identities[$name]=1
done
: "${APT_SIGNING_PRIVATE_KEY:?Missing signing key}"
: "${APT_SIGNING_KEY_FINGERPRINT:?Missing primary fingerprint}"
fingerprint=${APT_SIGNING_KEY_FINGERPRINT^^}
[[ "$fingerprint" =~ ^[A-F0-9]{40}$ ]]
[[ "$APT_SIGNING_PRIVATE_KEY" == *'-----BEGIN PGP PRIVATE KEY BLOCK-----'* ]]

scratch=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/masternaka-apt.XXXXXX")
trap 'rm -rf "$scratch"' EXIT
export GNUPGHOME="$scratch/gnupg"
mkdir -m 700 "$GNUPGHOME"
printf '%s\n' "$APT_SIGNING_PRIVATE_KEY" | gpg --batch --quiet --import
unset APT_SIGNING_PRIVATE_KEY
# Match the single primary secret key, not an arbitrary signing subkey.
actual=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1=="sec" {primary=1; next} primary && $1=="fpr" {print $10; primary=0}')
[[ "$actual" == "$fingerprint" ]]
printf '%s' "${APT_SIGNING_PASSPHRASE:-}" > "$scratch/passphrase"
chmod 600 "$scratch/passphrase"
unset APT_SIGNING_PASSPHRASE

mkdir -p "$output/pool"
chmod 755 "$output" "$output/pool"
cp "${packages[@]}" "$output/pool/"
cp "$collected/manifest.json" "$output/manifest.json"
chmod 644 "$output/pool/"*.deb
(
  cd "$output"
  apt-ftparchive packages pool > Packages
  gzip -n -c Packages > Packages.gz
  # Keep Release outside the tree being hashed.
  apt-ftparchive -o APT::FTPArchive::Release::Origin=Masternaka \
    -o APT::FTPArchive::Release::Label=Masternaka \
    -o APT::FTPArchive::Release::Architectures=all \
    -o APT::FTPArchive::Release::Description='Masternaka selected applications' \
    release . > "$scratch/Release"
)
cp "$scratch/Release" "$output/Release"
gpg --batch --yes --armor --export "$fingerprint" > "$output/masternaka-archive-keyring.asc"
gpg --batch --yes --export "$fingerprint" > "$scratch/keyring.gpg"
for mode in clearsign detach-sign; do
  destination=InRelease
  [[ "$mode" != detach-sign ]] || destination=Release.gpg
  gpg --batch --yes --pinentry-mode loopback --passphrase-file "$scratch/passphrase" \
    --local-user "$fingerprint" --digest-algo SHA256 --armor \
    --output "$output/$destination" --"$mode" "$output/Release"
done
cat > "$output/index.html" <<EOF_HTML
<!doctype html>
<html lang="fr">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dépôt APT Masternaka</title>
<h1>Dépôt APT Masternaka</h1>
<p>Applications sélectionnées par Masternaka. Ce dépôt propose leur paquet courant.</p>
<p><a href="masternaka-archive-keyring.asc">Clé publique de signature</a> ·
<a href="Packages">Index des paquets</a> · <a href="manifest.json">Provenance des paquets</a></p>
<p>Empreinte primaire : <code>$fingerprint</code>.</p>
<p>Comparez cette empreinte à celle du mainteneur dans un canal de confiance avant installation.</p>
<p><a href="https://github.com/Masternaka/masternaka-repository#installation-sur-linux-mint">Guide de configuration et d’installation</a></p>
</html>
EOF_HTML
find "$output" -type f -exec chmod 644 {} +
# Verify both signatures with only the exported public key.
gpgv --keyring "$scratch/keyring.gpg" "$output/InRelease"
gpgv --keyring "$scratch/keyring.gpg" "$output/Release.gpg" "$output/Release"

# Exercise APT's own Signed-By, index checksums and package download in isolation.
mkdir -p "$scratch/apt/lists/partial" "$scratch/apt/cache/archives/partial" "$scratch/download"
touch "$scratch/apt/status"
printf 'deb [signed-by=%s] file:%s ./\n' "$scratch/keyring.gpg" "$output" > "$scratch/apt/sources.list"
apt_options=(
  -o "Dir::Etc::sourcelist=$scratch/apt/sources.list"
  -o Dir::Etc::sourceparts=- -o Dir::Etc::parts=- -o Dir::Etc::main=/dev/null
  -o "Dir::State=$scratch/apt" -o "Dir::State::status=$scratch/apt/status"
  -o "Dir::Cache=$scratch/apt/cache" -o "APT::Sandbox::User=$(id -un)"
  -o APT::Get::List-Cleanup=0 -o APT::Update::Error-Mode=any
)
apt-get "${apt_options[@]}" update
(
  cd "$scratch/download"
  for package in "${packages[@]}"; do
    name=$(dpkg-deb -f "$package" Package)
    version=$(dpkg-deb -f "$package" Version)
    mkdir "$name"
    (
      cd "$name"
      apt-get "${apt_options[@]}" download "$name=$version"
      cmp "$package" ./*.deb
    )
  done
)
echo "Signed repository and isolated APT download verified: $output"
