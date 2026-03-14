# CS2 Demo Tool

A local web app for visualizing CS2 demo files — featuring a smooth replay viewer and position heatmaps, built with a custom demo parser on top of `demoparser2`.

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
| Demo parsing | demoparser2 |
| Heatmap | NumPy · SciPy gaussian_filter · Matplotlib turbo colormap · Pillow |

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Drop your .dem files into the demos/ folder

# 3. Run the server
python -m uvicorn server:app --reload --port 8000

# 4. Open in browser
# http://localhost:8000
```

## Usage

1. Select a demo from the dropdown or upload a `.dem` file directly
2. Click **Load** — players appear in the left sidebar
3. Click any player to open their **Replay** or **Heatmap** view
4. Use the round chips, side filters, and scrubber to explore the data

## Project Structure

```
cs2-demo-tool/
├── server.py          # FastAPI backend — API routes + heatmap generation
├── demo_parser.py     # .dem parser (demoparser2 wrapper)
├── static/
│   └── index.html     # Full frontend (single-file app)
├── requirements.txt
└── README.md
```

---

Part of [Pedro Brauner's Lab](https://pebrauner.com/lab) · Work in progress
