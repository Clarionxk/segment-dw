# Video & Segment Download Plan

Paste or generate your video download plan below. The downloader automatically detects:
- **Markdown Tables** (recommended for AI agents)
- **Key-Value Bullet Lists**
- **Inline Markdown Links**

---

## Download Queue

| # | Title | URL | Start | End | Resolution | Notes |
|---|---|---|---|---|---|---|
| 1 | Half-Life 2 - Ravenholm Snippet | https://www.youtube.com/watch?v=dQw4w9WgXcQ | 00:10 | 00:30 | 1080p | Example 20s clip |
| 2 | Classic Gaming Demo | https://youtu.be/dQw4w9WgXcQ | 00:45 | 01:10 | 720p | Example 25s clip |
| 3 | Full Showcase Video | https://www.youtube.com/watch?v=dQw4w9WgXcQ | | | best | Full video download |

---

## Instructions for AI Agents & Users

When generating or editing this file, you can use any of these formats:

### Format A: Table (Default & Cleanest)
```markdown
| Title | URL | Start | End | Resolution |
| Half-Life 2 Ravenholm | https://www.youtube.com/watch?v=... | 01:20 | 03:45 | 1080p |
| Doom 3 Gameplay | https://youtu.be/... | 00:30 | 01:15 | 720p |
| Full Video | https://www.youtube.com/watch?v=... | | | best |
```

### Format B: Bullet List
```markdown
### Half-Life 2 Ravenholm
- URL: https://www.youtube.com/watch?v=...
- Start: 01:20
- End: 03:45
- Resolution: 1080p
```

### Format C: Inline Links
```markdown
- [Half-Life 2](https://www.youtube.com/watch?v=...) (01:20 - 03:45) [1080p]
```

### Execution
Run on local or VPS:
```bash
python download.py
# or dry-run to preview:
python download.py --dry-run
```

