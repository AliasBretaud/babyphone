# Baby Monitor Analyzer (Python)

Cette application Python se connecte au serveur BabyPhone via WebRTC pour analyser en temps réel la vidéo et l'audio. Elle détecte les mouvements du bébé (mouvement léger, assis, debout) à l'aide d'un modèle de pose pré‑entraîné et signale les pleurs grâce à l'analyse audio tout en enregistrant le flux sonore.

---

## Fonctionnalités

- Client WebRTC autonome (basé sur `aiortc`) qui rejoint une salle en tant que `viewer`.
- Signalisation via WebSocket avec possibilité de désactiver la vérification TLS (certificats auto-signés).
- Analyse vidéo par `MediaPipe Pose` (modèle pré-entrainé) pour classer:
  - `mouvement` – bébé allongé qui bouge sensiblement.
  - `assis` – bébé redressé (torse quasi vertical).
  - `debout` – posture verticale stable.
- Détection des pleurs (analyse audio heuristique).
- Détection des mouvements du bébé (changement de position notable).
- Détection du réveil (position assise ou debout maintenue ≥ 3 s).
- Snapshots de debug annotés (landmarks MediaPipe) configurables.
- Logs structurés des événements détectés.

---

## Installation

1. Créez un virtualenv Python 3.9+ :

   ```bash
   cd python_analyzer
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Installez les dépendances :

   ```bash
   pip install -r requirements.txt
   ```

   > `mediapipe`, `aiortc` et `opencv-python` téléchargent des modèles/prérequis lors de l'installation. Assurez-vous d'avoir une connexion réseau pour la première installation.

---

## Utilisation

```bash
python run_analyzer.py \
  --signaling wss://localhost:3443/ws \
  --room baby \
  --no-ssl-verify \
  --record-audio \
  --snapshots
```

Arguments principaux :

- `--signaling` : URL WebSocket du serveur Node (par défaut `wss://localhost:3443/ws`).
- `--room` : nom de la salle (`baby` par défaut ou `?room=...` côté Web).
- `--ssl-verify/--no-ssl-verify` : active/désactive la vérification TLS (utile avec certificats auto-signés du projet).
- `--audio-dir` : dossier de sortie pour l'enregistrement audio (`python_analyzer/output` par défaut).
- `--record-audio / --no-record-audio` : activer ou non la sauvegarde du flux audio en WAV (désactivé par défaut).
- `--snapshots / --no-snapshots` : activer les captures annotées à chaque événement détecté (mouvement ou réveil).
- `--snapshot-dir` : dossier de sortie des captures annotées (défaut `python_analyzer/output/snapshots`).

Le script se connecte, attend un broadcaster et consomme le flux média. Les événements (pleurs, mouvement, réveil) sont affichés dans la console. Si `--record-audio` est activé, un fichier `python_analyzer/output/audio/baby_audio_<horodatage>.wav` est écrit. Avec `--snapshots`, chaque événement génère une image annotée enregistrée dans `snapshot-dir` avec un identifiant de trace commun au log.

> ℹ️ Au premier lancement, le modèle MediaPipe Tasks (`pose_landmarker_full.task`) est téléchargé automatiquement dans `python_analyzer/models/`. Vous pouvez fournir votre propre modèle via la variable d'environnement `POSE_MODEL_PATH`.

---

## Architecture

```
python_analyzer/
├── run_analyzer.py          # Point d'entrée CLI
├── requirements.txt
└── baby_monitor/
    ├── __init__.py
    ├── analyzer.py          # Client WebRTC + boucle principale
    ├── audio.py             # Détection de pleurs (+ enregistrement optionnel)
    ├── config.py            # Lecture de la configuration (CLI/env)
    ├── pose.py              # Analyse de pose MediaPipe + classification
    └── protobuf_compat.py   # Adaptateur protobuf (API non dépréciée)
```

### Pipeline vidéo
1. Réception des frames via `aiortc`.
2. Passage à `MediaPipe Pose` (modèle pré-entrainé) pour extraire les landmarks 3D.
3. Classification posture (debout/assis/allongé) via analyse angulaire indépendamment de l'angle caméra.
4. Détection de mouvement par variation des landmarks.

### Pipeline audio
1. Conversion PCM mono 16 bits.
2. Écriture dans un fichier WAV en continu.
3. Fenêtrage glissant + analyse fréquentielle pour identifier des signatures de pleurs.

---

## Variables d'environnement

Chaque argument CLI possède un équivalent via variables :

| Variable               | Description                                 | Valeur par défaut                  |
|------------------------|---------------------------------------------|------------------------------------|
| `ANALYZER_SIGNALING`   | URL WebSocket de signalisation              | `wss://localhost:3443/ws`          |
| `ANALYZER_ROOM`        | Salle à rejoindre                           | `baby`                             |
| `ANALYZER_SSL_VERIFY`  | `true` / `false` pour activer TLS strict    | `false`                            |
| `ANALYZER_AUDIO_DIR`        | Dossier d'enregistrement audio            | `python_analyzer/output/audio`     |
| `ANALYZER_AUDIO_RECORD`     | `true` / `false` pour écrire un WAV       | `false`                            |
| `ANALYZER_SNAPSHOT_ON_EVENT`| `true` / `false` pour activer les captures| `false`                            |
| `ANALYZER_SNAPSHOT_DIR`     | Dossier pour les captures annotées        | `python_analyzer/output/snapshots` |
| `POSE_MODEL_PATH`           | Chemin vers un modèle `.task` personnalisé (facultatif) | _auto-download_ |

---

## Limites & pistes d'amélioration

- Les heuristiques de classification et de pleurs peuvent être ajustées selon les caméras/bruits ambiants.
- Pour une précision accrue, intégrer un modèle audio spécialisé (ex. modèle de classification de cris d'enfant) et/ou un module de suivi multi-caméra.
- Ajouter des notifications (email, push) en plus des logs console.

---

## Développement

- Activez le logging détaillé en définissant `LOG_LEVEL=DEBUG`.
- Les modules sont découplés : vous pouvez tester l'analyse vidéo ou audio indépendamment en simulant des flux.
- Respectez la licence des modèles utilisés (MediaPipe Pose).
