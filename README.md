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
docker compose pull
docker compose up -d
```

Open http://localhost:8080.

Connect a Whisper HTTP service and a multimodal LLM to generate articles. The library works without them.

**[Example article](docs/screenshots/p3-hermes-article.html)** · **[Setup / Portainer](docs/deploy.md)**
