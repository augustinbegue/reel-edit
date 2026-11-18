# reel-edit

A [Claude Code](https://claude.com/claude-code) skill that builds a short vertical reel in DaVinci Resolve from derushed car-shoot selects and a music track. It finds the beats, picks which select plays where and for how long, and builds a timeline with the music on A1 and every cut on the beat.

Run [derush](https://github.com/augustinbegue/derush) first: this skill reads its `segments.json` and uses its sheet renderer.

## Requirements

- DaVinci Resolve with *Preferences > System > General > External scripting using* set to *Local*
- `ffmpeg` on PATH
- Python 3 (no packages needed)
- The `derush` skill installed at `~/.claude/skills/derush`

## Install

```bash
ln -s "$PWD/skill" ~/.claude/skills/reel-edit
```

Then ask Claude to "cut a reel from the selects to this track". The full workflow is in [`skill/SKILL.md`](skill/SKILL.md). [`skill/references/playbook.json`](skill/references/playbook.json) holds the editing rules and shot plan, drawn from nine reference reels.
