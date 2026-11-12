# TradeLens AI LaunchAgent Templates

Rendered `deploy/launchd/*.plist` files are **machine-specific runtime artifacts** (containing the target machine's repository root, Python executable path, log paths, and network flags) and are intentionally `.gitignore`d.

Never copy plist files rendered on an Office Mac into the Home Mac runtime directory, and never commit machine-specific plists to Git.

To render validated LaunchAgent templates directly on the Home Mac (without installing or bootstrapping them into `~/Library/LaunchAgents`):

```zsh
mise run tradelens-stage-b-render-launchd
mise run tradelens-stage-b-launchd-lint
```

