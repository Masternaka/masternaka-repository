# masternaka-repository

Dépôt APT signé central des applications choisies par Masternaka. La liste
autorisée est définie dans `apps.json` ; elle contient MintTweaks (`all`). Helium et Zen utilisent désormais leurs
sources officielles et ne sont plus collectés ni distribués par ce dépôt.
Il ne s’agit pas d’un miroir de tous les logiciels Linux Mint.

Le workflow peut publier un dépôt APT plat à l’adresse
`https://masternaka.github.io/masternaka-repository/`. Cette option est désactivée tant que
la variable `APT_REPOSITORY_ENABLED` ne vaut pas exactement `true`.
Les GitHub Releases continuent de fonctionner indépendamment de cette option.

Le workflow vérifie les tests à chaque push et pull request vers `main`.
La publication peut être déclenchée par push sur `main`, manuellement ou toutes
les six heures. Le déclenchement planifié peut être retardé par GitHub ; vérifiez
les paramètres Actions si GitHub suspend les exécutions planifiées pour inactivité.
Aucune clé ni secret de signature n’est utilisé pour les tests de pull request.

À chaque publication, le collecteur consulte la dernière release **standard**
(non brouillon, non préversion) de chaque application autorisée, puis exige
exactement l’asset correspondant à sa recette, avec le digest SHA-256 fourni par
GitHub : un fichier `nom-du-paquet_VERSION_ARCHITECTURE.deb` pour la recette
`deb` par défaut. Il vérifie le téléchargement, le nom Debian, l’architecture
configurée (`all` ou `amd64`) et la version interne du paquet. Une application manquante, un digest absent
ou une erreur font échouer l’ensemble : aucun dépôt partiel n’est publié.

La collecte a lieu dans le verrou de publication, afin de lire les releases
actuelles plutôt que de déployer un ancien artefact. Les publications sont
sérialisées ; GitHub peut remplacer une ancienne exécution en attente par une
plus récente. Les commits qui ne sont plus la tête de `main` ne sont pas déployés.
Le dépôt conserve uniquement le paquet courant de chaque application ; les
anciens paquets restent dans les Releases de leurs dépôts d’origine. Le fichier
public `manifest.json` expose dépôt source, tag, recette, version, nom d’asset,
URL et digest amont. Son objet `deb` indique séparément nom, version et SHA-256
du paquet réellement distribué.

Ce dépôt distribue des instantanés de développement. La vérification des
signatures et le téléchargement APT ne remplacent pas un essai réel de chaque
application sous Linux Mint. Aucune installation cliente n’est déclenchée par CI.

## Installer les fichiers du dépôt central

Copiez ce projet dans le dépôt GitHub **Masternaka/masternaka-repository** sur la
branche `main`, en conservant les éventuels fichiers existants utiles, dont
`.gitattributes`. Les fichiers n’incluent ni clé réelle ni configuration secrète.
La publication est désactivée par défaut. Exécutez les vérifications avant envoi :

```bash
python3 -B -m unittest discover -s tests -v
bash -n tools/build_apt_repository.sh
```

Le script de signature requiert Bash 4 ou ultérieur (fourni par Ubuntu ; le Bash 3
système de macOS ne suffit pas pour son exécution). L’intégration de signature
et de téléchargement APT nécessite Ubuntu 24.04 avec
`apt-utils`, `gnupg` et `dpkg-deb` ; elle s’exécute dans Actions avant déploiement
une fois la configuration ci-dessous activée.

## Préparer la clé de signature

Effectuez cette opération sur votre propre ordinateur. Ne créez ni ne stockez
une clé privée dans le dépôt Git. Sur macOS, installez d’abord GnuPG si `gpg`
n’est pas disponible, par exemple `brew install gnupg` si vous utilisez Homebrew.
Sur Linux Mint : `sudo apt install gnupg`.

Créez une clé dédiée avec capacité de signature et une date d’expiration :

```bash
gpg --full-generate-key
```

Choisissez RSA, une taille de 3072 ou 4096 bits, une durée adaptée à votre
maintenance et une identité telle que « Masternaka archive ». Protégez la clé
avec une phrase secrète. Relevez l’empreinte complète de la clé primaire :

```bash
gpg --list-secret-keys --keyid-format long --with-fingerprint
```

Utilisez les 40 caractères hexadécimaux sans espaces de l’empreinte primaire,
jamais un identifiant court ni l’empreinte d’une sous-clé. Exportez dans un
répertoire privé temporaire, hors du dépôt, en remplaçant `EMPREINTE` :

```bash
umask 077
mkdir -m 700 "$HOME/masternaka-signing-private"
gpg --armor --export-secret-keys EMPREINTE > "$HOME/masternaka-signing-private/private.asc"
```

Dans **Settings → Secrets and variables → Actions**, créez :

| Type | Nom | Valeur |
| --- | --- | --- |
| Secret | `APT_SIGNING_PRIVATE_KEY` | Contenu intégral du fichier `private.asc`, en armure ASCII |
| Secret | `APT_SIGNING_PASSPHRASE` | Phrase secrète de la clé ; peut être absent si la clé n’en possède pas |
| Variable | `APT_SIGNING_KEY_FINGERPRINT` | Empreinte primaire complète, sans espaces |
| Variable | `APT_REPOSITORY_ENABLED` | `true`, à définir une fois la configuration terminée |

Copiez le contenu du fichier directement dans le champ du secret, sans le
publier dans un message, une issue, une sortie de terminal ou un artefact.
Conservez une sauvegarde sécurisée de la clé et de son certificat de révocation,
puis supprimez la copie d’export temporaire. Publiez séparément l’empreinte de la
clé publique dans un canal de confiance pour permettre aux clients de la comparer.

## Activer GitHub Pages

Dans **Settings → Pages → Build and deployment → Source**, choisissez
**GitHub Actions**. Vérifiez que GitHub Actions est autorisé et que les règles
de l’environnement `github-pages` autorisent `main`. Ce workflow remplace le
contenu du site Pages de ce dépôt par le dépôt APT : réservez ce site à cet usage.

Après l’envoi des fichiers du workflow sur `main` et l’activation de la variable,
lancez le workflow depuis **Actions** en choisissant `main`, ou envoyez un nouveau
commit sur `main`. Aucun jeton personnel n’est nécessaire. Le travail APT reçoit
les permissions `contents: read`, `pages: write` et `id-token: write` ; la clé
privée n’est fournie qu’à l’étape de signature.

Avant tout déploiement, le script vérifie les noms des paquets, leur unicité et leur architecture
`all` ou `amd64`, puis l’empreinte primaire de la clé importée. Le champ
`Architectures` du fichier Release annonce `amd64 all`. Il génère `Packages`,
`Packages.gz`, `Release`, `InRelease` et `Release.gpg`, avec signatures SHA-256.
Seule la clé publique `masternaka-archive-keyring.asc` rejoint le site et l’artefact
Pages. Le répertoire GnuPG et le fichier de phrase secrète sont temporaires,
avec accès privé, puis supprimés en fin d’étape. Les deux signatures sont vérifiées
par `gpgv`, puis un véritable `apt-get update` et un téléchargement de chaque paquet
sont exécutés dans des répertoires isolés avec `Signed-By` et comparaison binaire.
Les données et sources APT de la machine de construction restent inchangées.

## Installation sur Linux Mint

Attendez la réussite du déploiement Pages. Téléchargez la clé publique :

```bash
sudo apt install ca-certificates curl gnupg
curl --fail --location https://masternaka.github.io/masternaka-repository/masternaka-archive-keyring.asc --output /tmp/masternaka.asc
gpg --show-keys --with-fingerprint /tmp/masternaka.asc
```

Comparez les 40 caractères de l’empreinte primaire à l’empreinte publiée par le
mainteneur dans un canal de confiance. En cas de différence, arrêtez l’installation.
Après vérification :

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo install -m 0644 /tmp/masternaka.asc /etc/apt/keyrings/masternaka.asc
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/masternaka.asc] https://masternaka.github.io/masternaka-repository/ ./' | sudo tee /etc/apt/sources.list.d/masternaka.list
sudo apt update
sudo apt install minttweaks
```

`Signed-By` limite cette clé à ce dépôt ; n’utilisez pas `apt-key` ou
`trusted=yes`. Les publications suivantes deviennent visibles après
`sudo apt update`. Installez la mise à jour avec `sudo apt install minttweaks`,
ou avec votre gestionnaire de mises à jour. Le workflow n’installe rien sur votre
ordinateur ; la configuration locale des mises à jour automatiques reste applicable.

## Désactivation et entretien

Pour arrêter les nouveaux déploiements, passez `APT_REPOSITORY_ENABLED` à `false`.
Le dernier site Pages reste accessible jusqu’à sa désactivation dans les réglages
Pages. La désactivation du workflow ne révoque pas la clé publique déjà installée.
Pour retirer le dépôt d’un client :

```bash
sudo rm /etc/apt/sources.list.d/masternaka.list /etc/apt/keyrings/masternaka.asc
sudo apt update
```

MintTweaks déjà installé reste présent ; sa suppression éventuelle se fait avec
`sudo apt remove minttweaks`.

Surveillez l’expiration de la clé. Pour prolonger la même clé, modifiez sa date
localement avec `gpg --edit-key EMPREINTE`, exportez à nouveau la clé privée dans
le secret, puis republiez. Les clients doivent télécharger et vérifier à nouveau
la clé publique mise à jour. Pour remplacer la clé, préparez une nouvelle clé,
actualisez secret et variable d’empreinte ensemble, puis publiez et distribuez
la nouvelle clé publique aux clients par un canal de confiance. Une rotation
sans migration des clés clientes provoque une erreur de signature APT ; ne
contournez pas cette erreur en désactivant la vérification.

Si la clé est compromise, désactivez la publication, révoquez-la, prévenez les
utilisateurs et remplacez-la avec une nouvelle empreinte vérifiée. Révoquer une
clé ne suffit pas à retirer automatiquement sa copie chez tous les clients.

## Helium et Zen : sources officielles

Ces navigateurs sont retirés de `apps.json`. Le prochain déploiement de ce
catalogue ne les inclura plus dans l’index APT ni dans `pool/`. Les paquets déjà
installés sur les machines clientes ne sont pas désinstallés.

- **Helium** : utiliser le [dépôt APT officiel](https://helium.computer/download),
  `https://pkg.helium.computer/deb/`, suite `stable main`, paquet `helium-bin`.
  Ajouter cette source avec sa clé officielle, actualiser APT puis vérifier
  `apt-cache policy helium-bin` avant toute mise à jour.
- **Zen** : utiliser l’[installateur officiel](https://docs.zen-browser.app/guides/install-linux)
  qui télécharge depuis GitHub et installe dans `~/.tarball-installations/zen`.
  Les mises à jour passent par Zen, sans reconstruction Debian Masternaka.

MintTweaks propose ces deux voies dans sa page Logiciels natifs. Si l’ancien
paquet non officiel `zen-browser` est présent, le supprimer avec le gestionnaire
de paquets avant l’installation officielle ; conserver son profil personnel et
sauvegarder ses données. Aucune suppression ni migration automatique des profils
n’est effectuée. Les anciens outils de recette et leurs tests sont conservés pour
référence ; ils ne sont pas activés dans le catalogue ni la publication courante.

## Ajouter une application

Ajoutez une entrée explicite dans `apps.json`, par exemple :

```json
{
  "apps": [
    {"repository": "Masternaka/MintTweaks", "package": "minttweaks", "architecture": "all"},
    {"repository": "Masternaka/AutreApplication", "package": "autre-application", "architecture": "all"}
  ]
}
```

Cet exemple de seconde application est fictif : ne l’ajoutez qu’après création
de sa release et de son paquet. Le collecteur n’explore pas vos autres dépôts.
Pour la recette `deb` (valeur implicite si `recipe` est absent), chaque application
doit publier une release standard avec un seul asset
`PACKAGE_VERSION_ARCHITECTURE.deb`, posséder un digest GitHub `sha256:` et déclarer dans le
paquet Debian les mêmes `Package`, `Version` et architecture que la configuration. Les clés JSON
inconnues, listes vides et doublons de dépôt ou de paquet sont rejetés. Les
seules architectures acceptées sont `all` et `amd64` ; `arm64` et les autres
architectures sont refusées. L’ancienne recette `zen-tar` reste dans les outils pour référence, mais ne doit
plus être ajoutée au catalogue. Elle est
strictement réservée au triplet `zen-browser/desktop`, `zen-browser`, `amd64`.
Les commandes ou recettes arbitraires dans `apps.json` ne sont pas autorisées.

Une fois le fichier envoyé sur `main`, un nouveau dépôt signé complet est généré.
Retirer une entrée retire ce paquet du prochain instantané ; cela ne désinstalle
pas les copies présentes sur les machines clientes. Les dépendances système sont
résolues par les dépôts Mint/Ubuntu déjà configurés chez le client.

## Sécurité de la collecte

Le `GITHUB_TOKEN` sert uniquement aux appels HTTPS vers `api.github.com`. Les
assets sont téléchargés sans ce jeton depuis GitHub, avec redirections limitées
aux hôtes d’assets GitHub autorisés. Les redirections d’une requête API portant
un jeton sont refusées. Le digest est obligatoire avant inspection du paquet.
Les tests unitaires simulent les réponses réseau et les métadonnées Debian :
aucune vraie clé privée, commande de signature ou connexion réseau n’est requise
pour les exécuter.
