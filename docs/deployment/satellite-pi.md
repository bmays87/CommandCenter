# Voice Satellite on a Raspberry Pi

Mjölnir as a kitchen/desk satellite: a Raspberry Pi with a microphone and
speaker, talking to a Command Center hub elsewhere on the network. Everything
below runs offline once models are downloaded — no cloud STT/TTS.

## Hardware

- Raspberry Pi 4 or 5 (2 GB is enough; 4 GB gives faster-whisper headroom).
- A USB speakerphone/conference mic is the low-effort option (mic + speaker,
  echo handling in hardware). ReSpeaker-class HATs also work.
- A decent SD card or USB SSD; whisper models make the difference visible.

## Install

64-bit Raspberry Pi OS (Bookworm+), then:

```bash
sudo apt install -y libportaudio2            # PortAudio for sounddevice
python3 -m venv ~/mjolnir && source ~/mjolnir/bin/activate
pip install 'prodeo-mjolnir[audio]' \
    prodeo-wakeword-openwakeword prodeo-stt-fasterwhisper prodeo-tts-piper
```

All three reference engines are CPU-only. (`prodeo-stt-parakeet` is the GPU
alternative — do not install it on a Pi.)

Download the models once:

```bash
python -m piper.download_voices en_GB-alan-medium --data-dir ~/voices
python - <<'EOF'
import openwakeword.utils
openwakeword.utils.download_models()      # stock wake word models
EOF
# faster-whisper downloads its model on first use (cached in ~/.cache)
```

## Configure

```bash
# /etc/default/mjolnir  (environment file)
MJOLNIR_SERVER_URL=http://hub.local:8600
MJOLNIR_API_TOKEN=<the hub's PRODEO_API_TOKEN>
MJOLNIR_CLIENT_ID=kitchen
MJOLNIR_NODE=kitchen-pi
MJOLNIR_ENGINES='{"piper": {"voice_path": "/home/pi/voices/en_GB-alan-medium.onnx"},
                  "faster-whisper": {"model": "base.en"}}'
# persona, if you want one:
MJOLNIR_HONORIFIC=sir
MJOLNIR_PERSONA_PACK=steward
```

The wake word defaults to "mjölnir" (Norse pronunciation, "MYOL-neer");
until the custom OpenWakeWord model ships, the engine logs a warning and
falls back to the stock `hey_jarvis` model. Set `MJOLNIR_WAKE_WORD` to any
stock or custom model to change it.

On the **hub**, tell the Notifier which channels are for an away user, so
phone push goes quiet while you're talking to the satellite:

```bash
PRODEO_NOTIFY_AWAY_ONLY_CHANNELS='["ntfy"]'
```

## Run as a service

```ini
# /etc/systemd/system/mjolnir.service
[Unit]
Description=Mjolnir voice satellite
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=pi
EnvironmentFile=/etc/default/mjolnir
ExecStart=/home/pi/mjolnir/bin/prodeo-mjolnir
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now mjolnir
journalctl -u mjolnir -f     # watch it come up: engines.loaded x3, pipeline.started
```

## Verify

Say the wake word, then "status". On the hub, `GET /api/presence` should show
the satellite (attentive right after an exchange), and
`GET /api/events?type=voice.*` shows the exchange with `node=kitchen-pi`.

## Latency notes

The budgets (wake→ack < 1.5 s, command→response < 3 s for cached queries)
hold on a Pi 5 with `base.en`; on a Pi 4, prefer `tiny.en` or accept ~1 s
more STT time. State queries never hit the network — they read the
event-stream-fed local cache — so server distance affects only commands.
