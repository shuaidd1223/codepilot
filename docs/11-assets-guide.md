# Demo Assets Guide

Place the following assets in `docs/` for the README:

## demo.gif

Record a terminal session demonstrating the core workflow:

1. Install [ScreenToGif](https://www.screentogif.com/) (Windows) or [LICEcap](https://www.cockos.com/licecap/) (macOS/Windows)
2. Open a terminal, run a typical workflow:
   ```bash
   codepilot setup .
   codepilot "实现用户登录功能" -p myproject
   codepilot task show <task_id>
   codepilot status -p myproject -v
   ```
3. Record and export as `docs/demo.gif` (建议 720p, ~15-30 秒)

## screenshot-webui.png

Screenshot of the Web UI dashboard:

```bash
codepilot ui start
# Open http://localhost:8765 in browser, take screenshot
```

## screenshot-task.png

Screenshot of task management panel:

```bash
codepilot hud -p myproject --preset full
# Or use the Web UI task panel
```
