#!/usr/bin/env python3
"""
Visualize the Smart Grocery LangGraph.

Usage:
    python scripts/visualize_graph.py

Outputs:
  - scripts/smart_grocery_graph.png  (if Pillow is installed)
  - scripts/smart_grocery_graph.html (always — open in browser)
"""

import sys
import os

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.smart_grocery.graph import graph

HERE = os.path.dirname(os.path.abspath(__file__))


def save_png():
    try:
        png_bytes = graph.get_graph().draw_mermaid_png()
        path = os.path.join(HERE, "smart_grocery_graph.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        print(f"PNG saved → {path}")
        return True
    except Exception as e:
        print(f"PNG skipped ({e})")
        return False


def save_html():
    mermaid_src = graph.get_graph().draw_mermaid()
    path = os.path.join(HERE, "smart_grocery_graph.html")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Smart Grocery Graph</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
  <style>
    body {{
      background: #0f1117;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      font-family: sans-serif;
    }}
    h1 {{ color: #e2e8f0; margin-bottom: 1.5rem; font-size: 1.2rem; letter-spacing: 0.05em; }}
    .mermaid {{
      background: #1e2130;
      border-radius: 12px;
      padding: 2rem;
      max-width: 900px;
      width: 100%;
      box-shadow: 0 4px 32px rgba(0,0,0,0.5);
    }}
  </style>
</head>
<body>
  <h1>Smart Grocery Agent — LangGraph</h1>
  <div class="mermaid">
{mermaid_src}
  </div>
  <script>mermaid.initialize({{ startOnLoad: true, theme: "dark" }});</script>
</body>
</html>
"""
    with open(path, "w") as f:
        f.write(html)
    print(f"HTML saved → {path}")
    return path


if __name__ == "__main__":
    png_ok = save_png()
    html_path = save_html()

    if not png_ok:
        print("\nTip: install Pillow + playwright for PNG output:")
        print("  pip install pillow playwright && playwright install chromium")

    # Auto-open HTML in browser
    import subprocess, platform
    cmd = {"darwin": "open", "linux": "xdg-open", "win32": "start"}.get(platform.system().lower(), "open")
    subprocess.run([cmd, html_path])
