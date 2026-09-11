# Regenerate the README demo GIF:
#   brew install vhs
#   uv sync --extra dev
#   uv run python scripts/demo_repo.py /tmp/preflight-demo   # a git repo with a broken branch
#   cd /tmp/preflight-demo && vhs <repo>/assets/demo.tape
# Then shrink it before committing:
#   ffmpeg -i demo.gif -vf "fps=12,palettegen=max_colors=32:stats_mode=diff" -y pal.png
#   ffmpeg -i demo.gif -i pal.png \
#     -lavfi "fps=12[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
#     -y <repo>/assets/demo.gif

Output demo.gif

Set Shell "zsh"
Set FontSize 15
Set Width 1200
Set Height 760
Set Padding 24
Set Framerate 20
Set Theme {"name": "jba", "black": "#182333", "red": "#e5989b", "green": "#7cc576", "yellow": "#FA9306", "blue": "#2196F0", "purple": "#A8C9EE", "cyan": "#2196F0", "white": "#F6F8FB", "brightBlack": "#3b4252", "brightRed": "#e5989b", "brightGreen": "#7cc576", "brightYellow": "#FA9306", "brightBlue": "#2196F0", "brightPurple": "#A8C9EE", "brightCyan": "#2196F0", "brightWhite": "#FFFFFF", "background": "#0e1117", "foreground": "#F6F8FB", "cursor": "#FA9306"}
Set TypingSpeed 22ms

Hide
Type "source $PREFLIGHT_VENV/bin/activate && clear"
Enter
Sleep 500ms
Show

# 1. The pull request: one staging model renamed a column.
Type "git diff main --stat && git diff main | grep '^[-+] '"
Sleep 300ms
Enter
Sleep 3500ms

Type "clear"
Enter
Sleep 300ms

# 2. Preflight, with no warehouse anywhere.
Type "dbt-preflight run --base-ref main --comment-file comment.md"
Sleep 300ms
Enter
Sleep 7000ms

Type "clear"
Enter
Sleep 300ms

# 3. The comment a reviewer sees.
Type "head -24 comment.md"
Sleep 300ms
Enter
Sleep 7000ms
