# Regenerate the demo recording (assets/demo.gif and assets/demo.mp4):
#
#   uv sync --extra dev
#   uv run python scripts/demo_repo.py /tmp/preflight-demo    # a git repo with a PR branch
#   cd /tmp/preflight-demo && PREFLIGHT_VENV=<repo>/.venv vhs <repo>/assets/demo.tape
#
# Then shrink the GIF before committing it:
#   ffmpeg -i demo.gif -vf "fps=12,palettegen=max_colors=32:stats_mode=diff" -y pal.png
#   ffmpeg -i demo.gif -i pal.png \
#     -lavfi "fps=12[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle" \
#     -y <repo>/assets/demo.gif
#
# The recorded change counts cancelled orders in customer lifetime revenue. Every test
# passes, so this is the shape nothing else catches without a warehouse: only the
# comparison against the base branch shows that the numbers moved.

Output demo.gif
Output demo.mp4

Set Shell "bash"
Set FontSize 15
Set Width 1200
Set Height 820
Set Padding 24
Set Framerate 20
Set Theme {"name": "jba", "black": "#182333", "red": "#e5989b", "green": "#7cc576", "yellow": "#FA9306", "blue": "#2196F0", "purple": "#A8C9EE", "cyan": "#2196F0", "white": "#F6F8FB", "brightBlack": "#3b4252", "brightRed": "#e5989b", "brightGreen": "#7cc576", "brightYellow": "#FA9306", "brightBlue": "#2196F0", "brightPurple": "#A8C9EE", "brightCyan": "#2196F0", "brightWhite": "#FFFFFF", "background": "#0e1117", "foreground": "#F6F8FB", "cursor": "#FA9306"}
Set TypingSpeed 22ms

Hide
Type "source $PREFLIGHT_VENV/bin/activate && clear"
Enter
Sleep 500ms
Show

# 1. The pull request: one line gone from a mart. Nothing a reviewer would stop on.
Type "git diff main --stat && git diff main | grep '^[-+] '"
Sleep 300ms
Enter
Sleep 4000ms

Type "clear"
Enter
Sleep 300ms

# 2. Preflight, with no warehouse and no credentials anywhere.
Type "dbt-preflight run --base-ref main --comment-file comment.md"
Sleep 300ms
Enter
Sleep 24000ms

Type "clear"
Enter
Sleep 300ms

# 3. The comment a reviewer sees. Every test passes, and the first line still says
#    three metrics moved.
Type "sed -n '2,14p' comment.md"
Sleep 300ms
Enter
Sleep 8000ms

Type "clear"
Enter
Sleep 300ms

# 4. What the change actually did, against main on the same synthetic data.
Type "sed -n '16,30p' comment.md"
Sleep 300ms
Enter
Sleep 10000ms
