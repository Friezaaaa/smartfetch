# Offline fixture-generation tool provenance

The checked-in 16 media fixtures were generated locally. Neither portable
tool is an application dependency; no executable, installer, archive, cache,
or generated extraction directory belongs in the repository.

| Tool | HTTPS origin | Resolved version | Download SHA-256 |
|---|---|---|---|
| eSpeak NG MSI | [official project release asset](https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi) | Release tag `1.52.0`; MSI `ProductVersion` is `1.51.0` | `7f673c709ea5dd579d3b5ebb98688cc575328a6ab7438d2bc405b88cedaeafb9` |
| FFmpeg essentials ZIP | [FFmpeg-project-linked Windows build](https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip) | `ffmpeg version 9.0.1-essentials_build-www.gyan.dev` | `fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9` |

The FFmpeg archive hash matched the publisher's HTTPS `.sha256` sidecar.
The extracted FFmpeg executable SHA-256 was
`72a489eccd008c2ec2c0a5856c5c75bc3d8bbfa90166c4566865c246445e6aa3`;
the FFprobe executable SHA-256 was
`19202b23c0043f15ad1b7bce2344f406fd52bd6efd8f995ce02e7392a1cec52f`.
The extracted eSpeak executable SHA-256 was
`3080ec3822c1b266ef557c710bc79a97d20a7ab133a34bac308b81ab0afc733e`.

The eSpeak tag/MSI version discrepancy is reported as observed; the portable
executable's `--version` did not produce a reliable result in this environment.
It was invoked with an explicit extracted data path for local WAV generation.
Do not infer that its runtime binary version is `1.52.0` solely from the tag.
