# yt2wiki

**Turn technical videos into illustrated, searchable guides.**

Stop scrubbing through tutorials. Get the instructions, commands, screenshots and important details in an article you can search, edit and export.

### How it works

Upload a video → Whisper transcribes it → your multimodal LLM writes the article and picks useful screenshots.

yt2wiki is local-first and uses external inference services. No bundled models or GPU required for the publisher.

### Get started

```bash
git clone https://github.com/mitchins/yt2wiki.git
cd yt2wiki
# Pin the release you want (never :latest; :main tracks the moving branch):
YT2WIKI_IMAGE_TAG=v0.1.0 docker compose pull
YT2WIKI_IMAGE_TAG=v0.1.0 docker compose up -d
```

### Licence

Apache-2.0 — see [LICENSE](LICENSE). Model weights are not bundled; ASR and
inference run against services you configure. Generated articles derive from
the videos you supply — respect those videos' own terms and rights.

Open http://localhost:8080.

Connect a Whisper HTTP service and a multimodal LLM to generate articles. The library works without them.

**[Example article](docs/screenshots/p3-hermes-article.html)** · **[Setup / Portainer](docs/deploy.md)**
