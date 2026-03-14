# CS2 Demo Tool

A local web app for visualizing CS2 demo files — featuring a smooth replay viewer and position heatmaps, built on top of the [cs2dave.com](https://cs2dave.com) API.

![CS2 Demo Tool](https://raw.githubusercontent.com/pebrauner/cs2-demo-tool/main/static/preview.png)

## Features

- **Replay Viewer** — scrub through rounds tick by tick with smooth 60fps interpolation; see player positions, aim direction, grenade throws, kill feed, and economy
- **Position Heatmap** — generate per-player position heatmaps filtered by round, side (CT/T), and alive-only status
- **Heatmap Export** — download the heatmap as PNG or JPEG at full 1024×1024 resolution
- **Sharp Zoom** — canvas-based rendering keeps everything crisp at any zoom level (1×–10×)
- **Scoreboard** — live K/D/HS%/ADR/KAST% stats filtered to the currently viewed round

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI + Uvicorn |
| Frontend | Vanilla HTML/CSS/JS (single file) |
| Heatmap | NumPy · SciPy gaussian_filter · Matplotlib turbo colormap · Pillow |
| Data source | [cs2dave.com](https://cs2dave.com) API |

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the server
python -m uvicorn server:app --reload --port 8000

# 3. Open in browser
# http://localhost:8000
```

## Usage

1. Select a demo from the dropdown (demos are fetched from cs2dave.com by filename)
2. Click **Load** — players appear in the left sidebar
3. Click any player to open their **Replay** or **Heatmap** view
4. Use the round chips, side filters, and scrubber to explore the data

## Project Structure

```
cs2-demo-tool/
├── server.py          # FastAPI backend — API routes + heatmap generation
├── demo_parser.py     # Local .dem parser (demoparser2 wrapper, Phase 2)
├── static/
│   └── index.html     # Full frontend (single-file app)
├── requirements.txt
└── README.md
```

---

Part of [Pedro Brauner's Lab](https://pebrauner.com/lab) · Work in progress
